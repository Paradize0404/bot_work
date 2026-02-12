"""
Shared bot utilities — DRY вместо дублей _escape_md в каждом хэндлере.
"""


def escape_md(s: str) -> str:
    """Экранировать спецсимволы Markdown v1 (*, _, `, [)."""
    for ch in ("*", "_", "`", "["):
        s = s.replace(ch, f"\\{ch}")
    return s
