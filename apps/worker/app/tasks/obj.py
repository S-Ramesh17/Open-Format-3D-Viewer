"""
OBJ processing pipeline — Week 2 Day 3.

Flow:
  1.  Size guard (≤ 500 MB)
  2.  Download .obj (+ optional .mtl sidecar) from S3 raw bucket
  3.  Load mesh with trimesh (force="mesh"), MTL preserved via trimesh resolver
  4.  Export loaded scene → GLTF using trimesh's GLTF exporter
  5.  Compress GLTF → GLB with Draco via gltf-pipeline (level 7)
  6.  Split outputs larger than 32 MB
  7.  Upload processed chunks to S3 processed bucket
  8.  Persist metadata to model_metadata
  9.  Update model status → "ready"
 10.  Publish Redis event model_events:{user_id}
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.config import settings
from app.tasks.common import (
    acquire_task_lock,
    dispatch_webhook_event,
    download_raw_file,
    get_model_row,
    get_sync_engine,
    is_already_processed,
    publish_model_progress,
    publish_model_ready,
    release_task_lock,
    run_node_tool,
    split_binary_chunks,
    update_model_status,
    upload_processed_file,
    build_cdn_url,
    upsert_model_metadata,
)
from app.tasks.error_handler import (
    assert_file_size,
    handle_task_failure,
    is_retryable,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OBJ → GLTF via trimesh
# ---------------------------------------------------------------------------

def _load_obj(obj_path: str) -> any:
    """
    Load OBJ file with trimesh.

    Uses force="mesh" to collapse multi-geometry scenes into a single mesh.
    trimesh automatically resolves MTL sidecars from the same directory,
    so the MTL file must be downloaded alongside the OBJ (handled in the
    main task by checking for a .mtl key in the same S3 prefix).

    Returns a trimesh.Trimesh or trimesh.Scene.
    Raises RuntimeError if mesh is empty or degenerate.
    """
    try:
        import trimesh  # type: ignore
    except ImportError:
        raise RuntimeError(
            "trimesh is not installed. "
            "Add it to worker dependencies: pip install trimesh[easy]"
        )

    logger.info("[OBJ] Loading %s", obj_path)
    mesh = trimesh.load(obj_path, force="mesh")

    if mesh is None:
        raise RuntimeError(f"trimesh.load returned None for {obj_path}")

    # Check for empty geometry
    if hasattr(mesh, "vertices") and len(mesh.vertices) == 0:
        raise RuntimeError("OBJ loaded with 0 vertices — file may be empty or corrupt")

    vertex_count = len(mesh.vertices) if hasattr(mesh, "vertices") else "?"
    face_count = len(mesh.faces) if hasattr(mesh, "faces") else "?"
    logger.info("[OBJ] Loaded: %s vertices, %s faces", vertex_count, face_count)

    return mesh


def _export_to_gltf(mesh: any, output_path: str) -> None:
    """
    Export a trimesh Trimesh/Scene to GLTF (.gltf + .bin sidecar or .glb).
    We export as .glb (single binary file) for simpler downstream handling.
    """
    try:
        import trimesh  # type: ignore
    except ImportError:
        raise RuntimeError("trimesh is not installed")

    logger.info("[OBJ] Exporting to GLTF: %s", output_path)

    # trimesh exports .glb when the output path ends in .glb
    # Use a scene wrapper if mesh is a raw Trimesh to preserve metadata
    if isinstance(mesh, trimesh.Trimesh):
        scene = trimesh.scene.scene.Scene(geometry={"mesh": mesh})
    else:
        scene = mesh

    scene.export(output_path, file_type="glb")

    if not Path(output_path).exists() or Path(output_path).stat().st_size == 0:
        raise RuntimeError(f"trimesh export produced empty or missing file at {output_path}")

    logger.info(
        "[OBJ] GLTF exported → %s (%.1f KB)",
        output_path,
        Path(output_path).stat().st_size / 1024,
    )


def _apply_draco(glb_path: str, output_dir: str, compression_level: int = 7) -> str:
    """
    Re-compress GLB with Draco via gltf-pipeline.
    Returns path to the Draco-compressed GLB.
    Falls back to input path if gltf-pipeline is not installed.
    """
    out_path = Path(output_dir) / (Path(glb_path).stem + "_draco.glb")

    cmd = [
        settings.GLTF_PIPELINE_BIN,
        "-i", glb_path,
        "-o", str(out_path),
        "--draco.compressMeshes",
        "--draco.compressionLevel", str(compression_level),
    ]

    try:
        run_node_tool(cmd, timeout=600, tool_name="gltf-pipeline")
    except FileNotFoundError:
        logger.warning(
            "[OBJ] gltf-pipeline not found — Draco compression skipped. "
            "Install: npm install -g gltf-pipeline"
        )
        return glb_path

    if not out_path.exists():
        logger.error("[OBJ] gltf-pipeline produced no output — using original GLB")
        return glb_path

    in_kb = Path(glb_path).stat().st_size / 1024
    out_kb = out_path.stat().st_size / 1024
    logger.info("[OBJ] Draco: %.1f KB → %.1f KB (%.0f%%)", in_kb, out_kb, (1 - out_kb / in_kb) * 100)
    return str(out_path)


def _collect_geometry_stats(mesh: any) -> dict:
    try:
        return {
            "vertices": int(len(mesh.vertices)) if hasattr(mesh, "vertices") else 0,
            "faces": int(len(mesh.faces)) if hasattr(mesh, "faces") else 0,
            "is_watertight": bool(mesh.is_watertight) if hasattr(mesh, "is_watertight") else None,
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Main Celery task
# ---------------------------------------------------------------------------

@celery_app.task(
    name="app.tasks.obj.process_obj",
    bind=True,
    time_limit=1800,
    soft_time_limit=1500,
    max_retries=2,
    default_retry_delay=60,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_obj(self: Task, model_id: str) -> dict:
    """Full OBJ processing pipeline."""
    logger.info("[OBJ] Starting processing for model_id=%s", model_id)

    engine = get_sync_engine()

    # ── Idempotency guard — skip if already terminal (redelivery) ──────────
    if is_already_processed(engine, model_id):
        logger.info("[OBJ] model_id=%s already in terminal status — skipping redelivered task", model_id)
        return {"model_id": model_id, "skipped": "already_processed"}

    # ── Redis lock — prevent duplicate concurrent execution ───────────────
    if not acquire_task_lock(model_id, "app.tasks.obj.process_obj"):
        return {"model_id": model_id, "status": "skipped", "reason": "duplicate_task"}

    model = get_model_row(engine, model_id)
    if model is None:
        logger.error("[OBJ] Model %s not found in DB", model_id)
        release_task_lock(model_id, "app.tasks.obj.process_obj")
        return {"error": "model_not_found", "model_id": model_id}

    user_id = str(model["uploaded_by"])
    s3_raw_key = model["s3_raw_key"]

    if not s3_raw_key:
        update_model_status(engine, model_id, "failed", error_message="No S3 raw key on model")
        return {"error": "no_s3_key", "model_id": model_id}

    with tempfile.TemporaryDirectory(prefix="obj_") as tmpdir:
        obj_local = os.path.join(tmpdir, "input.obj")
        out_dir = os.path.join(tmpdir, "output")
        os.makedirs(out_dir, exist_ok=True)

        stage = "init"
        try:
            # ── Download ────────────────────────────────────────────────────
            stage = "download"
            download_raw_file(s3_raw_key, obj_local)
            publish_model_progress(user_id, model_id, 10, "download")

            # Try to download a companion .mtl file (same key, different ext)
            # Failure is silently ignored — mesh loads without materials
            mtl_key = str(Path(s3_raw_key).with_suffix(".mtl"))
            mtl_local = os.path.join(tmpdir, "input.mtl")
            try:
                download_raw_file(mtl_key, mtl_local)
                logger.info("[OBJ] MTL sidecar downloaded: %s", mtl_key)
            except Exception:
                logger.debug("[OBJ] No MTL sidecar found at %s — continuing without materials", mtl_key)

            # ── Size guard ─────────────────────────────────────────────────
            stage = "size_check"
            assert_file_size(obj_local)

            # ── Load mesh ──────────────────────────────────────────────────
            stage = "parse"
            mesh = _load_obj(obj_local)
            geom_stats = _collect_geometry_stats(mesh)

            # ── Export → GLB ───────────────────────────────────────────────
            stage = "export"
            glb_path = os.path.join(out_dir, "model.glb")
            _export_to_gltf(mesh, glb_path)
            del mesh  # free memory before compression

            # ── Draco compression ──────────────────────────────────────────
            stage = "compress"
            final_path = _apply_draco(glb_path, out_dir, compression_level=7)

            # ── Split if > 32 MB ───────────────────────────────────────────
            stage = "split"
            chunks = split_binary_chunks(
                final_path, out_dir, Path(final_path).stem, ".glb"
            )

            # ── Upload to S3 ───────────────────────────────────────────────
            stage = "upload"
            processed_prefix = f"processed/{model_id}"
            uploaded_keys: list[str] = []
            for chunk_path in chunks:
                s3_key = f"{processed_prefix}/{Path(chunk_path).name}"
                upload_processed_file(chunk_path, s3_key, content_type="model/gltf-binary")
                uploaded_keys.append(s3_key)
            logger.info("[OBJ] Uploaded %d chunk(s)", len(uploaded_keys))

            # ── Persist metadata ───────────────────────────────────────────
            stage = "metadata"
            upsert_model_metadata(
                engine,
                model_id,
                properties={
                    "source_format": "obj",
                    "output_format": "glb",
                    "draco_compressed": final_path != glb_path,
                    "draco_level": 7,
                    "geometry": geom_stats,
                    "chunk_count": len(uploaded_keys),
                    "processed_keys": uploaded_keys,
                },
                spatial_tree={},
            )

            # ── Update status + publish ────────────────────────────────────
            stage = "finalize"
            update_model_status(engine, model_id, "ready", s3_processed_prefix=processed_prefix)
            chunk_urls = [build_cdn_url(k) for k in uploaded_keys]
            publish_model_ready(user_id, model_id, chunk_urls)
            dispatch_webhook_event(engine, "model.ready", {"model_id": model_id}, user_id)
            release_task_lock(model_id, "app.tasks.obj.process_obj")

            logger.info("[OBJ] Processing complete for model_id=%s", model_id)
            return {
                "model_id": model_id,
                "status": "ready",
                "source_format": "obj",
                "output_format": "glb",
                "chunks": len(uploaded_keys),
                "geometry": geom_stats,
            }

        except SoftTimeLimitExceeded:
            logger.error("[OBJ] Soft time limit exceeded at stage=%s", stage)
            release_task_lock(model_id, "app.tasks.obj.process_obj")
            return handle_task_failure(engine, model_id, user_id, stage, SoftTimeLimitExceeded("soft time limit"))

        except Exception as exc:
            logger.exception("[OBJ] Failed at stage=%s for model_id=%s", stage, model_id)
            result = handle_task_failure(engine, model_id, user_id, stage, exc)
            release_task_lock(model_id, "app.tasks.obj.process_obj")
            if is_retryable(exc):
                raise self.retry(exc=exc, countdown=60)
            return result