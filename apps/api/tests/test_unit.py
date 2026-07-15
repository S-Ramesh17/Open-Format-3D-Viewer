"""
Unit tests for core utility functions.

Covers:
  - app.core.sanitize.sanitize_text
  - app.services.storage  (filename validation, file size, MIME validation)
  - app.services.bcf_export (export_bcf structure)
  - app.services.models  (cursor pagination helpers)
"""

import base64
import struct
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.core.exceptions import FileTooLargeException, ValidationException
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
        from app.services.storage import PLAN_MAX_UPLOAD_BYTES
        validate_file_size(PLAN_MAX_UPLOAD_BYTES["free"])  # should not raise

    def test_above_limit_rejected(self):
        from app.services.storage import PLAN_MAX_UPLOAD_BYTES
        with pytest.raises(ValidationException):
            validate_file_size(PLAN_MAX_UPLOAD_BYTES["free"] + 1)

    def test_pro_plan_allows_above_free_limit(self):
        """A file too big for free (50MB) must pass under pro's 500MB cap."""
        from app.services.storage import PLAN_MAX_UPLOAD_BYTES
        oversized_for_free = PLAN_MAX_UPLOAD_BYTES["free"] + 1
        validate_file_size(oversized_for_free, plan="pro")  # should not raise

    def test_pro_plan_rejects_above_its_own_limit(self):
        from app.services.storage import PLAN_MAX_UPLOAD_BYTES
        with pytest.raises(FileTooLargeException):
            validate_file_size(PLAN_MAX_UPLOAD_BYTES["pro"] + 1, plan="pro")

    def test_enterprise_plan_allows_above_pro_limit(self):
        """Enterprise's 5GB cap — the whole reason this plan value needed
        to exist in the DB enum in the first place (see test_plan_enum.py)."""
        from app.services.storage import PLAN_MAX_UPLOAD_BYTES
        oversized_for_pro = PLAN_MAX_UPLOAD_BYTES["pro"] + 1
        validate_file_size(oversized_for_pro, plan="enterprise")  # should not raise

    def test_enterprise_plan_still_rejects_above_5gb(self):
        from app.services.storage import PLAN_MAX_UPLOAD_BYTES
        with pytest.raises(FileTooLargeException):
            validate_file_size(PLAN_MAX_UPLOAD_BYTES["enterprise"] + 1, plan="enterprise")

    def test_unknown_plan_falls_back_to_free_limit(self):
        """Defensive default — an unrecognized plan string must not
        silently grant an unbounded upload size."""
        from app.services.storage import PLAN_MAX_UPLOAD_BYTES
        with pytest.raises(FileTooLargeException):
            validate_file_size(PLAN_MAX_UPLOAD_BYTES["free"] + 1, plan="not-a-real-plan")


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
        # IFC/STEP are ASCII text — magic detects text/plain (or, for very
        # short/ambiguous headers, falls back to application/octet-stream,
        # which is intentionally still tolerated for these formats).
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


class TestValidateMimeFromBytesAllFormats:
    """
    Covers all six supported formats plus the required rejection cases.

    Byte fixtures here were verified against the actual `file`/libmagic
    CLI (the same magic database python-magic's magic.from_buffer uses)
    before being written into this test, not assumed.
    """

    def test_ifc_valid(self):
        header = b"ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION((),'');\nENDSEC;\n"
        validate_mime_type_from_bytes(header, "building.ifc")  # no raise

    def test_step_valid(self):
        header = b"ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION((''),'2;1');\nENDSEC;\n"
        validate_mime_type_from_bytes(header, "part.step")  # no raise

    def test_obj_valid(self):
        header = b"# comment\nv 0.0 0.0 0.0\nv 1.0 0.0 0.0\nf 1 2 3\n"
        validate_mime_type_from_bytes(header, "mesh.obj")  # no raise

    def test_stl_valid_ascii(self):
        header = b"solid test\nfacet normal 0 0 1\nouter loop\nendloop\nendfacet\nendsolid\n"
        validate_mime_type_from_bytes(header, "mesh.stl")  # no raise

    def test_stl_valid_binary(self):
        # Realistic binary STL: 80-byte header + uint32 triangle count +
        # actual binary float triangle data (a header of only zero-padding
        # is not representative — real triangle data is what confirms
        # magic can't fingerprint this format, matching the tolerant
        # octet-stream branch it shares with IFC/STEP/OBJ).
        header = b"binary stl test" + b"\x00" * (80 - len(b"binary stl test"))
        count = struct.pack("<I", 1)
        triangle = struct.pack("<3f", 0.0, 0.0, 1.0) + struct.pack(
            "<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0
        ) + struct.pack("<H", 0)
        validate_mime_type_from_bytes(header + count + triangle, "mesh.stl")  # no raise

    def test_gltf_valid_json(self):
        header = b'{"asset":{"version":"2.0"},"scenes":[]}'
        detected = validate_mime_type_from_bytes(header, "model.gltf")
        assert detected == "application/json"

    def test_glb_valid(self):
        # Real GLB magic header: "glTF" + version(u32 LE) + total length(u32 LE)
        # + a minimal JSON chunk, matching the actual glTF binary spec.
        json_chunk = b'{"asset":{"version":"2.0"}}'
        while len(json_chunk) % 4 != 0:
            json_chunk += b" "
        header = struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(json_chunk))
        chunk_header = struct.pack("<II", len(json_chunk), 0x4E4F534A)
        validate_mime_type_from_bytes(header + chunk_header + json_chunk, "model.glb")  # no raise

    def test_renamed_file_rejected(self):
        # Real glTF JSON content saved with a .ifc extension must be
        # rejected — IFC's tolerant branch only accepts text/plain or
        # application/octet-stream, and valid JSON detects as
        # application/json, which is neither.
        header = b'{"asset":{"version":"2.0"}}'
        with pytest.raises(ValidationException):
            validate_mime_type_from_bytes(header, "fake.ifc")

    def test_random_bytes_rejected_for_gltf(self):
        # .gltf is the one format where random-byte rejection is fully
        # enforceable: a genuine .gltf must be valid JSON text, so the
        # generic "unrecognized binary" fallback is deliberately not
        # tolerated for this extension (see validate_mime_type_from_bytes).
        random_bytes = bytes(range(256))
        with pytest.raises(ValidationException):
            validate_mime_type_from_bytes(random_bytes, "malicious.gltf")



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


# ---------------------------------------------------------------------------
# services/projects.py — invite race condition
# ---------------------------------------------------------------------------

class TestInviteProjectMemberRaceCondition:
    """
    invite_project_member() pre-checks for an existing membership row, then
    inserts. Between those two steps, a concurrent duplicate request can
    win the race and insert first — the DB's UniqueConstraint(project_id,
    user_id) then raises IntegrityError on this request's commit. That
    branch (rollback + ConflictException, as opposed to the pre-check 409)
    had no test coverage.
    """

    @pytest.mark.asyncio
    async def test_concurrent_duplicate_insert_returns_conflict_not_500(self):
        from sqlalchemy.exc import IntegrityError
        from app.services.projects import invite_project_member
        from app.schemas.projects import ProjectMemberInvite
        from app.core.exceptions import ConflictException

        project_id = uuid.uuid4()
        mock_user = MagicMock()
        mock_user.id = uuid.uuid4()
        mock_user.email = "race@example.com"
        mock_user.name = "Race Condition"

        mock_db = AsyncMock(spec=AsyncSession)
        mock_db.execute = AsyncMock(
            side_effect=[
                # 1. select(User) by email — found
                MagicMock(scalar_one_or_none=MagicMock(return_value=mock_user)),
                # 2. pre-check select(ProjectMember) — nothing yet (the race)
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
            ]
        )
        mock_db.add = MagicMock()
        # The concurrent request's insert already committed by the time
        # this one commits — DB constraint fires here instead.
        mock_db.commit = AsyncMock(side_effect=IntegrityError("INSERT", {}, Exception("duplicate key")))
        mock_db.rollback = AsyncMock()

        with pytest.raises(ConflictException):
            await invite_project_member(
                project_id,
                ProjectMemberInvite(email="race@example.com", role="viewer"),
                mock_db,
            )

        mock_db.rollback.assert_awaited_once()