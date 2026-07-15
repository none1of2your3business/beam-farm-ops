"""Delayed CME corn/soy futures via Yahoo Finance (informational only).

Nearby continuous (ZC=F / ZS=F) plus a rolling ~12-month strip of listed
contract months (e.g. Jul → Jul next year). Front month drops after last
trading day; the next listed month becomes the new front.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional


TICKERS = {
    "Corn": "ZC=F",
    "Soybeans": "ZS=F",
}

# Listed delivery months (CME month code, calendar month)
CORN_MONTHS = (("H", 3), ("K", 5), ("N", 7), ("U", 9), ("Z", 12))
SOY_MONTHS = (("F", 1), ("H", 3), ("K", 5), ("N", 7), ("Q", 8), ("U", 9), ("X", 11))

MONTH_NAMES = {
    1: "Jan",
    2: "Feb",
    3: "Mar",
    4: "Apr",
    5: "May",
    6: "Jun",
    7: "Jul",
    8: "Aug",
    9: "Sep",
    10: "Oct",
    11: "Nov",
    12: "Dec",
}


def _to_dollars_per_bu(raw: float) -> float:
    """Yahoo CBOT grain quotes are typically cents/bu; convert when clearly cents."""
    if raw is None:
        return 0.0
    val = float(raw)
    if val > 40:
        return round(val / 100.0, 4)
    return round(val, 4)


def last_trading_day(year: int, month: int) -> date:
    """CME grains: business day prior to the 15th calendar day of the contract month."""
    d = date(year, month, 15) - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def yahoo_symbol(root: str, month_code: str, year: int) -> str:
    """e.g. ZC + N + 26 → ZCN26.CBT"""
    return f"{root}{month_code}{year % 100:02d}.CBT"


def yy_short(year: int) -> str:
    return f"{year % 100:02d}"


def strip_contracts(crop: str, as_of: Optional[date] = None) -> list[dict[str, Any]]:
    """
    Active listed contracts from the current unexpired front month through
    the same calendar month one year later (e.g. Jul'26 … Jul'27).
    When the front expires, the window rolls forward automatically.
    """
    as_of = as_of or date.today()
    months = CORN_MONTHS if crop == "Corn" else SOY_MONTHS
    root = "ZC" if crop == "Corn" else "ZS"

    candidates: list[dict[str, Any]] = []
    for y in range(as_of.year - 1, as_of.year + 3):
        for code, m in months:
            ltd = last_trading_day(y, m)
            if ltd < as_of:
                continue
            candidates.append(
                {
                    "crop": crop,
                    "month_code": code,
                    "month": m,
                    "year": y,
                    "label": f"{MONTH_NAMES[m]} '{yy_short(y)}",
                    "short": f"{code}{yy_short(y)}",
                    "ticker": yahoo_symbol(root, code, y),
                    "last_trading_day": ltd.isoformat(),
                    "is_front": False,
                }
            )

    if not candidates:
        return []

    front = candidates[0]
    front["is_front"] = True
    end_year = front["year"] + 1
    end_month = front["month"]

    out: list[dict[str, Any]] = []
    for c in candidates:
        if (c["year"], c["month"]) <= (end_year, end_month):
            out.append(c)
        else:
            break
    return out


def _fi_get(info: Any, *names: str) -> Any:
    if info is None:
        return None
    if isinstance(info, dict):
        for n in names:
            if info.get(n) is not None:
                return info.get(n)
        return None
    for n in names:
        try:
            v = getattr(info, n, None)
        except Exception:
            v = None
        if v is not None:
            return v
    return None


def _quote_one_impl(ticker: str) -> Optional[dict[str, Any]]:
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        t = yf.Ticker(ticker)
        last = None
        prev = None
        open_px = None
        high_52 = None
        low_52 = None
        try:
            fi = t.fast_info or {}
            last = _fi_get(fi, "lastPrice", "last_price", "regularMarketPrice", "last")
            prev = _fi_get(fi, "previousClose", "previous_close", "regularMarketPreviousClose")
            open_px = _fi_get(fi, "open", "regularMarketOpen", "openPrice")
            high_52 = _fi_get(fi, "yearHigh", "year_high", "fiftyTwoWeekHigh")
            low_52 = _fi_get(fi, "yearLow", "year_low", "fiftyTwoWeekLow")
        except Exception:
            pass
        if last is None or open_px is None or high_52 is None or low_52 is None:
            try:
                info = t.info or {}
                if isinstance(info, dict):
                    last = last or info.get("regularMarketPrice") or info.get("previousClose")
                    prev = prev or info.get("regularMarketPreviousClose") or info.get("previousClose")
                    open_px = open_px or info.get("regularMarketOpen") or info.get("open")
                    high_52 = high_52 or info.get("fiftyTwoWeekHigh")
                    low_52 = low_52 or info.get("fiftyTwoWeekLow")
            except Exception:
                pass
        if last is None or open_px is None:
            hist = t.history(period="5d")
            if hist is not None and len(hist) > 0:
                if last is None:
                    last = float(hist["Close"].iloc[-1])
                if prev is None and len(hist) > 1:
                    prev = float(hist["Close"].iloc[-2])
                if open_px is None and "Open" in hist.columns:
                    open_px = float(hist["Open"].iloc[-1])
        if high_52 is None or low_52 is None:
            try:
                yr = t.history(period="1y")
                if yr is not None and len(yr) > 0:
                    if high_52 is None and "High" in yr.columns:
                        high_52 = float(yr["High"].max())
                    if low_52 is None and "Low" in yr.columns:
                        low_52 = float(yr["Low"].min())
            except Exception:
                pass
        if last is None:
            return None
        price = _to_dollars_per_bu(float(last))
        change = None
        if prev is not None:
            change = round(price - _to_dollars_per_bu(float(prev)), 4)
        open_d = _to_dollars_per_bu(float(open_px)) if open_px is not None else None
        high_d = _to_dollars_per_bu(float(high_52)) if high_52 is not None else None
        low_d = _to_dollars_per_bu(float(low_52)) if low_52 is not None else None
        return {
            "price": price,
            "change": change,
            "open": open_d,
            "prev_close": _to_dollars_per_bu(float(prev)) if prev is not None else None,
            "high_52w": high_d,
            "low_52w": low_d,
            "ticker": ticker,
            "source": "yahoo_delayed",
            "as_of": datetime.now(timezone.utc),
        }
    except Exception:
        return None


def _quote_one(ticker: str, timeout: float = 2.5) -> Optional[dict[str, Any]]:
    """Fetch one Yahoo quote with a hard timeout so pages never hang forever."""
    if timeout <= 0:
        return None
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(_quote_one_impl, ticker)
        try:
            return fut.result(timeout=timeout)
        except FuturesTimeout:
            return None
        except Exception:
            return None


def fetch_nearby_quotes(max_seconds: float = 12.0) -> dict[str, Any]:
    """Nearby continuous + full-year strip for Corn and Soybeans.

    Bounded total runtime so Refresh Quotes can't lock the UI for minutes.
    """
    out: dict[str, Any] = {
        "error": None,
        "as_of": datetime.now(timezone.utc),
        "strip": {"Corn": [], "Soybeans": []},
    }
    try:
        import yfinance  # noqa: F401
    except ImportError:
        out["error"] = "yfinance not installed"
        for crop in TICKERS:
            out[crop] = None
        return out

    deadline = time.monotonic() + max(3.0, float(max_seconds or 12.0))
    timed_out = False

    # Sequential fetches avoid Yahoo rate limits better than a big multi-download.
    for crop in ("Corn", "Soybeans"):
        strip_meta = strip_contracts(crop)
        strip_rows: list[dict[str, Any]] = []
        for meta in strip_meta:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                strip_rows.append({**meta, "price": None, "change": None})
                continue
            # Strip rows only need last/change — keep each fetch short.
            q = _quote_one(meta["ticker"], timeout=min(2.0, remaining))
            strip_rows.append(
                {
                    **meta,
                    "price": q["price"] if q else None,
                    "change": q["change"] if q else None,
                }
            )
            if q is not None:
                time.sleep(0.08)
        out["strip"][crop] = strip_rows

        front_meta = next((r for r in strip_rows if r.get("price") is not None), None)
        board_q = None
        remaining = deadline - time.monotonic()
        # Prefer front contract for board stats (open / 52w); fall back to continuous.
        if front_meta and remaining > 0.4:
            board_q = _quote_one(front_meta["ticker"], timeout=min(3.5, remaining))
            if board_q is not None:
                time.sleep(0.08)
        if board_q is None and not timed_out:
            remaining = deadline - time.monotonic()
            if remaining > 0.4:
                board_q = _quote_one(TICKERS[crop], timeout=min(3.5, remaining))

        if board_q and board_q.get("price") is not None:
            row = dict(board_q)
            if front_meta:
                row["label"] = front_meta.get("label")
                row["short"] = front_meta.get("short")
                row["ticker"] = front_meta.get("ticker") or row.get("ticker")
            else:
                row["label"] = "Nearby"
                row["short"] = "front"
            out[crop] = row
        elif front_meta:
            out[crop] = {
                "price": front_meta["price"],
                "change": front_meta.get("change"),
                "open": None,
                "prev_close": None,
                "high_52w": None,
                "low_52w": None,
                "ticker": front_meta["ticker"],
                "label": front_meta["label"],
                "short": front_meta["short"],
                "source": "yahoo_delayed",
                "as_of": datetime.now(timezone.utc),
            }
        else:
            out[crop] = None
            out["error"] = out["error"] or (
                "quote fetch timed out" if timed_out else "quote fetch failed (rate limit or offline)"
            )

    if timed_out and not out["error"]:
        out["error"] = "quote fetch timed out"
    return out


def board_from_settings(settings: Any) -> dict[str, Optional[dict]]:
    """Build board display dict from stored AppSettings values."""
    if not settings:
        return {"Corn": None, "Soybeans": None}
    as_of = getattr(settings, "quote_as_of", None)
    source = getattr(settings, "quote_source", None) or "stored"
    detail: dict = {}
    raw = getattr(settings, "quote_board_json", None)
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                detail = parsed
        except (json.JSONDecodeError, TypeError):
            detail = {}
    board: dict[str, Optional[dict]] = {}
    pairs = (
        ("Corn", "corn_futures", "corn_futures_change"),
        ("Soybeans", "soy_futures", "soy_futures_change"),
    )
    for crop, price_attr, change_attr in pairs:
        price = getattr(settings, price_attr, None)
        extra = detail.get(crop) if isinstance(detail.get(crop), dict) else {}
        if price is None and not extra.get("price"):
            board[crop] = None
            continue
        use_price = float(price) if price is not None else float(extra["price"])
        board[crop] = {
            "price": use_price,
            "change": getattr(settings, change_attr, None)
            if getattr(settings, change_attr, None) is not None
            else extra.get("change"),
            "open": extra.get("open"),
            "prev_close": extra.get("prev_close"),
            "high_52w": extra.get("high_52w"),
            "low_52w": extra.get("low_52w"),
            "label": extra.get("label"),
            "short": extra.get("short"),
            "as_of": as_of,
            "source": source,
            "ticker": extra.get("ticker") or TICKERS[crop],
        }
    return board


def calendar_spreads(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Contiguous calendar spreads for a futures strip.

    Farmer / grain-desk convention (carry vs inverse):
      spread = next month − this month
      positive → carry (later months bid for storage)
      negative → inverse (market wants grain sooner)
    """
    out: list[dict[str, Any]] = []
    for i in range(len(rows) - 1):
        a = rows[i]
        b = rows[i + 1]
        pa = a.get("price")
        pb = b.get("price")
        spread = None
        if pa is not None and pb is not None:
            try:
                spread = round(float(pb) - float(pa), 4)
            except (TypeError, ValueError):
                spread = None
        out.append(
            {
                "from_label": a.get("short") or a.get("label"),
                "to_label": b.get("short") or b.get("label"),
                "from_month": a.get("month"),
                "to_month": b.get("month"),
                "spread": spread,
                "cents": round(spread * 100, 1) if spread is not None else None,
                "is_carry": spread is not None and spread > 0,
                "is_inverse": spread is not None and spread < 0,
                "is_flat": spread is not None and spread == 0,
            }
        )
    return out


def strip_spreads_map(strip: dict[str, list]) -> dict[str, list]:
    return {
        "Corn": calendar_spreads(strip.get("Corn") or []),
        "Soybeans": calendar_spreads(strip.get("Soybeans") or []),
    }


def strip_from_settings(settings: Any) -> dict[str, list]:
    """Load cached strip JSON; drop expired months; fill window to same month +1y."""
    empty = {"Corn": strip_contracts("Corn"), "Soybeans": strip_contracts("Soybeans")}
    if not settings:
        return empty
    raw = getattr(settings, "futures_strip_json", None)
    if not raw:
        return empty
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return empty
        today = date.today()
        cleaned: dict[str, list] = {"Corn": [], "Soybeans": []}
        for crop in ("Corn", "Soybeans"):
            rows = data.get(crop) or []
            have = {}
            for r in rows:
                if not isinstance(r, dict):
                    continue
                try:
                    ltd = date.fromisoformat(r.get("last_trading_day", "2099-01-01"))
                except ValueError:
                    ltd = today
                if ltd < today:
                    continue
                key = (r.get("year"), r.get("month"))
                have[key] = r
            want = strip_contracts(crop)
            merged = []
            for meta in want:
                key = (meta["year"], meta["month"])
                if key in have:
                    old = have[key]
                    merged.append(
                        {
                            **meta,
                            "price": old.get("price"),
                            "change": old.get("change"),
                        }
                    )
                else:
                    merged.append(meta)
            if merged:
                merged[0]["is_front"] = True
                for r in merged[1:]:
                    r["is_front"] = False
            cleaned[crop] = merged
        return cleaned
    except (json.JSONDecodeError, TypeError, KeyError):
        return empty


def front_strip_quote(settings: Any, crop: str) -> Optional[dict[str, Any]]:
    """First strip month that has a price — last good delayed quote we keep."""
    rows = (strip_from_settings(settings).get(crop) or []) if settings else []
    for row in rows:
        if row.get("price") is not None:
            return row
    return None


def sync_nearby_from_strip(settings: Any, *, force: bool = False) -> bool:
    """
    Copy strip front-month prices into corn_futures / soy_futures.
    force=True overwrites existing nearby values (used after a failed live refresh).
    """
    if not settings:
        return False
    updated = False
    for crop, attr, ch_attr in (
        ("Corn", "corn_futures", "corn_futures_change"),
        ("Soybeans", "soy_futures", "soy_futures_change"),
    ):
        front = front_strip_quote(settings, crop)
        if not front or front.get("price") is None:
            continue
        current = getattr(settings, attr, None)
        if force or current is None:
            setattr(settings, attr, float(front["price"]))
            if front.get("change") is not None:
                setattr(settings, ch_attr, front.get("change"))
            updated = True
    return updated


def apply_fetch_to_settings(settings: Any, fetched: dict[str, Any]) -> bool:
    """Write successful fetch into settings. Keeps prior strip prices when a month fails."""
    updated = False
    board_payload: dict[str, Any] = {}
    prev_board: dict = {}
    raw_board = getattr(settings, "quote_board_json", None)
    if raw_board:
        try:
            parsed = json.loads(raw_board)
            prev_board = parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            prev_board = {}

    for crop, attr in (("Corn", "corn_futures"), ("Soybeans", "soy_futures")):
        q = fetched.get(crop)
        if not q or q.get("price") is None:
            if isinstance(prev_board.get(crop), dict):
                board_payload[crop] = prev_board[crop]
            continue
        setattr(settings, attr, q["price"])
        ch_attr = "corn_futures_change" if crop == "Corn" else "soy_futures_change"
        setattr(settings, ch_attr, q.get("change"))
        prev = prev_board.get(crop) if isinstance(prev_board.get(crop), dict) else {}
        board_payload[crop] = {
            "price": q.get("price"),
            "change": q.get("change"),
            "open": q.get("open") if q.get("open") is not None else prev.get("open"),
            "prev_close": q.get("prev_close") if q.get("prev_close") is not None else prev.get("prev_close"),
            "high_52w": q.get("high_52w") if q.get("high_52w") is not None else prev.get("high_52w"),
            "low_52w": q.get("low_52w") if q.get("low_52w") is not None else prev.get("low_52w"),
            "ticker": q.get("ticker"),
            "label": q.get("label"),
            "short": q.get("short"),
        }
        updated = True

    if board_payload:
        settings.quote_board_json = json.dumps(board_payload)
        updated = True

    strip = fetched.get("strip") or {}
    if strip:
        prev: dict = {}
        raw = getattr(settings, "futures_strip_json", None)
        if raw:
            try:
                prev = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                prev = {}
        payload = {}
        any_price = False
        for crop, rows in strip.items():
            prev_rows = {
                (r.get("year"), r.get("month")): r
                for r in (prev.get(crop) or [])
                if isinstance(r, dict)
            }
            merged = []
            for r in rows:
                key = (r.get("year"), r.get("month"))
                price = r.get("price")
                change = r.get("change")
                if price is None and key in prev_rows and prev_rows[key].get("price") is not None:
                    price = prev_rows[key].get("price")
                    change = prev_rows[key].get("change")
                if price is not None:
                    any_price = True
                merged.append(
                    {
                        "crop": r.get("crop"),
                        "month_code": r.get("month_code"),
                        "month": r.get("month"),
                        "year": r.get("year"),
                        "label": r.get("label"),
                        "short": r.get("short"),
                        "ticker": r.get("ticker"),
                        "last_trading_day": r.get("last_trading_day"),
                        "is_front": bool(r.get("is_front")),
                        "price": price,
                        "change": change,
                    }
                )
            payload[crop] = merged
        if any_price or not getattr(settings, "futures_strip_json", None):
            settings.futures_strip_json = json.dumps(payload)
            updated = True

    # Fill any missing nearby board price from strip front after merge
    if sync_nearby_from_strip(settings, force=False):
        updated = True

    if updated:
        as_of = fetched.get("as_of") or datetime.now(timezone.utc)
        if hasattr(as_of, "tzinfo") and as_of.tzinfo is not None:
            as_of = as_of.replace(tzinfo=None)
        settings.quote_as_of = as_of
        settings.quote_source = "yahoo_delayed"
    return updated
