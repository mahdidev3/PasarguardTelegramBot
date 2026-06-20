
r"""Small helpers for admin line parsing.

Admin forms historically use pipe-separated values.  Text fields may need a
literal pipe, so admins can write `\|` inside any field and it will be kept as a
normal `|` instead of being treated as a separator.
"""

from __future__ import annotations


def split_escaped_pipe(line: str, maxsplit: int = -1) -> list[str]:
    r"""Split a line by unescaped `|` and unescape `\|` in fields.

    Examples:
        A | B\|C | D  -> ["A", "B|C", "D"]
        A \\| B       -> ["A | B"]
    """
    out: list[str] = []
    current: list[str] = []
    escaped = False
    splits = 0
    for ch in line or "":
        if escaped:
            if ch == "|":
                current.append("|")
            else:
                current.append("\\")
                current.append(ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "|" and (maxsplit < 0 or splits < maxsplit):
            out.append("".join(current).strip())
            current = []
            splits += 1
            continue
        current.append(ch)
    if escaped:
        current.append("\\")
    out.append("".join(current).strip())
    return out


def pipe_escape_hint() -> str:
    return "برای استفاده از کاراکتر | داخل متن، آن را به شکل <code>\\|</code> بنویسید."






