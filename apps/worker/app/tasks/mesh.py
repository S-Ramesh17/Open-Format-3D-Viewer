from app.celery_app import celery_app

import logging

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.mesh.generate_chunks")
def generate_chunks(model_id: str) -> dict:
    """
    Mesh/XKT chunk generation for non-IFC formats (GLTF, GLB, OBJ, STL, STEP).
    Week 2 Day 2+ scope — routes non-IFC formats here for future implementation.
    IFC files are handled by app.tasks.ifc.process_model.
    """
    logger.info("mesh.generate_chunks received model_id=%s (stub — Week 2 Day 2+)", model_id)
    return {"model_id": model_id, "status": "queued", "queue": "mesh"}