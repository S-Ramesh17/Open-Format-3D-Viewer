"""
STEP processing pipeline — Week 2 Day 2.

Flow:
  1.  Download .step / .stp from S3 raw bucket
  2.  Read STEP via pythonOCC STEPControl_Reader
  3.  Mesh geometry: BRepMesh_IncrementalMesh(shape, 0.1, False, 0.5)
  4.  Export shape → GLTF via RWGltf_CafWriter
  5.  Compress GLTF → GLB with Draco (gltf-pipeline, level 7)
  6.  Split outputs larger than 32 MB
  7.  Upload processed chunks to S3 processed bucket
  8.  Persist metadata to model_metadata
  9.  Update model status → "ready"
 10.  Publish Redis event model_events:{user_id}
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from celery import Task

from app.celery_app import celery_app
from app.config import settings
from app.tasks.common import (
    GLTF_CHUNK_MAX_BYTES,
    download_raw_file,
    get_model_row,
    get_sync_engine,
    publish_model_failed,
    publish_model_ready,
    run_node_tool,
    split_binary_chunks,
    update_model_status,
    upsert_model_metadata,
    upload_processed_file,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# pythonOCC: STEP → GLTF conversion
# ---------------------------------------------------------------------------

def _read_step(step_path: str) -> Any:
    """
    Open a STEP file with STEPControl_Reader and transfer all roots
    into a single compound TopoDS_Shape.

    Raises RuntimeError if the file cannot be read or no shapes are
    transferred (corrupt / empty STEP).
    """
    try:
        from OCC.Core.STEPControl import STEPControl_Reader  # type: ignore
        from OCC.Core.IFSelect import IFSelect_RetDone         # type: ignore
    except ImportError:
        raise RuntimeError(
            "pythonOCC-core is not installed. "
            "Install: conda install -c conda-forge pythonocc-core=7.7.2"
        )

    reader = STEPControl_Reader()
    status = reader.ReadFile(step_path)
    if status != IFSelect_RetDone:
        raise RuntimeError(
            f"STEPControl_Reader.ReadFile() failed with status {status} "
            f"for file: {step_path}"
        )

    n_roots = reader.TransferRoots()
    logger.info("[STEP] TransferRoots: %d root(s) transferred", n_roots)
    if n_roots == 0:
        raise RuntimeError("STEP file transferred 0 roots — file may be empty or corrupt")

    shape = reader.OneShape()
    if shape.IsNull():
        raise RuntimeError("STEPControl_Reader.OneShape() returned a null shape")

    return shape


def _mesh_shape(shape: Any, linear_deflection: float = 0.1, angular_deflection: float = 0.5) -> None:
    """
    Tessellate the BRep shape using BRepMesh_IncrementalMesh.
    Mutates the shape's triangulation in-place.

    Parameters match PRD spec:
        BRepMesh_IncrementalMesh(shape, 0.1, False, 0.5)
    """
    try:
        from OCC.Core.BRepMesh import BRepMesh_IncrementalMesh  # type: ignore
    except ImportError:
        raise RuntimeError("pythonOCC-core is not installed")

    mesh = BRepMesh_IncrementalMesh(shape, linear_deflection, False, angular_deflection)
    mesh.Perform()

    if not mesh.IsDone():
        raise RuntimeError("BRepMesh_IncrementalMesh.Perform() did not complete successfully")

    logger.info(
        "[STEP] Mesh complete — linear_deflection=%.3f angular_deflection=%.3f",
        linear_deflection,
        angular_deflection,
    )


def _export_gltf(shape: Any, output_path: str) -> None:
    """
    Export a tessellated TopoDS_Shape to a GLTF file using RWGltf_CafWriter.
    The shape is first placed into an XDE document (TDocStd_Document) as
    required by the OCCT GLTF writer API.
    """
    try:
        from OCC.Core.RWGltf import RWGltf_CafWriter                # type: ignore
        from OCC.Core.TDocStd import TDocStd_Document                # type: ignore
        from OCC.Core.TCollection import TCollection_ExtendedString   # type: ignore
        from OCC.Core.BRep import BRep_Builder                        # type: ignore
        from OCC.Core.TopoDS import TopoDS_Compound                   # type: ignore
        from OCC.Core.XCAFDoc import XCAFDoc_DocumentTool             # type: ignore
        from OCC.Core.XCAFApp import XCAFApp_Application              # type: ignore
        from OCC.Core.TDF import TDF_LabelSequence                    # type: ignore
        from OCC.Core.TCollection import TCollection_AsciiString      # type: ignore
    except ImportError:
        raise RuntimeError("pythonOCC-core is not installed")

    # Create an XDE application + document
    app = XCAFApp_Application.GetApplication()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-CAF"))
    app.NewDocument(TCollection_ExtendedString("MDTV-CAF"), doc)

    # Add shape to the XDE shape tool
    shape_tool = XCAFDoc_DocumentTool.ShapeTool(doc.Main())
    shape_tool.AddShape(shape)

    # Write GLTF
    writer = RWGltf_CafWriter(TCollection_AsciiString(output_path), False)  # False = .gltf (JSON)
    labels = TDF_LabelSequence()
    shape_tool.GetFreeShapes(labels)

    result = writer.Perform(doc, labels, None)  # type: ignore[arg-type]
    if not result:
        raise RuntimeError(f"RWGltf_CafWriter.Perform() failed for output: {output_path}")

    logger.info("[STEP] GLTF exported → %s (%.1f KB)", output_path, Path(output_path).stat().st_size / 1024)


def _compress_with_draco(gltf_path: str, output_dir: str, compression_level: int = 7) -> str:
    """
    Compress a GLTF file to GLB with Draco mesh compression using gltf-pipeline.

    Returns path to the produced .glb file.
    Raises FileNotFoundError if gltf-pipeline is not installed.
    Raises RuntimeError if compression fails.
    """
    glb_out = Path(output_dir) / (Path(gltf_path).stem + ".glb")

    cmd = [
        settings.GLTF_PIPELINE_BIN,
        "-i", gltf_path,
        "-o", str(glb_out),
        "--draco.compressMeshes",
        f"--draco.compressionLevel", str(compression_level),
    ]

    try:
        run_node_tool(cmd, timeout=600, tool_name="gltf-pipeline")
    except FileNotFoundError:
        logger.warning(
            "[STEP] gltf-pipeline not found ('%s') — Draco compression skipped. "
            "Install: npm install -g gltf-pipeline",
            settings.GLTF_PIPELINE_BIN,
        )
        # Fall back to the uncompressed GLTF as the output asset
        return gltf_path

    if not glb_out.exists():
        raise RuntimeError(f"gltf-pipeline ran but produced no output at {glb_out}")

    logger.info(
        "[STEP] Draco-compressed GLB → %s (%.1f KB)",
        glb_out,
        glb_out.stat().st_size / 1024,
    )
    return str(glb_out)


def _collect_step_geometry_stats(shape: Any) -> dict:
    """Return basic shape statistics for metadata storage."""
    try:
        from OCC.Core.BRep import BRep_Tool            # type: ignore
        from OCC.Core.TopExp import TopExp_Explorer     # type: ignore
        from OCC.Core.TopAbs import TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX  # type: ignore

        face_exp = TopExp_Explorer(shape, TopAbs_FACE)
        faces = 0
        while face_exp.More():
            faces += 1
            face_exp.Next()

        edge_exp = TopExp_Explorer(shape, TopAbs_EDGE)
        edges = 0
        while edge_exp.More():
            edges += 1
            edge_exp.Next()

        vertex_exp = TopExp_Explorer(shape, TopAbs_VERTEX)
        vertices = 0
        while vertex_exp.More():
            vertices += 1
            vertex_exp.Next()

        return {"faces": faces, "edges": edges, "vertices": vertices}
    except Exception as exc:
        logger.debug("[STEP] Geometry stats error: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Main Celery task
# ---------------------------------------------------------------------------

@celery_app.task(
    name="app.tasks.step.process_step",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_step(self: Task, model_id: str) -> dict:
    """
    Full STEP processing pipeline.
    Dispatched by the mesh queue for file_format in ('step', 'stp').
    """
    logger.info("[STEP] Starting processing for model_id=%s", model_id)

    engine = get_sync_engine()

    # ── 1. Fetch model row ─────────────────────────────────────────────────
    model = get_model_row(engine, model_id)
    if model is None:
        logger.error("[STEP] Model %s not found in DB", model_id)
        return {"error": "model_not_found", "model_id": model_id}

    user_id = str(model["uploaded_by"])
    s3_raw_key = model["s3_raw_key"]

    if not s3_raw_key:
        update_model_status(engine, model_id, "failed", error_message="No S3 raw key on model")
        return {"error": "no_s3_key", "model_id": model_id}

    with tempfile.TemporaryDirectory(prefix="step_") as tmpdir:
        ext = Path(s3_raw_key).suffix.lower() or ".step"
        step_local = os.path.join(tmpdir, f"input{ext}")
        out_dir = os.path.join(tmpdir, "output")
        os.makedirs(out_dir, exist_ok=True)

        try:
            # ── 2. Download from S3 ────────────────────────────────────────
            download_raw_file(s3_raw_key, step_local)

            # ── 3. Read STEP ───────────────────────────────────────────────
            shape = _read_step(step_local)

            # ── 4. Mesh geometry ───────────────────────────────────────────
            _mesh_shape(shape, linear_deflection=0.1, angular_deflection=0.5)

            # ── 5. Geometry stats ──────────────────────────────────────────
            geom_stats = _collect_step_geometry_stats(shape)
            logger.info("[STEP] Geometry stats: %s", geom_stats)

            # ── 6. Export STEP → GLTF ──────────────────────────────────────
            gltf_path = os.path.join(out_dir, "model.gltf")
            _export_gltf(shape, gltf_path)

            # ── 7. Draco compression GLTF → GLB ───────────────────────────
            compressed_path = _compress_with_draco(gltf_path, out_dir, compression_level=7)

            # ── 8. Split if > 32 MB ────────────────────────────────────────
            out_ext = Path(compressed_path).suffix
            out_stem = Path(compressed_path).stem
            chunks = split_binary_chunks(compressed_path, out_dir, out_stem, out_ext)

            # ── 9. Upload chunks to S3 ─────────────────────────────────────
            processed_prefix = f"processed/{model_id}"
            uploaded_keys: list[str] = []

            content_type = "model/gltf-binary" if out_ext == ".glb" else "model/gltf+json"
            for chunk_path in chunks:
                chunk_name = Path(chunk_path).name
                s3_key = f"{processed_prefix}/{chunk_name}"
                upload_processed_file(chunk_path, s3_key, content_type=content_type)
                uploaded_keys.append(s3_key)

            logger.info("[STEP] Uploaded %d chunk(s)", len(uploaded_keys))

            # ── 10. Persist metadata ───────────────────────────────────────
            upsert_model_metadata(
                engine,
                model_id,
                properties={
                    "source_format": ext.lstrip("."),
                    "output_format": out_ext.lstrip("."),
                    "geometry": geom_stats,
                    "draco_compressed": compressed_path.endswith(".glb"),
                    "draco_level": 7,
                    "chunk_count": len(uploaded_keys),
                    "processed_keys": uploaded_keys,
                },
                spatial_tree={},
            )

            # ── 11. Update model status → ready ───────────────────────────
            update_model_status(
                engine,
                model_id,
                "ready",
                s3_processed_prefix=processed_prefix,
            )

            # ── 12. Publish Redis event ────────────────────────────────────
            publish_model_ready(user_id, model_id)

            logger.info("[STEP] Processing complete for model_id=%s", model_id)
            return {
                "model_id": model_id,
                "status": "ready",
                "source_format": ext.lstrip("."),
                "output_format": out_ext.lstrip("."),
                "chunks": len(uploaded_keys),
                "geometry": geom_stats,
            }

        except Exception as exc:
            logger.exception("[STEP] Processing failed for model_id=%s: %s", model_id, exc)
            update_model_status(engine, model_id, "failed", error_message=str(exc))
            publish_model_failed(user_id, model_id, str(exc))

            if isinstance(exc, (ConnectionError, OSError)):
                raise self.retry(exc=exc)

            return {"model_id": model_id, "status": "failed", "error": str(exc)[:500]}
