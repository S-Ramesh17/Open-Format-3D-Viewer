import pytest
from jose import JWTError

from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)


class TestPasswordHashing:
    def test_hash_password_produces_bcrypt_hash(self):
        hashed = hash_password("testpassword123")
        assert hashed.startswith("$2b$")

    def test_verify_password_correct(self):
        hashed = hash_password("testpassword123")
        assert verify_password("testpassword123", hashed) is True

    def test_verify_password_incorrect(self):
        hashed = hash_password("testpassword123")
        assert verify_password("wrongpassword", hashed) is False

    def test_hash_is_unique_per_call(self):
        # bcrypt includes a random salt — same input produces different hashes
        h1 = hash_password("samepassword")
        h2 = hash_password("samepassword")
        assert h1 != h2


class TestJWT:
    def test_create_access_token_has_correct_claims(self):
        token = create_access_token(subject="user-123")
        payload = decode_token(token)
        assert payload["sub"] == "user-123"
        assert payload["type"] == "access"
        assert "exp" in payload
        assert "iat" in payload

    def test_create_refresh_token_has_correct_claims(self):
        token = create_refresh_token(subject="user-123")
        payload = decode_token(token)
        assert payload["sub"] == "user-123"
        assert payload["type"] == "refresh"

    def test_access_and_refresh_tokens_differ(self):
        access = create_access_token(subject="user-123")
        refresh = create_refresh_token(subject="user-123")
        assert access != refresh

    def test_decode_invalid_token_raises(self):
        with pytest.raises(JWTError):
            decode_token("not.a.valid.token")

    def test_decode_expired_token_raises(self):
        """ExpiredSignatureError is a JWTError subclass — decode_token must
        surface it the same way as any other invalid token, since callers
        (AuthMiddleware, refresh_access_token) only catch JWTError."""
        import time
        from jose import jwt as jose_jwt
        from app.config import settings
        from app.core.security import ALGORITHM

        expired_payload = {
            "sub": "user-expired",
            "type": "access",
            "iat": int(time.time()) - 7200,
            "exp": int(time.time()) - 3600,  # expired 1 hour ago
        }
        expired_token = jose_jwt.encode(expired_payload, settings.SECRET_KEY, algorithm=ALGORITHM)

        with pytest.raises(JWTError):
            decode_token(expired_token)