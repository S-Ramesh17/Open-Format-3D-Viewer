"""
IFC processing pipeline — Week 2 Day 1 core task.

Flow:
  1. Download IFC from S3 (raw bucket)
  2. Validate schema (IFC2X3 / IFC4 only)
  3. Extract geometry via IfcOpenShell geom.iterator
  4. Extract element metadata + property sets
  5. Build spatial tree (Project→Site→Building→Storey→Space)
  6. Convert to XKT via @xeokit/xeokit-convert (Node subprocess)
  7. Upload XKT chunk(s) to S3 processed bucket
  8. Persist elements + metadata to PostgreSQL
  9. Update model status → "ready"
 10. Publish Redis event model_events:{user_id}
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
from celery.exceptions import SoftTimeLimitExceeded


from app.celery_app import celery_app
from app.config import settings

from app.tasks.common import (
    _raw_sql,
    acquire_task_lock,
    dispatch_webhook_event,
    download_raw_file,
    get_sync_engine,
    get_model_row,
    is_already_processed,
    release_task_lock,
    update_model_status,
    upsert_model_metadata,
    publish_model_progress,
    publish_model_ready,
    upload_processed_file,
    build_cdn_url,
)
from app.tasks.error_handler import (
    assert_file_size,
    handle_task_failure,
    is_retryable,
)
from app.tasks.metrics import (
    IFC_CONVERSION_DURATION,
    ACTIVE_TASKS,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SUPPORTED_SCHEMAS = {"IFC2X3", "IFC4", "IFC4X3"}

def _upsert_model_elements(engine, model_id: str, elements: list[dict]) -> None:
    """Bulk-insert model elements, replacing any existing ones for this model."""
    with engine.begin() as conn:
        conn.execute(
            _raw_sql("DELETE FROM model_elements WHERE model_id = :mid"),
            {"mid": model_id},
        )
        for el in elements:
            conn.execute(
                _raw_sql(
                    "INSERT INTO model_elements "
                    "(id, model_id, guid, element_type, name, properties, created_at) "
                    "VALUES (:id, :model_id, :guid, :element_type, :name, CAST(:properties AS jsonb), now())"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "model_id": model_id,
                    "guid": el["guid"],
                    "element_type": el.get("ifc_type"),
                    "name": el.get("name"),
                    "properties": json.dumps(el.get("properties", {})),
                },
            )

            
# ---------------------------------------------------------------------------
# IFC parsing helpers
# ---------------------------------------------------------------------------

def _extract_property_sets(ifc_file, element) -> dict[str, Any]:
    """Extract all Pset_* and custom property sets for an IFC element."""
    psets: dict[str, Any] = {}
    try:
        for definition in element.IsDefinedBy:
            if definition.is_a("IfcRelDefinesByProperties"):
                prop_set = definition.RelatingPropertyDefinition
                if prop_set.is_a("IfcPropertySet"):
                    pset_name = prop_set.Name or "UnknownPset"
                    pset_values: dict[str, Any] = {}
                    for prop in prop_set.HasProperties:
                        if prop.is_a("IfcPropertySingleValue"):
                            val = prop.NominalValue
                            pset_values[prop.Name] = val.wrappedValue if val else None
                        elif prop.is_a("IfcPropertyEnumeratedValue"):
                            pset_values[prop.Name] = [
                                v.wrappedValue for v in prop.EnumerationValues or []
                            ]
                        elif prop.is_a("IfcPropertyBoundedValue"):
                            lower = prop.LowerBoundValue
                            upper = prop.UpperBoundValue
                            pset_values[prop.Name] = {
                                "lower": lower.wrappedValue if lower else None,
                                "upper": upper.wrappedValue if upper else None,
                            }
                        else:
                            pset_values[prop.Name] = str(prop)
                    psets[pset_name] = pset_values
    except Exception as exc:
        logger.debug("Pset extraction warning for %s: %s", getattr(element, "GlobalId", "?"), exc)
    return psets


def _build_spatial_tree(ifc_file) -> dict:
    """
    Build a nested spatial tree:
    IfcProject → IfcSite → IfcBuilding → IfcBuildingStorey → IfcSpace
    """
    def _node(entity) -> dict:
        node: dict[str, Any] = {
            "guid": getattr(entity, "GlobalId", None),
            "type": entity.is_a(),
            "name": getattr(entity, "Name", None),
            "description": getattr(entity, "Description", None),
            "children": [],
        }
        # Walk IfcRelAggregates and IfcRelContainedInSpatialStructure
        try:
            for rel in entity.IsDecomposedBy:
                if rel.is_a("IfcRelAggregates"):
                    for child in rel.RelatedObjects:
                        node["children"].append(_node(child))
        except Exception:
            pass
        try:
            for rel in getattr(entity, "ContainsElements", []):
                if rel.is_a("IfcRelContainedInSpatialStructure"):
                    for obj in rel.RelatedElements:
                        node["children"].append(
                            {
                                "guid": getattr(obj, "GlobalId", None),
                                "type": obj.is_a(),
                                "name": getattr(obj, "Name", None),
                            }
                        )
        except Exception:
            pass
        return node

    projects = ifc_file.by_type("IfcProject")
    if not projects:
        return {}
    return _node(projects[0])


def _extract_elements(ifc_file) -> list[dict]:
    """
    Extract all IfcElement instances with GUID, type, name, description,
    and property sets.
    """
    elements = []
    for element in ifc_file.by_type("IfcElement"):
        guid = getattr(element, "GlobalId", None)
        if not guid:
            continue
        name = getattr(element, "Name", None)
        description = getattr(element, "Description", None)
        psets = _extract_property_sets(ifc_file, element)
        elements.append(
            {
                "guid": guid,
                "ifc_type": element.is_a(),
                "name": name,
                "description": description,
                "properties": {
                    "description": description,
                    **psets,
                },
            }
        )
    logger.info("Extracted %d IFC elements", len(elements))
    return elements


# ---------------------------------------------------------------------------
# Geometry extraction via IfcOpenShell geom.iterator
# ---------------------------------------------------------------------------

def _extract_geometry(ifc_file, ifc_path: str) -> dict:
    """
    Run IfcOpenShell geometry iterator with world coordinates + welded vertices.
    Returns summary statistics only (full mesh data goes to XKT conversion).
    """
    try:
        import ifcopenshell.geom as geom  # type: ignore

        settings = geom.settings()
        settings.set("use-world-coords", True)
        settings.set("weld-vertices", True)

        it = geom.iterator(settings, ifc_file, num_threads=1)

        vertex_count = 0
        face_count = 0
        shape_count = 0

        if it.initialize():
            while True:
                shape = it.get()
                geom_data = shape.geometry
                vertex_count += len(geom_data.verts) // 3
                face_count += len(geom_data.faces) // 3
                shape_count += 1
                if not it.next():
                    break

        logger.info(
            "Geometry: %d shapes, %d vertices, %d faces",
            shape_count, vertex_count, face_count,
        )
        return {
            "shape_count": shape_count,
            "vertex_count": vertex_count,
            "face_count": face_count,
        }
    except ImportError:
        logger.warning("ifcopenshell.geom not available — skipping geometry extraction")
        return {}
    except Exception as exc:
        logger.error("Geometry extraction error: %s", exc)
        return {"error": str(exc)}


def _compute_bounds(ifc_file) -> tuple[list[float] | None, list[float] | None]:
    """
    Compute axis-aligned bounding box across all IfcElement shapes using
    a separate world-coordinate geom.iterator pass.
    Returns (min_xyz, max_xyz) or (None, None) if geometry is unavailable.
    """
    try:
        import ifcopenshell.geom as geom  # type: ignore

        bounds_settings = geom.settings()
        bounds_settings.set("use-world-coords", True)

        it = geom.iterator(bounds_settings, ifc_file, num_threads=1)

        min_x = min_y = min_z = float("inf")
        max_x = max_y = max_z = float("-inf")
        found = False

        if it.initialize():
            while True:
                shape = it.get()
                verts = shape.geometry.verts
                for i in range(0, len(verts), 3):
                    x, y, z = verts[i], verts[i + 1], verts[i + 2]
                    min_x, min_y, min_z = min(min_x, x), min(min_y, y), min(min_z, z)
                    max_x, max_y, max_z = max(max_x, x), max(max_y, y), max(max_z, z)
                    found = True
                if not it.next():
                    break

        if not found:
            return None, None
        return [min_x, min_y, min_z], [max_x, max_y, max_z]
    except Exception as exc:
        logger.warning("[IFC] Bounds computation skipped: %s", exc)
        return None, None


# ---------------------------------------------------------------------------
# XKT conversion via Node subprocess
# ---------------------------------------------------------------------------

def _convert_to_xkt(ifc_path: str, output_dir: str) -> list[str]:
    xkt_out = os.path.join(output_dir, "model.xkt")
    convert_bin = settings.XEOKIT_CONVERT_BIN
    
    # Force invocation via 'node' if the binary is the JS script.
    # If the user has a custom binary, we treat it as an executable.
    if convert_bin.endswith(".js") or "convert2xkt.js" in convert_bin:
        # --no-experimental-fetch: works around a bug in xeokit-convert's
        # bundled web-ifc WASM loader (dist/convert2xkt.cjs.js, inside
        # createWasm()/instantiateAsync()). That loader's streaming-fetch
        # branch only checks `typeof fetch === "function"` before calling
        # fetch(wasmBinaryFile, ...) — it's missing the Node/file-URI guard
        # its own synchronous fallback path has a few lines above. Node 18+
        # ships a native global `fetch`, so under plain Node this branch now
        # fires and hands fetch() a bare filesystem path (not a file:// URL),
        # which throws "Failed to parse URL from .../web-ifc.wasm" (Invalid
        # URL) — reproduced directly against the installed package. Removing
        # the global fetch for this subprocess makes the loader correctly
        # fall back to reading the .wasm file from disk instead.
        cmd = ["node", "--no-experimental-fetch", convert_bin, "-s", ifc_path, "-o", xkt_out]
    else:
        cmd = [convert_bin, "-s", ifc_path, "-o", xkt_out]

    logger.info("Running XKT conversion: %s", " ".join(cmd))

    try:
        # Use shell=False (default) for security; pass cmd list directly
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            check=False # We handle returncode manually below
        )
        
        if result.returncode != 0:
            logger.error("xeokit-convert stderr: %s", result.stderr)
            raise RuntimeError(
                f"xeokit-convert exited with code {result.returncode}: {result.stderr[:500]}"
            )
            
        logger.info("xeokit-convert stdout: %s", result.stdout[:300])
        
    except FileNotFoundError as e:
        logger.error("Binary not found: %s. Check XEOKIT_CONVERT_BIN path.", convert_bin)
        raise e
    except subprocess.TimeoutExpired:
        raise RuntimeError("xeokit-convert timed out after 600s")

    xkt_files = sorted(Path(output_dir).glob("*.xkt"))
    logger.info("XKT conversion produced %d file(s)", len(xkt_files))
    return [str(p) for p in xkt_files]

def _split_xkt_chunks(xkt_files: list[str], output_dir: str) -> list[str]:
    """
    Ensure no XKT file exceeds XKT_CHUNK_MAX_BYTES (16 MB).
    Oversized files are split into raw binary chunks numbered _part0, _part1, …
    Returns final list of chunk paths.
    """
    max_bytes = settings.XKT_CHUNK_MAX_BYTES
    result: list[str] = []

    for xkt_path in xkt_files:
        size = os.path.getsize(xkt_path)
        if size <= max_bytes:
            result.append(xkt_path)
            continue

        # Split into chunks
        logger.info("Splitting %s (%d bytes) into %d-byte chunks", xkt_path, size, max_bytes)
        base = Path(xkt_path).stem
        with open(xkt_path, "rb") as fh:
            part = 0
            while True:
                chunk_data = fh.read(max_bytes)
                if not chunk_data:
                    break
                chunk_path = os.path.join(output_dir, f"{base}_part{part}.xkt")
                with open(chunk_path, "wb") as out:
                    out.write(chunk_data)
                result.append(chunk_path)
                part += 1

    return result


# ---------------------------------------------------------------------------
# Main Celery task
# ---------------------------------------------------------------------------

@celery_app.task(
    name="app.tasks.ifc.process_model",
    bind=True,
    time_limit=1800,
    soft_time_limit=1500,
    max_retries=2,
    default_retry_delay=60,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_model(self: Task, model_id: str) -> dict:
    """
    Full IFC processing pipeline.
    Called by the API layer after upload confirmation.
    """
    logger.info("[IFC] Task started model_id=%s", model_id)

    try:
        engine = get_sync_engine()
        _task_start = __import__('time').monotonic()
        ACTIVE_TASKS.labels(task_name="ifc.process_model").inc()
    except Exception:
        # Failure here happens before the main try/except below can catch
        # anything, so without this it falls through to Celery's generic
        # handler with no [IFC]-prefixed log and no model status update.
        logger.exception(
            "[IFC] Failed during task setup (engine/metrics init) model_id=%s", model_id
        )
        raise

    try:
        # ── 1. Idempotency guard — skip if already terminal (redelivery) ──────
        if is_already_processed(engine, model_id):
            logger.info("[IFC] model_id=%s already in terminal status — skipping redelivered task", model_id)
            return {"model_id": model_id, "skipped": "already_processed"}

        # ── 1b. Redis lock — prevent duplicate concurrent execution ──────────
        if not acquire_task_lock(model_id, "app.tasks.ifc.process_model"):
            return {"model_id": model_id, "status": "skipped", "reason": "duplicate_task"}

        # ── 1c. Fetch model row ────────────────────────────────────────────────
        model = get_model_row(engine, model_id)
        if model is None:
            logger.error("[IFC] Model %s not found in DB", model_id)
            release_task_lock(model_id, "app.tasks.ifc.process_model")
            return {"error": "model_not_found", "model_id": model_id}

        logger.info("[IFC] Model row loaded model_id=%s format=%s", model_id, model.get("format"))

        user_id = str(model["uploaded_by"])
        s3_raw_key = model["raw_s3_key"]


        if not s3_raw_key:
            update_model_status(engine, model_id, "failed", error_message="No S3 raw key on model")
            return {"error": "no_s3_key", "model_id": model_id}

        with tempfile.TemporaryDirectory(prefix="ifc_") as tmpdir:
            ifc_local = os.path.join(tmpdir, "input.ifc")
            xkt_dir = os.path.join(tmpdir, "xkt")
            os.makedirs(xkt_dir, exist_ok=True)

            stage = "init"
            try:
                stage = "download"
                # ── 2. Download from S3 ──────────────────────────────────────
                download_raw_file(s3_raw_key, ifc_local)
                logger.info("[IFC] Download complete model_id=%s", model_id)
                publish_model_progress(user_id, model_id, 10, "download")

                stage = "size_check"
                # ── 2b. 500 MB file size guard ───────────────────────────────
                assert_file_size(ifc_local)

                stage = "parse"
                # ── 3. Open with IfcOpenShell ────────────────────────────────
                try:
                    import ifcopenshell  # type: ignore
                except ImportError:
                    raise RuntimeError(
                        "ifcopenshell is not installed. "
                        "Add it to worker dependencies: pip install ifcopenshell"
                    )

                logger.info("[IFC] Opening %s with ifcopenshell", ifc_local)
                ifc_file = ifcopenshell.open(ifc_local)


                # ── 4. Validate schema ───────────────────────────────────────
                stage = "validate"
                schema = ifc_file.schema  # e.g. "IFC2X3", "IFC4", "IFC4X3"
                logger.info("[IFC] Schema: %s", schema)
                if schema not in SUPPORTED_SCHEMAS:
                    raise ValueError(
                        f"Unsupported IFC schema '{schema}'. "
                        f"Supported: {', '.join(sorted(SUPPORTED_SCHEMAS))}"
                    )

                # ── 5. Extract geometry stats ────────────────────────────────
                publish_model_progress(user_id, model_id, 25, "parse")
                stage = "extract"
                geom_stats = _extract_geometry(ifc_file, ifc_local)

                # ── 6. Extract elements + property sets ──────────────────────
                elements = _extract_elements(ifc_file)

                # ── 7. Build spatial tree ────────────────────────────────────
                spatial_tree = _build_spatial_tree(ifc_file)
                logger.info(
                    "[IFC] Extraction complete model_id=%s elements=%d", model_id, len(elements)
                )
                publish_model_progress(user_id, model_id, 50, "extract")

                # ── 8. XKT conversion ────────────────────────────────────────
                stage = "convert"
                logger.info("[IFC] Starting XKT conversion model_id=%s", model_id)
                xkt_files = _convert_to_xkt(ifc_local, xkt_dir)
                xkt_chunks = _split_xkt_chunks(xkt_files, xkt_dir)
                publish_model_progress(user_id, model_id, 75, "convert")

                # ── 9. Upload XKT chunks to S3 ───────────────────────────────
                stage = "upload"
                processed_prefix = f"processed/{model_id}"
                uploaded_keys: list[str] = []

                for i, chunk_path in enumerate(xkt_chunks):
                    chunk_name = Path(chunk_path).name
                    s3_processed_key = f"{processed_prefix}/{chunk_name}"
                    upload_processed_file(chunk_path, s3_processed_key, content_type="application/octet-stream")
                    uploaded_keys.append(s3_processed_key)

                logger.info("[IFC] Uploaded %d XKT chunk(s)", len(uploaded_keys))
                publish_model_progress(user_id, model_id, 90, "upload")

                # ── 10. Persist elements to DB ───────────────────────────────
                stage = "metadata"
                _upsert_model_elements(engine, model_id, elements)

                # ── 11. Persist metadata ─────────────────────────────────────
                upsert_model_metadata(
                    engine,
                    model_id,
                    properties={
                        "source_format": "ifc",
                        "schema": schema,
                        "geometry": geom_stats,
                        "element_count": len(elements),
                        "chunk_count": len(uploaded_keys),
                        "xkt_chunks": uploaded_keys,
                    },
                    spatial_tree=spatial_tree,
                )

                # ── 12. Compute bounding box ──────────────────────────────────
                bounds_min, bounds_max = _compute_bounds(ifc_file)

                # ── 13. Update model status → ready ───────────────────────────
                stage = "finalize"
                update_model_status(
                    engine,
                    model_id,
                    "ready",
                    processed_s3_prefix=processed_prefix,
                    element_count=len(elements),
                    bounds_min_xyz=bounds_min,
                    bounds_max_xyz=bounds_max,
                )
                logger.info("[IFC] Model status set to ready model_id=%s", model_id)

                # ── 14. Publish Redis event ────────────────────────────────────
                chunk_urls = [build_cdn_url(k) for k in uploaded_keys]
                publish_model_ready(user_id, model_id, chunk_urls)
                publish_model_progress(user_id, model_id, 100, "ready")
                dispatch_webhook_event(engine, "model.ready", {"model_id": model_id}, user_id)
                release_task_lock(model_id, "app.tasks.ifc.process_model")

                logger.info("[IFC] Processing complete for model_id=%s", model_id)
                return {
                    "model_id": model_id,
                    "status": "ready",
                    "schema": schema,
                    "element_count": len(elements),
                    "chunks": len(uploaded_keys),
                    "geometry": geom_stats,
                }

            except SoftTimeLimitExceeded:
                logger.error("[IFC] Soft time limit exceeded at stage=%s", stage)
                release_task_lock(model_id, "app.tasks.ifc.process_model")
                return handle_task_failure(
                    engine, model_id, user_id, stage, SoftTimeLimitExceeded("soft time limit")
                )

            except Exception as exc:
                logger.exception("[IFC] Failed at stage=%s for model_id=%s", stage, model_id)
                result = handle_task_failure(engine, model_id, user_id, stage, exc)
                release_task_lock(model_id, "app.tasks.ifc.process_model")
                if is_retryable(exc):
                    raise self.retry(exc=exc)
                return result
    finally:
        ACTIVE_TASKS.labels(task_name="ifc.process_model").dec()
        elapsed = __import__('time').monotonic() - _task_start
        IFC_CONVERSION_DURATION.observe(elapsed)