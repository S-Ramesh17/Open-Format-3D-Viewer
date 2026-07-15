from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.csrf import requires_csrf_check, validate_csrf
from app.core.request_id import get_request_id


class CSRFMiddleware(BaseHTTPMiddleware):
    """
    Enforces the Double Submit Cookie CSRF pattern for cookie-authenticated,
    state-changing requests only.

    Must run AFTER AuthMiddleware has already resolved
    request.state.auth_via_cookie (see main.py's middleware registration
    order/comment) — that's what lets this middleware tell a cookie-backed
    browser request apart from a Bearer-token API request without
    re-implementing any auth logic here.

    Bearer-token clients (JWT or API key), safe methods (GET/HEAD/OPTIONS),
    and any request with no cookie-based session in play (e.g. register,
    login) pass through untouched — see requires_csrf_check() for the
    exact rule.
    """

    async def dispatch(self, request: Request, call_next):
        if not requires_csrf_check(request):
            return await call_next(request)

        if not validate_csrf(request):
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "CSRF_VALIDATION_FAILED",
                        "message": "Missing or invalid CSRF token",
                        "details": {},
                    },
                    "meta": {"request_id": get_request_id()},
                },
            )

        return await call_next(request)