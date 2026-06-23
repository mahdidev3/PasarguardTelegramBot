"""Persian/Arabic digit normalization helpers."""

from __future__ import annotations


def normalize_digits(value: str) -> str:
    fa = "۰۱۲۳۴۵۶۷۸۹"
    ar = "٠١٢٣٤٥٦٧٨٩"
    for i, ch in enumerate(fa):
        value = value.replace(ch, str(i))
    for i, ch in enumerate(ar):
        value = value.replace(ch, str(i))
    return value


