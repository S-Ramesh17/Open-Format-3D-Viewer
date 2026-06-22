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

import io
import json
import logging
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from celery import Task
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SUPPORTED_SCHEMAS = {"IFC2X3", "IFC4", "IFC4X3"}

# ---------------------------------------------------------------------------
# Helpers — S3
# ---------------------------------------------------------------------------

def _s3_client():
    return boto3.client(
        "s3",
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID or None,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY or None,
        config=BotoConfig(signature_version="s3v4"),
    )


def _download_ifc(s3_key: str, dest_path: str) -> None:
    """Stream IFC file from S3 raw bucket to local disk."""
    s3 = _s3_client()
    logger.info("Downloading s3://%s/%s → %s", settings.S3_RAW_BUCKET, s3_key, dest_path)
    s3.download_file(settings.S3_RAW_BUCKET, s3_key, dest_path)


def _upload_xkt_chunk(local_path: str, s3_key: str) -> None:
    """Upload a single XKT chunk to the processed bucket."""
    s3 = _s3_client()
    logger.info("Uploading XKT chunk → s3://%s/%s", settings.S3_PROCESSED_BUCKET, s3_key)
    s3.upload_file(
        local_path,
        settings.S3_PROCESSED_BUCKET,
        s3_key,
        ExtraArgs={"ContentType": "application/octet-stream"},
    )


# ---------------------------------------------------------------------------
# Helpers — DB (sync SQLAlchemy — Celery runs sync)
# ---------------------------------------------------------------------------

def _get_sync_engine():
    """Create a synchronous SQLAlchemy engine for use inside Celery tasks."""
    url = settings.DATABASE_URL
    # If the URL uses asyncpg driver, swap to psycopg2 for sync access
    url = url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    url = url.replace("postgresql+aiopg://", "postgresql+psycopg2://")
    return create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=0)


def _get_model_row(engine, model_id: str) -> dict | None:
    """Fetch model row as dict. Returns None if not found."""
    with engine.connect() as conn:
        row = conn.execute(
            _raw_sql(
                "SELECT id, uploaded_by, s3_raw_key, s3_processed_prefix, status "
                "FROM models WHERE id = :mid",
            ),
            {"mid": model_id},
        ).fetchone()
    if row is None:
        return None
    return dict(row._mapping)


def _raw_sql(sql: str):
    from sqlalchemy import text
    return text(sql)


def _update_model_status(
    engine,
    model_id: str,
    status: str,
    s3_processed_prefix: str | None = None,
    error_message: str | None = None,
) -> None:
    set_clauses = "status = :status, updated_at = now()"
    params: dict = {"status": status, "model_id": model_id}
    if s3_processed_prefix is not None:
        set_clauses += ", s3_processed_prefix = :prefix"
        params["prefix"] = s3_processed_prefix
    if error_message is not None:
        set_clauses += ", error_message = :err"
        params["err"] = error_message[:2000]
    with engine.begin() as conn:
        conn.execute(_raw_sql(f"UPDATE models SET {set_clauses} WHERE id = :model_id"), params)


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
                    "VALUES (:id, :model_id, :guid, :element_type, :name, :properties::jsonb, now())"
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


def _upsert_model_metadata(engine, model_id: str, properties: dict, spatial_tree: dict) -> None:
    with engine.begin() as conn:
        existing = conn.execute(
            _raw_sql("SELECT id FROM model_metadata WHERE model_id = :mid"),
            {"mid": model_id},
        ).fetchone()
        if existing:
            conn.execute(
                _raw_sql(
                    "UPDATE model_metadata SET properties = :props::jsonb, "
                    "spatial_tree = :tree::jsonb, updated_at = now() "
                    "WHERE model_id = :mid"
                ),
                {"props": json.dumps(properties), "tree": json.dumps(spatial_tree), "mid": model_id},
            )
        else:
            conn.execute(
                _raw_sql(
                    "INSERT INTO model_metadata (id, model_id, properties, spatial_tree, created_at, updated_at) "
                    "VALUES (:id, :mid, :props::jsonb, :tree::jsonb, now(), now())"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "mid": model_id,
                    "props": json.dumps(properties),
                    "tree": json.dumps(spatial_tree),
                },
            )


# ---------------------------------------------------------------------------
# Helpers — Redis publish (sync)
# ---------------------------------------------------------------------------

def _publish_model_ready(user_id: str, model_id: str) -> None:
    """Publish model_ready event to Redis channel model_events:{user_id}."""
    import redis as redis_lib

    channel = f"model_events:{user_id}"
    payload = json.dumps(
        {"event": "model:ready", "data": {"model_id": model_id, "status": "ready"}}
    )
    try:
        r = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
        r.publish(channel, payload)
        r.close()
        logger.info("Published model:ready → %s", channel)
    except Exception as exc:  # pragma: no cover
        logger.error("Failed to publish Redis event: %s", exc)


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


# ---------------------------------------------------------------------------
# XKT conversion via Node subprocess
# ---------------------------------------------------------------------------

def _convert_to_xkt(ifc_path: str, output_dir: str) -> list[str]:
    """
    Call @xeokit/xeokit-convert (Node.js) to produce XKT files in output_dir.
    Returns list of produced .xkt file paths.

    The convert CLI is expected to be available as the binary named in
    settings.XEOKIT_CONVERT_BIN, or as a node script at a well-known path.
    Falls back gracefully if unavailable so the rest of the pipeline still runs.
    """
    xkt_out = os.path.join(output_dir, "model.xkt")

    # Determine how to invoke the converter
    convert_bin = settings.XEOKIT_CONVERT_BIN
    cmd: list[str] = []

    if convert_bin.endswith(".js"):
        cmd = ["node", convert_bin, "-s", ifc_path, "-o", xkt_out]
    else:
        cmd = [convert_bin, "-s", ifc_path, "-o", xkt_out]

    logger.info("Running XKT conversion: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode != 0:
            logger.error("xeokit-convert stderr: %s", result.stderr)
            raise RuntimeError(
                f"xeokit-convert exited with code {result.returncode}: {result.stderr[:500]}"
            )
        logger.info("xeokit-convert stdout: %s", result.stdout[:300])
    except FileNotFoundError:
        logger.warning(
            "xeokit-convert binary '%s' not found — XKT output skipped. "
            "Install @xeokit/xeokit-convert or set XEOKIT_CONVERT_BIN.",
            convert_bin,
        )
        return []
    except subprocess.TimeoutExpired:
        raise RuntimeError("xeokit-convert timed out after 600s")

    # Collect all .xkt files produced (converter may produce one or split into chunks)
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
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_model(self: Task, model_id: str) -> dict:
    """
    Full IFC processing pipeline.
    Called by the API layer after upload confirmation.
    """
    logger.info("[IFC] Starting processing for model_id=%s", model_id)

    engine = _get_sync_engine()

    # ── 1. Fetch model row ────────────────────────────────────────────────
    model = _get_model_row(engine, model_id)
    if model is None:
        logger.error("[IFC] Model %s not found in DB", model_id)
        return {"error": "model_not_found", "model_id": model_id}

    user_id = str(model["uploaded_by"])
    s3_raw_key = model["s3_raw_key"]

    if not s3_raw_key:
        _update_model_status(engine, model_id, "failed", error_message="No S3 raw key on model")
        return {"error": "no_s3_key", "model_id": model_id}

    with tempfile.TemporaryDirectory(prefix="ifc_") as tmpdir:
        ifc_local = os.path.join(tmpdir, "input.ifc")
        xkt_dir = os.path.join(tmpdir, "xkt")
        os.makedirs(xkt_dir, exist_ok=True)

        try:
            # ── 2. Download from S3 ──────────────────────────────────────
            _download_ifc(s3_raw_key, ifc_local)

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
            schema = ifc_file.schema  # e.g. "IFC2X3", "IFC4", "IFC4X3"
            logger.info("[IFC] Schema: %s", schema)
            if schema not in SUPPORTED_SCHEMAS:
                raise ValueError(
                    f"Unsupported IFC schema '{schema}'. "
                    f"Supported: {', '.join(sorted(SUPPORTED_SCHEMAS))}"
                )

            # ── 5. Extract geometry stats ────────────────────────────────
            geom_stats = _extract_geometry(ifc_file, ifc_local)

            # ── 6. Extract elements + property sets ──────────────────────
            elements = _extract_elements(ifc_file)

            # ── 7. Build spatial tree ────────────────────────────────────
            spatial_tree = _build_spatial_tree(ifc_file)

            # ── 8. XKT conversion ────────────────────────────────────────
            xkt_files = _convert_to_xkt(ifc_local, xkt_dir)
            xkt_chunks = _split_xkt_chunks(xkt_files, xkt_dir)

            # ── 9. Upload XKT chunks to S3 ───────────────────────────────
            processed_prefix = f"processed/{model_id}"
            uploaded_keys: list[str] = []

            for i, chunk_path in enumerate(xkt_chunks):
                chunk_name = Path(chunk_path).name
                s3_processed_key = f"{processed_prefix}/{chunk_name}"
                _upload_xkt_chunk(chunk_path, s3_processed_key)
                uploaded_keys.append(s3_processed_key)

            logger.info("[IFC] Uploaded %d XKT chunk(s)", len(uploaded_keys))

            # ── 10. Persist elements to DB ───────────────────────────────
            _upsert_model_elements(engine, model_id, elements)

            # ── 11. Persist metadata + spatial tree to DB ─────────────────
            _upsert_model_metadata(
                engine,
                model_id,
                properties={
                    "schema": schema,
                    "element_count": len(elements),
                    "geometry": geom_stats,
                    "xkt_chunks": uploaded_keys,
                },
                spatial_tree=spatial_tree,
            )

            # ── 12. Update model status → ready ──────────────────────────
            _update_model_status(
                engine,
                model_id,
                "ready",
                s3_processed_prefix=processed_prefix,
            )

            # ── 13. Publish Redis event ───────────────────────────────────
            _publish_model_ready(user_id, model_id)

            logger.info("[IFC] Processing complete for model_id=%s", model_id)
            return {
                "model_id": model_id,
                "status": "ready",
                "schema": schema,
                "element_count": len(elements),
                "xkt_chunks": len(xkt_chunks),
            }

        except Exception as exc:
            logger.exception("[IFC] Processing failed for model_id=%s: %s", model_id, exc)
            _update_model_status(engine, model_id, "failed", error_message=str(exc))

            # Publish failure event so ws-server can relay it to the client
            try:
                import redis as redis_lib
                r = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
                r.publish(
                    f"model_events:{user_id}",
                    json.dumps({
                        "event": "model:failed",
                        "data": {"model_id": model_id, "error": str(exc)[:500]},
                    }),
                )
                r.close()
            except Exception:
                pass

            # Celery retry with exponential backoff for transient errors
            if isinstance(exc, (ConnectionError, OSError)):
                raise self.retry(exc=exc)

            return {"model_id": model_id, "status": "failed", "error": str(exc)[:500]}