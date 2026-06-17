import uuid
from contextvars import ContextVar

# ContextVar is isolated per async task — safe for concurrent requests
_request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")


def set_request_id(request_id: str) -> None:
    _request_id_ctx.set(request_id)


def get_request_id() -> str:
    val = _request_id_ctx.get()
    if not val:
        return str(uuid.uuid4())
    return val


def generate_request_id() -> str:
    return str(uuid.uuid4())