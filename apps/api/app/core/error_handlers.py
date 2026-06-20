from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.exceptions import AppException
from app.core.request_id import get_request_id


def _error_response(
    status_code: int,
    error_code: str,
    message: str,
    details: dict | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": error_code,
                "message": message,
                "details": details or {},
            },
            "meta": {
                "request_id": get_request_id(),
            },
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register all exception handlers on the FastAPI app."""

    @app.exception_handler(AppException)
    async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
        """Handle all custom AppException subclasses."""
        return _error_response(
            status_code=exc.status_code,
            error_code=exc.error_code,
            message=exc.message,
            details=exc.details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """
        Handle FastAPI/Starlette HTTP exceptions (404, 405, etc).
        Maps common status codes to our error codes.
        """
        error_code_map = {
            400: "BAD_REQUEST",
            401: "AUTHENTICATION_ERROR",
            403: "AUTHORIZATION_ERROR",
            404: "NOT_FOUND",
            405: "METHOD_NOT_ALLOWED",
            409: "CONFLICT",
            422: "VALIDATION_ERROR",
            429: "RATE_LIMIT_EXCEEDED",
            500: "INTERNAL_ERROR",
            502: "STORAGE_ERROR",
        }
        error_code = error_code_map.get(exc.status_code, "HTTP_ERROR")
        return _error_response(
            status_code=exc.status_code,
            error_code=error_code,
            message=str(exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """
        Handle Pydantic validation errors on request bodies.
        Formats field errors into a readable structure.
        """
        field_errors = {}
        for error in exc.errors():
            location = " → ".join(str(loc) for loc in error["loc"] if loc != "body")
            field_errors[location] = error["msg"]

        return _error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            error_code="VALIDATION_ERROR",
            message="Request validation failed",
            details={"fields": field_errors},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """
        Catch-all for any unhandled exception.
        Never leaks internal details to the client.
        """
        return _error_response(
            status_code=500,
            error_code="INTERNAL_ERROR",
            message="An internal server error occurred",
        )