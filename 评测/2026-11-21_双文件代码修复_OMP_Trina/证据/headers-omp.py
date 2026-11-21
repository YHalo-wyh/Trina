from __future__ import annotations


def normalize_headers(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """Normalize request headers while preserving duplicate value order."""
    normalized: dict[str, str] = {}
    for name, value in pairs:
        key = name.strip().lower()
        if not key:
            continue
        if key in normalized:
            normalized[key] = f"{normalized[key]}, {value}"
        else:
            normalized[key] = value
    return normalized
