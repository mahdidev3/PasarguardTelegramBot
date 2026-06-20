
"""Security helpers for future admin confirmation and permission flows."""

from __future__ import annotations

import secrets


def make_numeric_code(length: int = 6) -> str:
    if length < 4:
        raise ValueError("confirmation code length must be at least 4")
    return "".join(secrets.choice("0123456789") for _ in range(length))






