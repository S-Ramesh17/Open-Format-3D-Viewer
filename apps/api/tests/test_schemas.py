import pytest
from pydantic import ValidationError

from app.schemas.auth import LoginRequest, RegisterRequest
from app.schemas.projects import ProjectCreate


class TestRegisterRequestSchema:
    def test_valid_registration(self):
        req = RegisterRequest(
            email="user@example.com", password="securepass123", name="Jane"
        )
        assert req.email == "user@example.com"

    def test_password_too_short_rejected(self):
        with pytest.raises(ValidationError):
            RegisterRequest(email="user@example.com", password="short1")

    def test_all_digit_password_rejected(self):
        with pytest.raises(ValidationError):
            RegisterRequest(email="user@example.com", password="12345678")

    def test_email_is_lowercased(self):
        req = RegisterRequest(email="TEST@EXAMPLE.COM", password="securepass123")
        assert req.email == "test@example.com"

    def test_invalid_email_format_rejected(self):
        with pytest.raises(ValidationError):
            RegisterRequest(email="not-an-email", password="securepass123")


class TestLoginRequestSchema:
    def test_valid_login(self):
        req = LoginRequest(email="user@example.com", password="anypassword")
        assert req.email == "user@example.com"

    def test_blank_password_rejected(self):
        with pytest.raises(ValidationError):
            LoginRequest(email="user@example.com", password="")


class TestProjectCreateSchema:
    def test_valid_project(self):
        proj = ProjectCreate(name="Tower Project", description="A tower")
        assert proj.name == "Tower Project"

    def test_blank_name_rejected(self):
        with pytest.raises(ValidationError):
            ProjectCreate(name="   ", description=None)

    def test_name_is_stripped(self):
        proj = ProjectCreate(name="  Tower  ", description=None)
        assert proj.name == "Tower"

    def test_name_too_long_rejected(self):
        with pytest.raises(ValidationError):
            ProjectCreate(name="x" * 256, description=None)