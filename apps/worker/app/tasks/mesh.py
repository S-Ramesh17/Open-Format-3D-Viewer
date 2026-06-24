"""
Mesh queue router

Routes non-IFC formats to their dedicated processing tasks:
  - step / stp  → app.tasks.step.process_step
  - gltf / glb  → app.tasks.gltf.process_gltf
  - obj / stl   → reserved for future Day 3+ tasks

IFC files are handled exclusively by app.tasks.ifc.process_model.
"""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.tasks.common import get_model_row, get_sync_engine, update_model_status

logger = logging.getLogger(__name__)

# Map DB file_format values to concrete task names
_FORMAT_TO_TASK: dict[str, str] = {
    "step": "app.tasks.step.process_step",
    "stp":  "app.tasks.step.process_step",
    "gltf": "app.tasks.gltf.process_gltf",
    "glb":  "app.tasks.gltf.process_gltf",
    "obj":  "app.tasks.obj.process_obj",
    "stl":  "app.tasks.stl.process_stl",
}


@celery_app.task(
    name="app.tasks.mesh.generate_chunks",
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def generate_chunks(self, model_id: str) -> dict:
    """
    Entry-point for all non-IFC formats arriving on the 'mesh' queue.

    Reads file_format from DB and re-dispatches to the appropriate
    format-specific task on the same mesh queue.  Returns immediately
    after dispatch so the mesh worker slot is freed.
    """
    logger.info("[mesh] generate_chunks called for model_id=%s", model_id)

    engine = get_sync_engine()
    model = get_model_row(engine, model_id)

    if model is None:
        logger.error("[mesh] Model %s not found in DB — cannot route", model_id)
        return {"error": "model_not_found", "model_id": model_id}

    file_format = (model.get("file_format") or "").lower()
    task_name = _FORMAT_TO_TASK.get(file_format)

    if task_name is None:
        msg = f"No processor registered for format '{file_format}' (model_id={model_id})"
        logger.warning("[mesh] %s", msg)
        update_model_status(engine, model_id, "failed", error_message=msg)
        return {"model_id": model_id, "status": "failed", "error": msg}

    logger.info("[mesh] Routing model_id=%s (format=%s) → %s", model_id, file_format, task_name)
    celery_app.send_task(task_name, args=[model_id], queue="mesh")

    return {"model_id": model_id, "status": "dispatched", "routed_to": task_name}
