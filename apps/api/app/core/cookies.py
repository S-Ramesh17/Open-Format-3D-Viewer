from datetime import timedelta

from fastapi import Response

from app.config import settings
from app.core.csrf import clear_csrf_cookie, generate_csrf_token, set_csrf_cookie

ACCESS_TOKEN_COOKIE = "access_token"
REFRESH_TOKEN_COOKIE = "refresh_token"


def _is_secure() -> bool:
    """
    Secure flag must be True in production (HTTPS only).
    False in local development (HTTP on localhost).
    """
    return settings.ENVIRONMENT == "production"


def set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    """
    Set both auth cookies on the response, plus a fresh CSRF cookie for
    this new/rotated session (register, login, refresh, and the OAuth
    callback are the only places a session begins or is rotated — see
    app/core/csrf.py for why the CSRF cookie is tied to this same
    lifecycle rather than issued separately).
    access_token: available on all paths, expires in 1 hour.
    refresh_token: scoped to /v1/auth only, expires in 30 days.
    """
    access_max_age = settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    refresh_max_age = int(timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS).total_seconds())

    secure = _is_secure()
    samesite = "none" if secure else "lax"

    response.set_cookie(
        key=ACCESS_TOKEN_COOKIE,
        value=access_token,
        max_age=access_max_age,
        httponly=True,
        secure=secure,
        samesite=samesite,
        path="/",
    )
    response.set_cookie(
        key=REFRESH_TOKEN_COOKIE,
        value=refresh_token,
        max_age=refresh_max_age,
        httponly=True,
        secure=secure,
        samesite=samesite,
        path="/v1/auth",
    )
    set_csrf_cookie(response, generate_csrf_token())


def clear_auth_cookies(response: Response) -> None:
    """
    Clear both auth cookies, plus the CSRF cookie.
    Must mirror the same secure/samesite/path attributes used when setting,
    otherwise browsers ignore the deletion.
    """
    secure = _is_secure()
    samesite = "none" if secure else "lax"

    response.delete_cookie(
        key=ACCESS_TOKEN_COOKIE,
        path="/",
        httponly=True,
        secure=secure,
        samesite=samesite,
    )
    response.delete_cookie(
        key=REFRESH_TOKEN_COOKIE,
        path="/v1/auth",
        httponly=True,
        secure=secure,
        samesite=samesite,
    )
    clear_csrf_cookie(response)