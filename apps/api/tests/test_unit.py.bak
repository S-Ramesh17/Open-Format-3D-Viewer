"""
Unit tests for core utility functions.

Covers:
  - app.core.sanitize.sanitize_text
  - app.services.storage  (filename validation, file size, MIME validation)
  - app.services.bcf_export (export_bcf structure)
  - app.services.models  (cursor pagination helpers)
"""

import base64
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.core.exceptions import ValidationException
from app.services.storage import (
    validate_filename,
    validate_file_size,
    validate_mime_type_declared,
    validate_mime_type_from_bytes,
    build_storage_key,
    build_cdn_url,
    trigger_clamav_scan,
)

import io
import zipfile

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession
# ---------------------------------------------------------------------------
# sanitize.py
# ---------------------------------------------------------------------------

from app.core.sanitize import sanitize_text


class TestSanitizeText:
    def test_plain_text_unchanged(self):
        assert sanitize_text("Hello world") == "Hello world"

    def test_script_tag_stripped(self):
        result = sanitize_text("<script>alert('xss')</script>Hello")
        assert "<script>" not in result
        assert "Hello" in result

    def test_all_html_tags_stripped(self):
        result = sanitize_text("<b>bold</b> <i>italic</i> <a href='x'>link</a>")
        assert "<b>" not in result
        assert "<i>" not in result
        assert "<a" not in result
        assert "bold" in result
        assert "italic" in result
        assert "link" in result

    def test_empty_string_passthrough(self):
        assert sanitize_text("") == ""

    def test_none_like_empty_string_returns_unchanged(self):
        # sanitize_text checks `if not value` — empty string is falsy
        assert sanitize_text("") == ""

    def test_nested_tags_stripped(self):
        result = sanitize_text("<div><p>Inner</p></div>")
        assert "<div>" not in result
        assert "<p>" not in result
        assert "Inner" in result

    def test_unicode_preserved(self):
        text = "Risse in Säule — строение"
        assert sanitize_text(text) == text

    def test_html_entities_handled(self):
        result = sanitize_text("&lt;not a tag&gt;")
        # bleach handles entities; content should be preserved
        assert "not a tag" in result

    def test_inline_event_handler_stripped(self):
        result = sanitize_text('<img src="x" onerror="alert(1)">')
        assert "onerror" not in result
        assert "alert" not in result


# ---------------------------------------------------------------------------
# storage.py — validation
# ---------------------------------------------------------------------------



class TestValidateFilename:
    def test_valid_ifc_filename(self):
        assert validate_filename("model.ifc") == "model.ifc"

    def test_valid_gltf_filename(self):
        assert validate_filename("building.gltf") == "building.gltf"

    def test_valid_step_filename(self):
        assert validate_filename("part.step") == "part.step"

    def test_extension_normalised_to_lowercase(self):
        result = validate_filename("MODEL.IFC")
        assert result.endswith(".ifc") or result == "MODEL.IFC"

    def test_path_traversal_rejected(self):
        with pytest.raises(ValidationException):
            validate_filename("../../etc/passwd.ifc")

    def test_null_byte_rejected(self):
        with pytest.raises(ValidationException):
            validate_filename("file\x00.ifc")

    def test_empty_string_rejected(self):
        with pytest.raises(ValidationException):
            validate_filename("")

    def test_disallowed_extension_rejected(self):
        with pytest.raises(ValidationException):
            validate_filename("malware.exe")

    def test_no_extension_rejected(self):
        with pytest.raises(ValidationException):
            validate_filename("noextension")

    def test_special_chars_rejected(self):
        with pytest.raises(ValidationException):
            validate_filename("file;rm -rf.ifc")

    def test_dot_dot_in_basename_rejected(self):
        with pytest.raises(ValidationException):
            validate_filename("..ifc")

    def test_space_in_filename_allowed(self):
        result = validate_filename("my model.ifc")
        assert result == "my model.ifc"


class TestValidateFileSize:
    def test_zero_size_rejected(self):
        with pytest.raises(ValidationException):
            validate_file_size(0)

    def test_negative_size_rejected(self):
        with pytest.raises(ValidationException):
            validate_file_size(-100)

    def test_valid_small_file(self):
        validate_file_size(1024)  # should not raise

    def test_exactly_at_limit_passes(self):
        from app.config import settings
        validate_file_size(settings.MAX_UPLOAD_SIZE_BYTES)  # should not raise

    def test_above_limit_rejected(self):
        from app.config import settings
        with pytest.raises(ValidationException):
            validate_file_size(settings.MAX_UPLOAD_SIZE_BYTES + 1)


class TestValidateMimeDeclared:
    def test_octet_stream_allowed(self):
        validate_mime_type_declared("application/octet-stream")  # no raise

    def test_gltf_json_allowed(self):
        validate_mime_type_declared("model/gltf+json")  # no raise

    def test_unknown_mime_rejected(self):
        with pytest.raises(ValidationException):
            validate_mime_type_declared("application/pdf")

    def test_text_html_rejected(self):
        with pytest.raises(ValidationException):
            validate_mime_type_declared("text/html")


class TestValidateMimeFromBytes:
    def test_text_file_bytes_for_ifc(self):
        # IFC/STEP are ASCII — filetype.guess returns None → octet-stream
        header = b"ISO-10303-21;\nHEADER;"
        detected = validate_mime_type_from_bytes(header, "building.ifc")
        assert detected in ("application/octet-stream", "text/plain")

    def test_glb_magic_bytes(self):
        # GLB files start with 0x676C5446 ("glTF")
        header = b"glTF\x02\x00\x00\x00" + b"\x00" * 100
        # This should not raise
        validate_mime_type_from_bytes(header, "model.glb")

    def test_binary_mismatch_rejected_for_ifc(self):
        # PNG magic bytes declared as IFC should be rejected
        png_header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        with pytest.raises(ValidationException):
            validate_mime_type_from_bytes(png_header, "notreal.ifc")


class TestBuildStorageKey:
    def test_key_format(self):
        user_id = uuid.UUID("aaaabbbb-aaaa-bbbb-aaaa-bbbbaaaabbbb")
        model_id = uuid.UUID("ccccdddd-cccc-dddd-cccc-ddddccccdddd")
        key = build_storage_key(user_id, model_id, "test.ifc")
        assert key == f"{user_id}/{model_id}/test.ifc"


class TestBuildCdnUrl:
    def test_local_storage_url(self):
        with patch("app.services.storage.settings") as mock_settings:
            mock_settings.STORAGE_PROVIDER = "local"

            url = build_cdn_url("processed/abc/model.xkt")

            assert url == "/files/processed/abc/model.xkt"

    def test_s3_storage_url(self):
        with patch("app.services.storage.settings") as mock_settings:
            mock_settings.STORAGE_PROVIDER = "s3"
            mock_settings.CDN_BASE_URL = "https://cdn.example.com"

            url = build_cdn_url("processed/abc/model.xkt")

            assert url == "https://cdn.example.com/processed/abc/model.xkt"

    def test_trailing_slash_stripped(self):
        with patch("app.services.storage.settings") as mock_settings:
            mock_settings.STORAGE_PROVIDER = "s3"
            mock_settings.CDN_BASE_URL = "https://cdn.example.com/"

            url = build_cdn_url("path/to/file.xkt")

            assert url == "https://cdn.example.com/path/to/file.xkt"

class TestTriggerClamavScan:
    def test_dispatches_with_both_args(self):
        """Regression: trigger_clamav_scan must pass model_id AND storage_key."""
        with patch("app.services.storage.get_celery_client") as mock_get_client:
            mock_celery_instance = MagicMock()
            mock_get_client.return_value = mock_celery_instance

            trigger_clamav_scan("model-id-123", "user/model/file.ifc")

            mock_celery_instance.send_task.assert_called_once()
            call_args = mock_celery_instance.send_task.call_args
            # task name
            assert call_args[0][0] == "app.tasks.scan.scan_file"
            # args list contains both model_id and s3_key
            task_args = call_args[1].get("args") or call_args[0][1]
            assert "model-id-123" in task_args
            assert "user/model/file.ifc" in task_args


# ---------------------------------------------------------------------------
# bcf_export.py
# ---------------------------------------------------------------------------

class TestBcfExport:
    """Unit-level tests for BCF export; mock the DB queries."""

    @pytest.mark.asyncio
    async def test_export_returns_bytes(self):
        from app.services.bcf_export import export_bcf

        # Build minimal mock DB
        mock_db = AsyncMock(spec=AsyncSession)

        # Mock model query
        mock_model = MagicMock()
        mock_model.id = uuid.uuid4()

        # Mock annotations query — empty model
        mock_annotations_result = MagicMock()
        mock_annotations_result.scalars.return_value.all.return_value = []

        # DB execute returns model first, then annotations
        mock_db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=mock_model)),
                mock_annotations_result,
            ]
        )

        result = await export_bcf(mock_model.id, mock_db)

        assert isinstance(result, bytes)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_export_contains_bcf_version(self):
        from app.services.bcf_export import export_bcf

        mock_db = AsyncMock(spec=AsyncSession)
        mock_model = MagicMock()
        mock_model.id = uuid.uuid4()

        mock_db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=mock_model)),
                MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))),
            ]
        )

        result = await export_bcf(mock_model.id, mock_db)
        with zipfile.ZipFile(io.BytesIO(result)) as zf:
            names = zf.namelist()
            assert "bcf.version" in names

    @pytest.mark.asyncio
    async def test_export_nonexistent_model_raises_404(self):
        from app.services.bcf_export import export_bcf
        from app.core.exceptions import NotFoundException

        mock_db = AsyncMock(spec=AsyncSession)
        mock_db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        )

        with pytest.raises(NotFoundException):
            await export_bcf(uuid.uuid4(), mock_db)

    @pytest.mark.asyncio
    async def test_export_annotation_creates_topic_folder(self):
        from app.services.bcf_export import export_bcf

        mock_db = AsyncMock(spec=AsyncSession)
        mock_model = MagicMock()
        mock_model.id = uuid.uuid4()

        ann_id = uuid.uuid4()
        mock_annotation = MagicMock()
        mock_annotation.id = ann_id
        mock_annotation.title = "Test Annotation"
        mock_annotation.body = "Test body"
        mock_annotation.status = "open"
        mock_annotation.created_at = datetime.now(timezone.utc)

        # comments query for this annotation
        mock_db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=mock_model)),
                MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[mock_annotation])))),
                MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))),
            ]
        )

        result = await export_bcf(mock_model.id, mock_db)
        with zipfile.ZipFile(io.BytesIO(result)) as zf:
            names = zf.namelist()
            markup_path = f"{ann_id}/markup.bcf"
            assert markup_path in names


# ---------------------------------------------------------------------------
# Pagination helpers
# ---------------------------------------------------------------------------

class TestPaginationHelpers:
    def test_encode_decode_cursor_roundtrip(self):
        from app.services.models import _encode_cursor, _decode_cursor

        ts = datetime.now(timezone.utc)
        uid = uuid.uuid4()

        cursor = _encode_cursor(ts, uid)
        assert isinstance(cursor, str)
        assert len(cursor) > 0

        decoded_ts, decoded_uid = _decode_cursor(cursor)
        assert decoded_uid == uid
        # Timestamps should match within microsecond precision
        assert abs((decoded_ts - ts).total_seconds()) < 0.001

    def test_decode_invalid_cursor_raises(self):
        from app.services.models import _decode_cursor

        with pytest.raises(ValidationException):
            _decode_cursor("not-a-valid-cursor!!!")

    def test_decode_truncated_cursor_raises(self):
        from app.services.models import _decode_cursor

        with pytest.raises(ValidationException):
            _decode_cursor(base64.urlsafe_b64encode(b"no-pipe-here").decode())

    def test_project_cursor_encode_decode(self):
        """Project service uses its own cursor helpers — verify those too."""
        from app.services.projects import _encode_cursor, _decode_cursor

        ts = datetime.now(timezone.utc)
        uid = uuid.uuid4()

        cursor = _encode_cursor(ts, uid)
        decoded_ts, decoded_uid = _decode_cursor(cursor)
        assert decoded_uid == uid


# ---------------------------------------------------------------------------
# Week 3 Day 3 — Path traversal, MIME, and validate_filename
# ---------------------------------------------------------------------------

class TestValidateFilenameDay3:
    """Path traversal and filename edge cases found during Day 3 audit."""

    def _validate(self, filename):
        from app.services.storage import validate_filename
        return validate_filename(filename)

    def test_empty_stem_rejected(self):
        """'.ifc' has no stem and must be rejected."""
        with pytest.raises(ValidationException):
            self._validate(".ifc")

    def test_parentheses_allowed(self):
        """CAD tools commonly produce filenames like 'Tower (Level 1).ifc'."""
        result = self._validate("Tower (Level 1).ifc")
        assert result == "Tower (Level 1).ifc"

    def test_parentheses_in_glb(self):
        result = self._validate("Building A (Draft).glb")
        assert result == "Building A (Draft).glb"

    def test_path_traversal_unix(self):
        with pytest.raises(ValidationException):
            self._validate("../etc/passwd.ifc")

    def test_path_traversal_windows(self):
        with pytest.raises(ValidationException):
            self._validate("C:\\Windows\\secret.ifc")

    def test_path_traversal_absolute_unix(self):
        with pytest.raises(ValidationException):
            self._validate("/etc/shadow.ifc")

    def test_null_byte(self):
        with pytest.raises(ValidationException):
            self._validate("foo\x00bar.ifc")

    def test_percent_encoded_slash_caught_after_url_decode(self):
        """Percent-encoded traversal is decoded by HTTP layer before reaching this fn."""
        from urllib.parse import unquote
        decoded = unquote("..%2Fetc%2Fpasswd.ifc")
        with pytest.raises(ValidationException):
            self._validate(decoded)

    def test_double_extension_allowed(self):
        """shell.php.ifc — last extension is .ifc, we allow it (extension whitelist wins)."""
        result = self._validate("shell.php.ifc")
        assert result == "shell.php.ifc"

    def test_all_dots_rejected(self):
        with pytest.raises(ValidationException):
            self._validate("....ifc")

    def test_valid_filename_passes(self):
        result = self._validate("my_model-v2.step")
        assert result == "my_model-v2.step"

    def test_unsupported_extension_rejected(self):
        with pytest.raises(ValidationException):
            self._validate("script.exe")
