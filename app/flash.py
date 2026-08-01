"""Session flash toasts — set on POST, shown once on the next page."""

from __future__ import annotations

from fastapi.responses import JSONResponse, RedirectResponse
from starlette.requests import Request

from app.httputil import wants_json


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
) -> RedirectResponse | JSONResponse:
    """Redirect with a one-time toast, or JSON when the client is autosaving."""
    if wants_json(request):
        ok = level != "error"
        payload = {
            "ok": ok,
            "message": (message or "").strip(),
            "level": level if level in ("ok", "warn", "error") else "ok",
            "redirect": url,
        }
        if not ok:
            payload["error"] = (message or "").strip() or "Save failed"
        return JSONResponse(payload, status_code=200 if ok else 400)
    set_flash(request, message, level)
    return RedirectResponse(url, status_code=status_code)
