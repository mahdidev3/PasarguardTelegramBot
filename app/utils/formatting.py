"""Shared formatting helpers for future extracted handlers."""

from __future__ import annotations

import html
from typing import Any


def h(value: Any) -> str:
    return html.escape(str(value or ""))


def fmt_money(amount: int) -> str:
    return f"{int(amount):,}".replace(",", "٬") + " تومان"


def fmt_number(value: int | float) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}".replace(",", "٬")









