"""Shared HTTP helpers for form saves and fetch autosave."""

from __future__ import annotations

from starlette.requests import Request


def wants_json(request: Request) -> bool:
    """True when the client asked for a JSON save response (autosave / fetch)."""
    if (request.headers.get("x-requested-with") or "").lower() == "fetch":
        return True
    if (request.query_params.get("format") or "").lower() == "json":
        return True
    accept = (request.headers.get("accept") or "").lower()
    return "application/json" in accept
