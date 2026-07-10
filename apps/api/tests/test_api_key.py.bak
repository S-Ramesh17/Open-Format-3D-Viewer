import secrets

import pytest

from app.services.api_key import _generate_raw_key, _hash_key, API_KEY_PREFIX


class TestApiKeyGeneration:
    def test_generated_key_has_correct_prefix(self):
        key = _generate_raw_key()
        assert key.startswith(API_KEY_PREFIX)

    def test_generated_keys_are_unique(self):
        keys = {_generate_raw_key() for _ in range(50)}
        assert len(keys) == 50

    def test_key_hash_is_sha256_hex_length(self):
        key = _generate_raw_key()
        hashed = _hash_key(key)
        assert len(hashed) == 64
        int(hashed, 16)  # raises ValueError if not valid hex

    def test_same_key_produces_same_hash(self):
        key = _generate_raw_key()
        assert _hash_key(key) == _hash_key(key)

    def test_different_keys_produce_different_hashes(self):
        k1 = _generate_raw_key()
        k2 = _generate_raw_key()
        assert _hash_key(k1) != _hash_key(k2)


class TestCompareDigest:
    def test_compare_digest_matches_identical_hashes(self):
        key = _generate_raw_key()
        h1 = _hash_key(key)
        h2 = _hash_key(key)
        assert secrets.compare_digest(h1, h2) is True

    def test_compare_digest_rejects_different_hashes(self):
        h1 = _hash_key(_generate_raw_key())
        h2 = _hash_key(_generate_raw_key())
        assert secrets.compare_digest(h1, h2) is False