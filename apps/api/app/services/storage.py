import re
import uuid

import boto3
import filetype
from botocore.config import Config as BotoConfig
from celery import Celery

from app.config import settings
from app.core.exceptions import StorageException, ValidationException

ALLOWED_EXTENSIONS = {".ifc", ".gltf", ".glb", ".step", ".stp", ".obj", ".stl"}

ALLOWED_MIME_TYPES = {
    "application/octet-stream",
    "model/gltf+json",
    "model/gltf-binary",
    "model/step",
    "model/obj",
    "application/sla",
    "text/plain",
}

_s3_client = None


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client(
            "s3",
            region_name=settings.AWS_REGION,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID or None,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY or None,
            config=BotoConfig(signature_version="s3v4"),
        )
    return _s3_client


def validate_filename(filename: str) -> str:
    if not filename or "\x00" in filename:
        raise ValidationException("Invalid filename")
    
    if ".." in filename or "/" in filename or "\\" in filename:
        raise ValidationException("Invalid filename — path traversal detected")

    safe_name = filename.replace("\\", "/").split("/")[-1]

    if safe_name in (".", "..") or not safe_name.strip():
        raise ValidationException("Invalid filename")

    if ".." in safe_name:
        raise ValidationException("Invalid filename — path traversal detected")

    if not re.match(r"^[\w\-. ]+$", safe_name):
        raise ValidationException("Filename contains invalid characters")

    ext = "." + safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationException(
            f"Unsupported file extension '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    return safe_name


def validate_file_size(size_bytes: int) -> None:
    if size_bytes <= 0:
        raise ValidationException("File size must be greater than 0")
    if size_bytes > settings.MAX_UPLOAD_SIZE_BYTES:
        max_mb = settings.MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)
        raise ValidationException(f"File exceeds maximum size of {max_mb}MB")


def validate_mime_type_declared(content_type: str) -> None:
    """
    Pre-upload check: validate the CLIENT-DECLARED content type before
    issuing a presigned URL. This is a cheap first gate — the authoritative
    check happens in validate_mime_type_from_bytes after upload, since a
    client can lie about Content-Type in the presigned PUT request.
    """
    if content_type not in ALLOWED_MIME_TYPES:
        raise ValidationException(
            f"Unsupported content type '{content_type}'. "
            f"Allowed: {', '.join(sorted(ALLOWED_MIME_TYPES))}"
        )


def validate_mime_type_from_bytes(header_bytes: bytes, declared_filename: str) -> str:
    """
    Authoritative MIME validation using filetype against actual file bytes
    (magic numbers), not client-supplied headers. Called during /confirm
    after downloading the first N bytes from S3.

    Returns the detected MIME type. Raises ValidationException if the
    detected type is implausible for the declared extension.

    Note: IFC/STEP/OBJ files are plain-text formats (SPFF / Wavefront).
    filetype returns None for these since they carry no binary magic bytes —
    we resolve that to application/octet-stream, which is the same result
    libmagic produces for these formats on many systems.

    Binary formats (GLB, binary STL) are detected via their magic bytes.
    """
    # filetype.guess() inspects magic bytes with no system library dependency.
    # Returns None when the byte signature is unrecognised (all text-based
    # 3D formats fall into this category).
    guess = filetype.guess(header_bytes)
    detected = guess.mime if guess is not None else "application/octet-stream"

    ext = "." + declared_filename.rsplit(".", 1)[-1].lower()

    # IFC, STEP, STP, OBJ are ASCII text formats — no binary magic bytes.
    # filetype correctly returns None → we use application/octet-stream.
    text_based_formats = {".ifc", ".step", ".stp", ".obj"}

    if ext in text_based_formats:
        if detected not in ("text/plain", "application/octet-stream"):
            raise ValidationException(
                f"File content does not match declared format '{ext}' "
                f"(detected: {detected})"
            )
    else:
        if detected not in ALLOWED_MIME_TYPES and not detected.startswith("model/"):
            raise ValidationException(
                f"File content does not match declared format '{ext}' "
                f"(detected: {detected})"
            )

    return detected


def build_storage_key(user_id: uuid.UUID, model_id: uuid.UUID, filename: str) -> str:
    return f"{user_id}/{model_id}/{filename}"


def generate_presigned_upload_url(
    storage_key: str,
    content_type: str,
    size_bytes: int,
    expires_in: int = 600,
) -> str:
    client = _get_s3_client()
    try:
        url = client.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": settings.S3_RAW_BUCKET,
                "Key": storage_key,
                "ContentType": content_type,
            },
            ExpiresIn=expires_in,
        )
    except Exception as exc:
        raise StorageException(f"Failed to generate upload URL: {exc}")

    return url


def verify_object_exists(storage_key: str) -> dict:
    client = _get_s3_client()
    try:
        response = client.head_object(Bucket=settings.S3_RAW_BUCKET, Key=storage_key)
    except client.exceptions.ClientError as exc:
        raise StorageException(f"Upload not found in storage: {exc}")
    except Exception as exc:
        raise StorageException(f"Failed to verify upload: {exc}")

    return {
        "size_bytes": response.get("ContentLength", 0),
        "content_type": response.get("ContentType", ""),
    }


def fetch_object_header_bytes(storage_key: str, num_bytes: int = 2048) -> bytes:
    """Fetch the first N bytes of an S3 object for MIME sniffing without full download."""
    client = _get_s3_client()
    try:
        response = client.get_object(
            Bucket=settings.S3_RAW_BUCKET,
            Key=storage_key,
            Range=f"bytes=0-{num_bytes - 1}",
        )
        return response["Body"].read()
    except Exception as exc:
        raise StorageException(f"Failed to read uploaded file: {exc}")


def trigger_clamav_scan(model_id: str, storage_key: str) -> None:
    """
    Enqueues the Celery ClamAV scan task.
    Real clamd streaming scan is implemented in apps/worker/app/tasks/scan.py.
    This function only dispatches; it does not block the confirm request.

    Task signature: scan_file(model_id: str, s3_key: str)
    """

    celery_client = Celery(broker=settings.REDIS_URL)
    celery_client.send_task(
        "app.tasks.scan.scan_file",
        args=[model_id, storage_key],
        queue="scan",
    )


def build_cdn_url(processed_key: str) -> str:
    base = settings.CDN_BASE_URL.rstrip("/")
    return f"{base}/{processed_key}"