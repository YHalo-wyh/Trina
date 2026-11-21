from __future__ import annotations


def normalize_headers(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """Normalize request headers while preserving duplicate value order."""
    return {name.lower(): value for name, value in pairs}
