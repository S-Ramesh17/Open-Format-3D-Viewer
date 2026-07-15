# apps/api/app/services/storage.py
import os
import re
import uuid

import boto3
import magic
from botocore.config import Config as BotoConfig
from app.core.celery_client import get_celery_client

from app.config import settings
from app.core.exceptions import FileTooLargeException, StorageException, ValidationException

ALLOWED_EXTENSIONS = {".ifc", ".gltf", ".glb", ".step", ".stp", ".obj", ".stl"}

ALLOWED_MIME_TYPES = {
    "application/octet-stream",
    "application/json",  # .gltf (non-binary glTF) is JSON text — see below
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


# ---------------------------------------------------------------------------
# TEMP LOCAL STORAGE — helpers for local filesystem provider
# REMOVE AFTER S3 CREDENTIALS AVAILABLE
# ---------------------------------------------------------------------------

def _local_raw_path(storage_key: str) -> str:
    """Return the absolute local path for a raw upload key."""
    return os.path.join(settings.LOCAL_STORAGE_PATH, "raw", storage_key)


def _local_processed_path(storage_key: str) -> str:
    """Return the absolute local path for a processed output key."""
    return os.path.join(settings.LOCAL_STORAGE_PATH, "processed", storage_key)


def _ensure_local_dir(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)

# END TEMP LOCAL STORAGE
# ---------------------------------------------------------------------------


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

    # Reject filenames with no stem (e.g. ".ifc" — just an extension)
    if safe_name.startswith("."):
        raise ValidationException("Invalid filename — must have a non-empty stem")

    # Allow word chars, hyphens, dots, underscores, spaces, and parentheses.
    # Parentheses are common in CAD exports: "Tower (Level 1).ifc"
    if not re.match(r"^[\w\-. ()]+$", safe_name):
        raise ValidationException("Filename contains invalid characters")

    ext = "." + safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationException(
            f"Unsupported file extension '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    return safe_name


PLAN_MAX_UPLOAD_BYTES = {
    "free": 50 * 1024 * 1024,
    "pro": 500 * 1024 * 1024,
    "enterprise": 5 * 1024 * 1024 * 1024,
}

def validate_file_size(size_bytes: int, plan: str = "free") -> None:
    if size_bytes <= 0:
        raise ValidationException("File size must be greater than 0")
    max_bytes = PLAN_MAX_UPLOAD_BYTES.get(plan, PLAN_MAX_UPLOAD_BYTES["free"])
    if size_bytes > max_bytes:
        max_mb = max_bytes // (1024 * 1024)
        raise FileTooLargeException(f"File exceeds the {plan} plan limit of {max_mb}MB")


def validate_mime_type_declared(content_type: str) -> None:
    if content_type not in ALLOWED_MIME_TYPES:
        raise ValidationException(
            f"Unsupported content type '{content_type}'. "
            f"Allowed: {', '.join(sorted(ALLOWED_MIME_TYPES))}"
        )


def validate_mime_type_from_bytes(header_bytes: bytes, declared_filename: str) -> str:
    detected = magic.from_buffer(header_bytes, mime=True) if header_bytes else "application/octet-stream"

    ext = "." + declared_filename.rsplit(".", 1)[-1].lower()
    text_based_formats = {".ifc", ".step", ".stp", ".obj"}

    if ext in text_based_formats:
        # IFC/STEP/OBJ are plain-text formats and cannot be reliably
        # fingerprinted from magic bytes — unchanged from before.
        if detected not in ("text/plain", "application/octet-stream"):
            raise ValidationException(
                f"File content does not match declared format '{ext}' "
                f"(detected: {detected})"
            )
    elif ext == ".gltf":
        # .gltf (non-binary glTF) is a JSON document. python-magic
        # correctly reports this as application/json — the previous
        # filetype.guess() could not detect JSON at all (it only matches
        # binary signatures) and always fell back to
        # application/octet-stream, for valid AND invalid .gltf files
        # alike. A genuine .gltf file should never produce
        # application/octet-stream, so — unlike the text-based formats
        # above — that fallback is intentionally NOT accepted here; doing
        # so is what makes rejecting a random-bytes file renamed to
        # .gltf actually possible.
        if detected not in ("application/json", "text/plain"):
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
) -> dict:
    """
    Generate a presigned S3 upload URL. In local mode returns a ``local://``
    sentinel instead.

    Returns a JSON-serialisable dict, always shaped the same way regardless
    of storage provider:
      {"url": str, "fields": dict}
    ``fields`` is always {} — the client issues a single ``PUT`` request to
    ``url`` with the file body and matching ``Content-Type`` header (S3 mode),
    or uses the local-mode sentinel URL as-is.
    """
    # TEMP LOCAL STORAGE — return a local upload URL instead of S3 presigned URL
    if settings.STORAGE_PROVIDER == "local":
        local_path = _local_raw_path(storage_key)
        _ensure_local_dir(local_path)
        return {"url": f"local://{storage_key}", "fields": {}}
    # END TEMP LOCAL STORAGE

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

    return {"url": url, "fields": {}}

def verify_object_exists(storage_key: str) -> dict:
    # TEMP LOCAL STORAGE
    if settings.STORAGE_PROVIDER == "local":
        local_path = _local_raw_path(storage_key)
        if not os.path.exists(local_path):
            raise StorageException(
                f"Upload not found in local storage: {local_path}. "
                "In local mode, copy your file to this path before calling /confirm."
            )
        size = os.path.getsize(local_path)
        return {"size_bytes": size, "content_type": "application/octet-stream"}
    # END TEMP LOCAL STORAGE

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
    # TEMP LOCAL STORAGE
    if settings.STORAGE_PROVIDER == "local":
        local_path = _local_raw_path(storage_key)
        try:
            with open(local_path, "rb") as f:
                return f.read(num_bytes)
        except OSError as exc:
            raise StorageException(f"Failed to read local file: {exc}")
    # END TEMP LOCAL STORAGE

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
    Task signature: scan_file(model_id: str, s3_key: str)
    """
    celery_client = get_celery_client()
    celery_client.send_task(
        "app.tasks.scan.scan_file",
        args=[model_id, storage_key],
        queue="scan",
    )


def build_cdn_url(processed_key: str) -> str:
    # TEMP LOCAL STORAGE — serve processed files via API /files/ route
    if settings.STORAGE_PROVIDER == "local":
        return f"/files/{processed_key}"
    # END TEMP LOCAL STORAGE

    base = settings.CDN_BASE_URL.rstrip("/")
    return f"{base}/{processed_key}"


def delete_raw_object(storage_key: str) -> None:
    """Delete a single raw-upload object (or local file). Idempotent —
    a missing object is not an error, since callers use this for cleanup."""
    if not storage_key:
        return
    # TEMP LOCAL STORAGE
    if settings.STORAGE_PROVIDER == "local":
        local_path = _local_raw_path(storage_key)
        try:
            os.remove(local_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise StorageException(f"Failed to delete local raw file: {exc}")
        return
    # END TEMP LOCAL STORAGE

    client = _get_s3_client()
    try:
        client.delete_object(Bucket=settings.S3_RAW_BUCKET, Key=storage_key)
    except Exception as exc:
        raise StorageException(f"Failed to delete raw object '{storage_key}': {exc}")


def delete_processed_objects(prefix: str) -> None:
    """Delete every processed chunk under a model's output prefix
    (model.processed_s3_prefix). Idempotent — a missing prefix/directory
    is not an error."""
    if not prefix:
        return
    # TEMP LOCAL STORAGE
    if settings.STORAGE_PROVIDER == "local":
        import shutil
        local_dir = _local_processed_path(prefix)
        shutil.rmtree(local_dir, ignore_errors=True)
        return
    # END TEMP LOCAL STORAGE

    client = _get_s3_client()
    try:
        paginator = client.get_paginator("list_objects_v2")
        keys_to_delete = [
            {"Key": obj["Key"]}
            for page in paginator.paginate(Bucket=settings.S3_PROCESSED_BUCKET, Prefix=prefix)
            for obj in page.get("Contents", [])
        ]
        # S3 delete_objects caps at 1000 keys per call
        for i in range(0, len(keys_to_delete), 1000):
            batch = keys_to_delete[i : i + 1000]
            client.delete_objects(
                Bucket=settings.S3_PROCESSED_BUCKET, Delete={"Objects": batch}
            )
    except Exception as exc:
        raise StorageException(f"Failed to delete processed objects under '{prefix}': {exc}")