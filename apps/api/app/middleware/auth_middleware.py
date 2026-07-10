from fastapi import Request
from jose import JWTError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.cookies import ACCESS_TOKEN_COOKIE
from app.core.request_id import get_request_id
from app.core.security import decode_token

EXCLUDED_PREFIXES = (
    "/v1/auth/",
)
# Routes that are entirely public (no auth even for GET)
EXCLUDED_EXACT_PREFIXES = (
    "/v1/share/",  # GET /v1/share/{token} is public; other methods still require auth below
)

def _is_excluded(path: str, method: str) -> bool:
    if not path.startswith("/v1/"):
        return True  # non-v1 routes (e.g. /health) untouched by this middleware

    if any(path.startswith(p) for p in EXCLUDED_PREFIXES):
        return True

    import re as _re
    if _re.match(r'^/v1/share/[^/]+$', path) and path != '/v1/share':
        # Only GET is public; POST/DELETE (revoke, etc.) still require auth.
        return method == "GET"

    return False


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Global authentication enforcement for all /v1/* routes.
    Excludes /v1/auth/* and any path ending in /public.
    Injects resolved user_id into request.state.user_id for downstream use.
    Does NOT replace get_current_user dependency — routes still use it to
    fetch the full User object from DB. This middleware is the hard gate;
    the dependency is the data-loading convenience layer.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if _is_excluded(path, request.method):
            return await call_next(request)

        token = None
        is_api_key = False

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            is_api_key = token.startswith("ofv_")
        else:
            token = request.cookies.get(ACCESS_TOKEN_COOKIE)

        if not token:
            return _unauthorized("Authentication required")

        if is_api_key:
            # API key validation requires DB access; defer full validation
            # to get_current_user dependency. Middleware only confirms
            # presence of a credential to block fully anonymous requests.
            request.state.auth_token = token
            request.state.auth_is_api_key = True
            return await call_next(request)

        try:
            payload = decode_token(token)
        except JWTError:
            return _unauthorized("Invalid or expired token")

        if payload.get("type") != "access":
            return _unauthorized("Invalid token type")

        user_id = payload.get("sub")
        if not user_id:
            return _unauthorized("Invalid token subject")

        request.state.user_id = user_id
        request.state.auth_token = token
        request.state.auth_is_api_key = False

        return await call_next(request)


def _unauthorized(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "error": {
                "code": "UNAUTHORIZED",
                "message": message,
                "details": {},
            },
            "meta": {
                "request_id": get_request_id(),
            },
        },
        headers={"WWW-Authenticate": "Bearer"},
    )