"""Opaque, typed identifiers."""

from secrets import token_urlsafe

PREFIXES = {"ses", "src", "exe", "art", "evd", "val", "vrn"}


def new_id(prefix: str) -> str:
    if prefix not in PREFIXES:
        raise ValueError(f"unknown entity prefix: {prefix}")
    return f"{prefix}_{token_urlsafe(12)}"

