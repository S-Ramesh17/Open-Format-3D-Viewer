from typing import Any

from fastapi.responses import JSONResponse

from app.core.request_id import get_request_id


def envelope(data: Any, status_code: int = 200, meta_extra: dict | None = None) -> JSONResponse:
    """
    Wrap any payload in the standard API response envelope.

    Usage in routes:
        return envelope({"user": user})
        return envelope(token_response.model_dump(), status_code=201)
    """
    meta = {"request_id": get_request_id()}
    if meta_extra:
        meta.update(meta_extra)
        
    content = {
        "data": data,
        "meta": meta,
    }
    return JSONResponse(content=content, status_code=status_code)


def envelope_model(model: Any, status_code: int = 200) -> JSONResponse:
    """
    Wrap a Pydantic model instance in the envelope.
    Calls .model_dump() with mode="json" to handle UUID, datetime serialization.
    """
    return envelope(model.model_dump(mode="json"), status_code=status_code)