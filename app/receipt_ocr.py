"""On-farm OCR for receipt / statement photos with light field guessing."""

from __future__ import annotations

import io
import re
from datetime import date, datetime
from typing import Any

from PIL import Image, ImageOps

_engine = None
_engine_failed = False


def _get_engine():
    global _engine, _engine_failed
    if _engine_failed:
        return None
    if _engine is not None:
        return _engine
    try:
        from rapidocr_onnxruntime import RapidOCR

        _engine = RapidOCR()
        return _engine
    except Exception:
        _engine_failed = True
        return None


def _prepare_image(image_bytes: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    # Cap long edge for faster OCR on phone photos
    max_edge = 1600
    w, h = img.size
    scale = max(w, h) / max_edge
    if scale > 1:
        img = img.resize((int(w / scale), int(h / scale)))
    return img


def extract_text(image_bytes: bytes) -> tuple[str, str]:
    """
    Returns (ocr_text, status_note).
    status_note is empty on success, or explains fallback.
    """
    try:
        img = _prepare_image(image_bytes)
    except Exception as exc:
        return "", f"Could not open image ({exc}). Type the details below."

    engine = _get_engine()
    if engine is None:
        return "", "OCR not available on this PC yet — type or paste what you see, then file it."

    try:
        import numpy as np

        result, _ = engine(np.array(img))
    except Exception as exc:
        return "", f"OCR failed ({exc}). Type the details below."

    if not result:
        return "", "No text found — try a clearer photo, or type the details below."

    lines: list[str] = []
    for row in result:
        # RapidOCR rows: [box, text, score]
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            text = str(row[1]).strip()
            if text:
                lines.append(text)
    return "\n".join(lines), ""


_DATE_PATTERNS = [
    re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b"),
    re.compile(r"\b(\d{4})[/-](\d{1,2})[/-](\d{1,2})\b"),
]
_MONEY_PATTERNS = [
    re.compile(
        r"(?i)\b(?:total|amount\s*due|balance|grand\s*total|amount)\b\s*[:\-]?\s*\$?\s*([\d,]+\.\d{2})\b"
    ),
    re.compile(r"(?i)\btotal\s*([\d,]+\.\d{2})\b"),
    re.compile(r"\$\s*([\d,]+\.\d{2})\b"),
    re.compile(r"\b([\d,]+\.\d{2})\b"),
]


def _parse_date(text: str) -> str | None:
    for pat in _DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        g = m.groups()
        try:
            if len(g[0]) == 4:
                y, mo, d = int(g[0]), int(g[1]), int(g[2])
            else:
                mo, d, y = int(g[0]), int(g[1]), int(g[2])
                if y < 100:
                    y += 2000
            return date(y, mo, d).isoformat()
        except ValueError:
            continue
    return None


def _parse_total(text: str) -> float | None:
    candidates: list[float] = []
    for pat in _MONEY_PATTERNS:
        for m in pat.finditer(text):
            try:
                val = float(m.group(1).replace(",", ""))
                if 0 < val < 1_000_000:
                    candidates.append(val)
            except ValueError:
                continue
    if not candidates:
        return None
    # Prefer values near "total" matches (first pattern), else largest
    return max(candidates)


def _guess_vendor(lines: list[str]) -> str | None:
    skip = re.compile(r"(?i)^(total|subtotal|tax|date|invoice|receipt|thank|visa|mastercard|auth|tel|phone|www\.|http)")
    for line in lines[:8]:
        cleaned = line.strip()
        if len(cleaned) < 3 or skip.search(cleaned):
            continue
        if re.fullmatch(r"[\d\s\-\(\)\.\$/]+", cleaned):
            continue
        return cleaned[:120]
    return None


def suggest_fields(ocr_text: str) -> dict[str, Any]:
    lines = [ln.strip() for ln in (ocr_text or "").splitlines() if ln.strip()]
    joined = "\n".join(lines)
    total = _parse_total(joined)
    return {
        "vendor": _guess_vendor(lines),
        "date": _parse_date(joined) or date.today().isoformat(),
        "total": total,
        "quantity": 1.0 if total is not None else None,
        "product_hint": None,
        "description": (lines[0][:200] if lines else None),
    }


def run_ocr(image_bytes: bytes) -> dict[str, Any]:
    text, note = extract_text(image_bytes)
    suggested = suggest_fields(text)
    return {
        "ocr_text": text,
        "ocr_note": note,
        "suggested": suggested,
        "scanned_at": datetime.utcnow().isoformat(timespec="seconds"),
    }
