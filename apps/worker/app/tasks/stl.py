"""
STL processing pipeline — Week 2 Day 3.

Flow:
  1.  Size guard (≤ 500 MB)
  2.  Download .stl from S3 raw bucket
  3.  Auto-detect ASCII vs binary STL
  4.  Load with trimesh
  5.  Mesh repair: fix_normals, fix_winding
  6.  Validate: vertex count > 0, face count > 0, no degenerate mesh
  7.  Export → GLB via trimesh GLTF exporter
  8.  Compress GLB with Draco via gltf-pipeline (level 7)
  9.  Split outputs larger than 32 MB
 10.  Upload processed chunks to S3 processed bucket
 11.  Persist metadata to model_metadata
 12.  Update model status → "ready"
 13.  Publish Redis event model_events:{user_id}
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
    get_plan_max_bytes,
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
# STL format detection
# ---------------------------------------------------------------------------

def _detect_stl_type(stl_path: str) -> str:
    """
    Detect whether an STL file is ASCII or binary by reading the header.

    ASCII STL files begin with the ASCII string "solid".
    Binary STL files have an 80-byte header followed by a uint32 face count;
    they may also start with "solid" in the header (false positive risk),
    so we additionally check the file size against the expected binary size.

    Returns "ascii" or "binary".
    """
    with open(stl_path, "rb") as f:
        header = f.read(80)

    # Check for ASCII marker
    try:
        header_str = header.decode("ascii", errors="ignore").strip().lower()
    except Exception:
        return "binary"

    if not header_str.startswith("solid"):
        return "binary"

    # Could be binary with "solid" in header — verify by file size
    import struct
    file_size = Path(stl_path).stat().st_size
    with open(stl_path, "rb") as f:
        f.seek(80)
        try:
            face_count = struct.unpack("<I", f.read(4))[0]
        except struct.error:
            return "ascii"

    expected_binary_size = 80 + 4 + face_count * 50
    if abs(file_size - expected_binary_size) < 10:
        return "binary"

    return "ascii"


# ---------------------------------------------------------------------------
# STL loading + repair
# ---------------------------------------------------------------------------

def _load_and_repair_stl(stl_path: str) -> any:
    """
    Load STL with trimesh, then apply mesh repairs:
      - fix_normals:  recalculate face normals, resolve winding inconsistencies
      - fix_winding:  ensure consistent face orientation

    Returns repaired trimesh.Trimesh.
    Raises RuntimeError if mesh is empty after repair.
    """
    try:
        import trimesh  # type: ignore
    except ImportError:
        raise RuntimeError(
            "trimesh is not installed. "
            "Add it to worker dependencies: pip install trimesh[easy]"
        )

    stl_type = _detect_stl_type(stl_path)
    logger.info("[STL] Detected STL type: %s", stl_type)

    logger.info("[STL] Loading %s", stl_path)
    mesh = trimesh.load(stl_path, force="mesh")

    if mesh is None or not hasattr(mesh, "vertices"):
        raise RuntimeError("trimesh.load returned invalid object — file may be corrupt")

    if len(mesh.vertices) == 0:
        raise RuntimeError("STL loaded with 0 vertices — file is empty or degenerate")

    if len(mesh.faces) == 0:
        raise RuntimeError("STL loaded with 0 faces — file is empty or degenerate")

    logger.info(
        "[STL] Before repair: %d vertices, %d faces, watertight=%s",
        len(mesh.vertices),
        len(mesh.faces),
        mesh.is_watertight,
    )

    trimesh.repair.fix_normals(mesh)
    trimesh.repair.fix_winding(mesh)

    logger.info(
        "[STL] After repair: %d vertices, %d faces, watertight=%s",
        len(mesh.vertices),
        len(mesh.faces),
        mesh.is_watertight,
    )

    return mesh


def _validate_mesh(mesh: any) -> None:
    """
    Post-repair validation. Raises RuntimeError for degenerate meshes
    that would produce invalid GLTF output.
    """
    if len(mesh.vertices) == 0:
        raise RuntimeError("Mesh has 0 vertices after repair — cannot export")
    if len(mesh.faces) == 0:
        raise RuntimeError("Mesh has 0 faces after repair — cannot export")

    # Reject meshes with NaN/Inf in vertex coordinates
    import numpy as np
    if not np.isfinite(mesh.vertices).all():
        raise RuntimeError("Mesh contains NaN or Inf vertex coordinates — corrupt geometry")

    logger.info("[STL] Mesh validation passed")


def _export_to_glb(mesh: any, output_path: str) -> None:
    """Export repaired STL mesh to GLB via trimesh."""
    try:
        import trimesh  # type: ignore
    except ImportError:
        raise RuntimeError("trimesh is not installed")

    logger.info("[STL] Exporting to GLB: %s", output_path)
    scene = trimesh.scene.scene.Scene(geometry={"mesh": mesh})
    scene.export(output_path, file_type="glb")

    if not Path(output_path).exists() or Path(output_path).stat().st_size == 0:
        raise RuntimeError(f"trimesh GLB export produced empty file at {output_path}")

    logger.info(
        "[STL] GLB exported → %s (%.1f KB)",
        output_path,
        Path(output_path).stat().st_size / 1024,
    )


def _apply_draco(glb_path: str, output_dir: str, compression_level: int = 7) -> str:
    """Draco-compress GLB via gltf-pipeline. Returns final path."""
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
        logger.warning("[STL] gltf-pipeline not found — Draco compression skipped")
        return glb_path

    if not out_path.exists():
        logger.error("[STL] gltf-pipeline produced no output — using original GLB")
        return glb_path

    in_kb = Path(glb_path).stat().st_size / 1024
    out_kb = out_path.stat().st_size / 1024
    logger.info("[STL] Draco: %.1f KB → %.1f KB", in_kb, out_kb)
    return str(out_path)


def _collect_geometry_stats(mesh: any, stl_type: str) -> dict:
    try:
        return {
            "vertices": int(len(mesh.vertices)),
            "faces": int(len(mesh.faces)),
            "is_watertight": bool(mesh.is_watertight),
            "stl_type": stl_type,
            "bounds": mesh.bounds.tolist() if mesh.bounds is not None else None,
        }
    except Exception:
        return {"stl_type": stl_type}


# ---------------------------------------------------------------------------
# Main Celery task
# ---------------------------------------------------------------------------

@celery_app.task(
    name="app.tasks.stl.process_stl",
    bind=True,
    time_limit=1800,
    soft_time_limit=1500,
    max_retries=2,
    default_retry_delay=60,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_stl(self: Task, model_id: str) -> dict:
    """Full STL processing pipeline."""
    logger.info("[STL] Starting processing for model_id=%s", model_id)

    engine = get_sync_engine()

    # ── Idempotency guard — skip if already terminal (redelivery) ──────────
    if is_already_processed(engine, model_id):
        logger.info("[STL] model_id=%s already in terminal status — skipping redelivered task", model_id)
        return {"model_id": model_id, "skipped": "already_processed"}

    # ── Redis lock — prevent duplicate concurrent execution ───────────────
    if not acquire_task_lock(model_id, "app.tasks.stl.process_stl"):
        return {"model_id": model_id, "status": "skipped", "reason": "duplicate_task"}

    model = get_model_row(engine, model_id)
    if model is None:
        logger.error("[STL] Model %s not found in DB", model_id)
        release_task_lock(model_id, "app.tasks.stl.process_stl")
        return {"error": "model_not_found", "model_id": model_id}

    user_id = str(model["uploaded_by"])
    s3_raw_key = model["raw_s3_key"]

    if not s3_raw_key:
        update_model_status(engine, model_id, "failed", error_message="No S3 raw key on model")
        return {"error": "no_s3_key", "model_id": model_id}

    # ── Per-plan size limit ────────────────────────────────────────────────
    plan = model.get("plan")
    max_bytes = get_plan_max_bytes(plan)
    file_size_bytes = model.get("file_size_bytes") or 0
    if file_size_bytes > max_bytes:
        from app.tasks.error_handler import FileTooLargeError

        exc = FileTooLargeError(
            f"File size {file_size_bytes / 1024 / 1024:.1f} MB exceeds "
            f"{plan or 'free'} plan limit of {max_bytes / 1024 / 1024:.0f} MB"
        )
        # Runs before the tempfile/download try-block below, so nothing
        # catches a raised exception here — it would previously propagate
        # out of the task entirely, skipping handle_task_failure() and
        # leaving the model stuck in "processing" forever.
        result = handle_task_failure(engine, model_id, user_id, "size_check", exc)
        release_task_lock(model_id, "app.tasks.stl.process_stl")
        return result

    with tempfile.TemporaryDirectory(prefix="stl_") as tmpdir:
        stl_local = os.path.join(tmpdir, "input.stl")
        out_dir = os.path.join(tmpdir, "output")
        os.makedirs(out_dir, exist_ok=True)

        stage = "init"
        try:
            # ── Download ────────────────────────────────────────────────────
            stage = "download"
            download_raw_file(s3_raw_key, stl_local)
            publish_model_progress(user_id, model_id, 10, "download")

            # ── Per-plan on-disk size guard ─────────────────────────────────
            stage = "size_check"
            assert_file_size(stl_local, max_bytes=max_bytes)

            # ── Detect format ──────────────────────────────────────────────
            stage = "detect"
            stl_type = _detect_stl_type(stl_local)

            # ── Load + repair ──────────────────────────────────────────────
            stage = "parse"
            mesh = _load_and_repair_stl(stl_local)

            # ── Validate ───────────────────────────────────────────────────
            stage = "validate"
            _validate_mesh(mesh)
            geom_stats = _collect_geometry_stats(mesh, stl_type)

            # ── Export → GLB ───────────────────────────────────────────────
            stage = "export"
            glb_path = os.path.join(out_dir, "model.glb")
            _export_to_glb(mesh, glb_path)
            del mesh  # free memory

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
            logger.info("[STL] Uploaded %d chunk(s)", len(uploaded_keys))

            # ── Persist metadata ───────────────────────────────────────────
            stage = "metadata"
            upsert_model_metadata(
                engine,
                model_id,
                properties={
                    "source_format": "stl",
                    "stl_type": stl_type,
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
            update_model_status(engine, model_id, "ready", processed_s3_prefix=processed_prefix)
            chunk_urls = [build_cdn_url(k) for k in uploaded_keys]
            publish_model_ready(user_id, model_id, chunk_urls)
            dispatch_webhook_event(engine, "model.ready", {"model_id": model_id}, user_id)
            release_task_lock(model_id, "app.tasks.stl.process_stl")

            logger.info("[STL] Processing complete for model_id=%s", model_id)
            return {
                "model_id": model_id,
                "status": "ready",
                "source_format": "stl",
                "stl_type": stl_type,
                "output_format": "glb",
                "chunks": len(uploaded_keys),
                "geometry": geom_stats,
            }

        except SoftTimeLimitExceeded:
            logger.error("[STL] Soft time limit exceeded at stage=%s", stage)
            release_task_lock(model_id, "app.tasks.stl.process_stl")
            return handle_task_failure(engine, model_id, user_id, stage, SoftTimeLimitExceeded("soft time limit"))

        except Exception as exc:
            logger.exception("[STL] Failed at stage=%s for model_id=%s", stage, model_id)
            result = handle_task_failure(engine, model_id, user_id, stage, exc)
            release_task_lock(model_id, "app.tasks.stl.process_stl")
            if is_retryable(exc):
                     raise self.retry(
                            exc=exc,
                        countdown=60,
                    )
            return result