from __future__ import annotations

from app.security import hash_password, verify_password


def test_bcrypt_password_hash_round_trip():
    password_hash = hash_password("VeryLongPassword123")

    assert verify_password("VeryLongPassword123", password_hash)
    assert not verify_password("wrong-password", password_hash)

