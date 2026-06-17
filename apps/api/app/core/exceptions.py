from typing import Any


class AppException(Exception):
    """
    Base exception for all application errors.
    All custom exceptions inherit from this.
    """
    status_code: int = 500
    error_code: str = "INTERNAL_ERROR"
    message: str = "An unexpected error occurred"

    def __init__(
        self,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        self.message = message or self.__class__.message
        self.details = details or {}
        super().__init__(self.message)


class ValidationException(AppException):
    """Invalid request data — field constraints, format errors."""
    status_code = 422
    error_code = "VALIDATION_ERROR"
    message = "Request validation failed"


class AuthenticationException(AppException):
    """Missing, invalid, or expired credentials."""
    status_code = 401
    error_code = "AUTHENTICATION_ERROR"
    message = "Authentication required"


class AuthorizationException(AppException):
    """Authenticated but not permitted to perform this action."""
    status_code = 403
    error_code = "AUTHORIZATION_ERROR"
    message = "You do not have permission to perform this action"


class NotFoundException(AppException):
    """Requested resource does not exist."""
    status_code = 404
    error_code = "NOT_FOUND"
    message = "Resource not found"


class ConflictException(AppException):
    """Resource already exists or state conflict."""
    status_code = 409
    error_code = "CONFLICT"
    message = "Resource already exists"


class RateLimitException(AppException):
    """Too many requests — rate limit exceeded."""
    status_code = 429
    error_code = "RATE_LIMIT_EXCEEDED"
    message = "Rate limit exceeded"


class StorageException(AppException):
    """External storage failure (S3, file system)."""
    status_code = 502
    error_code = "STORAGE_ERROR"
    message = "Storage service unavailable"


class ProcessingException(AppException):
    """Model conversion or background processing failed."""
    status_code = 422
    error_code = "PROCESSING_ERROR"
    message = "Processing failed"


class InternalException(AppException):
    """Catch-all for unexpected server errors."""
    status_code = 500
    error_code = "INTERNAL_ERROR"
    message = "An internal server error occurred"