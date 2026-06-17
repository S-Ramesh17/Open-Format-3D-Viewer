from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.request_id import generate_request_id, set_request_id

REQUEST_ID_HEADER = "X-Request-ID"


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Generates a unique request ID for every incoming request.
    - Reads X-Request-ID from client if provided (useful for tracing)
    - Generates a new UUID if not provided
    - Sets the ID in ContextVar for use throughout the request lifecycle
    - Adds X-Request-ID to the response headers
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # Accept client-provided ID or generate a new one
        request_id = request.headers.get(REQUEST_ID_HEADER) or generate_request_id()

        # Store in context var — accessible anywhere in this request's call stack
        set_request_id(request_id)

        # Process request
        response = await call_next(request)

        # Always echo the request ID back in the response
        response.headers[REQUEST_ID_HEADER] = request_id

        return response