"""
CSRF protection via the Double Submit Cookie pattern.

Why this pattern: the API is cookie-authenticated (httpOnly access/refresh
token cookies), which means a browser will automatically attach those
cookies to a cross-site request forged by a malicious page. The Double
Submit Cookie pattern defeats this without any server-side session store:
a random token is set as a *non-httponly* cookie (so client-side JS can
read it) and the client must echo that same value back in a custom
request header. A cross-site attacker can trigger the browser into
*sending* the cookie automatically, but cannot *read* it (browsers do not
allow cross-origin JS to read another site's cookies), so it cannot
construct a matching header value — this is exactly what defeats the
forgery.

Bearer-token clients (JWT in Authorization header, or `ofv_...` API keys)
never touch this — they aren't relying on the browser's automatic cookie
attachment, so CSRF does not apply to them at all.
"""
import secrets

from fastapi import Response

from app.config import settings

CSRF_COOKIE = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Paths that read a cookie directly for a real state-changing action but
# are excluded from AuthMiddleware's normal access_token-cookie gate
# (they authenticate off the *refresh* token cookie instead, before an
# access token even exists). CSRF must still apply to these.
_REFRESH_COOKIE_PATHS = frozenset({"/v1/auth/refresh", "/v1/auth/logout"})


def _is_secure() -> bool:
    return settings.ENVIRONMENT == "production"


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, token: str) -> None:
    """
    Set the CSRF cookie. Deliberately NOT httponly — client-side JS must
    be able to read this value to echo it back in the X-CSRF-Token header.
    Path="/" (unlike the refresh-token cookie) because this token has to
    be presented for state-changing requests across the whole API, not
    just under /v1/auth.
    """
    secure = _is_secure()
    samesite = "none" if secure else "lax"
    response.set_cookie(
        key=CSRF_COOKIE,
        value=token,
        httponly=False,
        secure=secure,
        samesite=samesite,
        path="/",
    )


def clear_csrf_cookie(response: Response) -> None:
    secure = _is_secure()
    samesite = "none" if secure else "lax"
    response.delete_cookie(
        key=CSRF_COOKIE,
        path="/",
        httponly=False,
        secure=secure,
        samesite=samesite,
    )


def requires_csrf_check(request) -> bool:
    """
    Decide whether this request must pass CSRF validation.

    Skips: safe methods, and any request authenticated via a Bearer
    header (JWT or API key) — those never rely on the browser's implicit
    cookie attachment, so forgery via a third-party site isn't possible
    for them.

    Applies to: any request whose method is not safe AND that is either
    (a) authenticated via the access_token cookie (flagged by
    AuthMiddleware as request.state.auth_via_cookie), or (b) hitting
    /v1/auth/refresh or /v1/auth/logout while carrying the refresh_token
    cookie — both bypass AuthMiddleware's normal gate but still consume a
    cookie to perform a real, state-changing action.
    """
    if request.method in SAFE_METHODS:
        return False

    if request.headers.get("Authorization", "").startswith("Bearer "):
        return False

    if getattr(request.state, "auth_via_cookie", False):
        return True

    if request.url.path in _REFRESH_COOKIE_PATHS and "refresh_token" in request.cookies:
        return True

    return False


def validate_csrf(request) -> bool:
    cookie_token = request.cookies.get(CSRF_COOKIE)
    header_token = request.headers.get(CSRF_HEADER)
    if not cookie_token or not header_token:
        return False
    return secrets.compare_digest(cookie_token, header_token)