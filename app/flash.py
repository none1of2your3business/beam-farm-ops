"""Session flash toasts — set on POST, shown once on the next page."""

from __future__ import annotations

from fastapi.responses import RedirectResponse
from starlette.requests import Request


def set_flash(request: Request, message: str, level: str = "ok") -> None:
    """level: ok | warn | error"""
    if level not in ("ok", "warn", "error"):
        level = "ok"
    request.session["flash"] = {"message": (message or "").strip(), "level": level}


def pop_flash(request: Request) -> dict | None:
    flash = request.session.pop("flash", None)
    if not flash or not flash.get("message"):
        return None
    return flash


def redirect_flash(
    request: Request,
    url: str,
    message: str,
    level: str = "ok",
    status_code: int = 303,
) -> RedirectResponse:
    set_flash(request, message, level)
    return RedirectResponse(url, status_code=status_code)
