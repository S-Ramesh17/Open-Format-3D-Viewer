"""
GLTF / GLB processing pipeline — Week 2 Day 2.

Flow:
  1.  Download .gltf / .glb from S3 raw bucket
  2.  Validate with gltf-validator (reject invalid assets)
  3.  Optimize with gltf-pipeline (Draco compression, level 7)
  4.  Split outputs larger than 32 MB
  5.  Upload processed chunks to S3 processed bucket
  6.  Persist metadata to model_metadata
  7.  Update model status → "ready"
  8.  Publish Redis event model_events:{user_id}
"""

from __future__ import annotations

import json
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
    upsert_model_metadata,
    upload_processed_file,
    build_cdn_url,
)
from app.tasks.error_handler import (
    assert_file_size,
    handle_task_failure,
    is_retryable,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class GltfValidationError(Exception):
    """Raised when gltf-validator reports errors that should fail the upload."""


def _validate_gltf(file_path: str) -> dict:
    """
    Run gltf-validator on the file. Returns the parsed validation report dict.

    If gltf-validator is not installed the file is accepted (warning logged).
    If the report contains errors (not warnings), GltfValidationError is raised.
    """
    cmd = [settings.GLTF_VALIDATOR_BIN, file_path, "--stdout"]

    try:
        stdout = run_node_tool(cmd, timeout=120, tool_name="gltf-validator")
    except FileNotFoundError:
        logger.warning(
            "[GLTF] gltf-validator not found ('%s') — skipping validation. "
            "See apps/worker/Dockerfile for the correct install (native CLI "
            "binary, NOT the npm 'gltf-validator' library package).",
            settings.GLTF_VALIDATOR_BIN,
        )
        return {}
    except RuntimeError as exc:
        # gltf-validator exits non-zero when it finds errors; capture report
        # from stderr/stdout when possible
        logger.warning("[GLTF] gltf-validator returned non-zero: %s", exc)
        raise GltfValidationError(f"GLTF validation failed: {exc}") from exc

    try:
        report = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        logger.warning("[GLTF] Could not parse gltf-validator JSON output; accepting file")
        return {}

    issues = report.get("issues", {})
    num_errors = issues.get("numErrors", 0)
    num_warnings = issues.get("numWarnings", 0)

    logger.info(
        "[GLTF] Validation report: errors=%d warnings=%d",
        num_errors,
        num_warnings,
    )

    if num_errors > 0:
        messages = issues.get("messages", [])
        error_msgs = [
            m.get("message", "") for m in messages if m.get("severity", 1) == 0
        ][:5]
        raise GltfValidationError(
            f"GLTF asset has {num_errors} error(s): {'; '.join(error_msgs)}"
        )

    return report


# ---------------------------------------------------------------------------
# Optimization
# ---------------------------------------------------------------------------

def _optimize_gltf(
    input_path: str,
    output_dir: str,
    compression_level: int = 7,
) -> str:
    """
    Optimize and Draco-compress a GLTF/GLB file using gltf-pipeline.

    The output is always .glb (binary GLTF) to reduce file count.
    Returns the path to the produced file.

    If gltf-pipeline is not found, logs a warning and returns the original
    path unmodified so the pipeline can still complete.
    """
    input_p = Path(input_path)
    output_path = Path(output_dir) / (input_p.stem + "_optimized.glb")

    cmd = [
        settings.GLTF_PIPELINE_BIN,
        "-i", input_path,
        "-o", str(output_path),
        "--draco.compressMeshes",
        "--draco.compressionLevel", str(compression_level),
    ]

    try:
        run_node_tool(cmd, timeout=600, tool_name="gltf-pipeline")
    except FileNotFoundError:
        logger.warning(
            "[GLTF] gltf-pipeline not found ('%s') — optimization skipped. "
            "Install: npm install -g gltf-pipeline",
            settings.GLTF_PIPELINE_BIN,
        )
        return input_path
    except RuntimeError as exc:
        # Optimization failure is non-fatal: upload original instead
        logger.error("[GLTF] gltf-pipeline optimization failed: %s — using original", exc)
        return input_path

    if not output_path.exists():
        logger.error("[GLTF] gltf-pipeline produced no output — using original")
        return input_path

    in_size = input_p.stat().st_size
    out_size = output_path.stat().st_size
    ratio = (1 - out_size / in_size) * 100 if in_size > 0 else 0
    logger.info(
        "[GLTF] Optimized: %.1f KB → %.1f KB (%.0f%% reduction)",
        in_size / 1024,
        out_size / 1024,
        ratio,
    )
    return str(output_path)


# ---------------------------------------------------------------------------
# Main Celery task
# ---------------------------------------------------------------------------

@celery_app.task(
    name="app.tasks.gltf.process_gltf",
    bind=True,
    time_limit=1800,
    soft_time_limit=1500,
    max_retries=2,
    default_retry_delay=60,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_gltf(self: Task, model_id: str) -> dict:
    """
    Full GLTF/GLB processing pipeline.
    Dispatched by the mesh queue for file_format in ('gltf', 'glb').
    """
    logger.info("[GLTF] Starting processing for model_id=%s", model_id)

    engine = get_sync_engine()

    # ── 1. Idempotency guard — skip if already terminal (redelivery) ──────
    if is_already_processed(engine, model_id):
        logger.info("[GLTF] model_id=%s already in terminal status — skipping redelivered task", model_id)
        return {"model_id": model_id, "skipped": "already_processed"}

    # ── 1b. Redis lock — prevent duplicate concurrent execution ──────────
    if not acquire_task_lock(model_id, "app.tasks.gltf.process_gltf"):
        return {"model_id": model_id, "status": "skipped", "reason": "duplicate_task"}

    # ── 1c. Fetch model row ─────────────────────────────────────────────────
    model = get_model_row(engine, model_id)
    if model is None:
        logger.error("[GLTF] Model %s not found in DB", model_id)
        release_task_lock(model_id, "app.tasks.gltf.process_gltf")
        return {"error": "model_not_found", "model_id": model_id}

    user_id = str(model["uploaded_by"])
    s3_raw_key = model["s3_raw_key"]

    if not s3_raw_key:
        update_model_status(engine, model_id, "failed", error_message="No S3 raw key on model")
        return {"error": "no_s3_key", "model_id": model_id}

    # ── Per-plan size limit ────────────────────────────────────────────────
    plan = model.get("plan")
    max_bytes = get_plan_max_bytes(plan)
    file_size_bytes = model.get("file_size_bytes") or 0
    if file_size_bytes > max_bytes:
        from app.tasks.error_handler import FileTooLargeError
        raise FileTooLargeError(
            f"File size {file_size_bytes / 1024 / 1024:.1f} MB exceeds "
            f"{plan or 'free'} plan limit of {max_bytes / 1024 / 1024:.0f} MB"
        )

    with tempfile.TemporaryDirectory(prefix="gltf_") as tmpdir:
        in_ext = Path(s3_raw_key).suffix.lower() or ".gltf"
        local_input = os.path.join(tmpdir, f"input{in_ext}")
        out_dir = os.path.join(tmpdir, "output")
        os.makedirs(out_dir, exist_ok=True)

        stage = "init"
        try:
            stage = "download"
            # ── 2. Download from S3 ────────────────────────────────────────
            download_raw_file(s3_raw_key, local_input)
            publish_model_progress(user_id, model_id, 10, "download")

            stage = "size_check"
            # ── 2b. Per-plan on-disk file size guard ──────────────────────
            assert_file_size(local_input, max_bytes=max_bytes)

            stage = "validate"
            # ── 3. Validate ────────────────────────────────────────────────
            validation_report = _validate_gltf(local_input)
            publish_model_progress(user_id, model_id, 25, "validate")

            stage = "compress"
            # ── 4. Optimize with Draco compression ─────────────────────────
            optimized_path = _optimize_gltf(local_input, out_dir, compression_level=7)
            publish_model_progress(user_id, model_id, 50, "optimize")

            stage = "split"
            # ── 5. Split if > 32 MB ────────────────────────────────────────
            out_ext = Path(optimized_path).suffix
            out_stem = Path(optimized_path).stem
            chunks = split_binary_chunks(optimized_path, out_dir, out_stem, out_ext)

            stage = "upload"
            # ── 6. Upload chunks to S3 ─────────────────────────────────────
            processed_prefix = f"processed/{model_id}"
            uploaded_keys: list[str] = []

            content_type = "model/gltf-binary" if out_ext == ".glb" else "model/gltf+json"
            for chunk_path in chunks:
                chunk_name = Path(chunk_path).name
                s3_key = f"{processed_prefix}/{chunk_name}"
                upload_processed_file(chunk_path, s3_key, content_type=content_type)
                uploaded_keys.append(s3_key)

            logger.info("[GLTF] Uploaded %d chunk(s)", len(uploaded_keys))

            stage = "metadata"
            # ── 7. Persist metadata ────────────────────────────────────────
            warnings = validation_report.get("issues", {}).get("numWarnings", 0) if validation_report else 0
            upsert_model_metadata(
                engine,
                model_id,
                properties={
                    "source_format": in_ext.lstrip("."),
                    "output_format": out_ext.lstrip("."),
                    "draco_compressed": optimized_path != local_input,
                    "draco_level": 7,
                    "validation_warnings": warnings,
                    "chunk_count": len(uploaded_keys),
                    "processed_keys": uploaded_keys,
                },
                spatial_tree={},
            )

            stage = "finalize"
            # ── 8. Update model status → ready ─────────────────────────────
            update_model_status(
                engine,
                model_id,
                "ready",
                processed_s3_prefix=processed_prefix,
            )

           # ── 9. Publish Redis event ─────────────────────────────────────
            chunk_urls = [build_cdn_url(k) for k in uploaded_keys]
            publish_model_ready(user_id, model_id, chunk_urls)
            dispatch_webhook_event(engine, "model.ready", {"model_id": model_id}, user_id)
            release_task_lock(model_id, "app.tasks.gltf.process_gltf")

            logger.info("[GLTF] Processing complete for model_id=%s", model_id)
            return {
                "model_id": model_id,
                "status": "ready",
                "source_format": in_ext.lstrip("."),
                "output_format": out_ext.lstrip("."),
                "chunks": len(uploaded_keys),
                "draco_compressed": optimized_path != local_input,
                "validation_warnings": warnings,
            }

        except GltfValidationError as exc:
            # Validation errors are permanent failures — do not retry
            logger.error("[GLTF] Validation failed for model_id=%s: %s", model_id, exc)
            release_task_lock(model_id, "app.tasks.gltf.process_gltf")
            return handle_task_failure(engine, model_id, user_id, "validate", exc)

        except SoftTimeLimitExceeded:
            logger.error("[GLTF] Soft time limit exceeded at stage=%s", stage)
            release_task_lock(model_id, "app.tasks.gltf.process_gltf")
            return handle_task_failure(
                engine, model_id, user_id, stage, SoftTimeLimitExceeded("soft time limit")
            )

        except Exception as exc:
            logger.exception("[GLTF] Failed at stage=%s for model_id=%s", stage, model_id)
            result = handle_task_failure(engine, model_id, user_id, stage, exc)
            release_task_lock(model_id, "app.tasks.gltf.process_gltf")
            if is_retryable(exc):
                raise self.retry(exc=exc)
            return result