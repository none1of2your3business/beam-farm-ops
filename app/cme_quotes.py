"""Delayed CME corn/soy futures via Yahoo Finance (informational only).

Nearby continuous (ZC=F / ZS=F) plus a rolling ~12-month strip of listed
contract months (e.g. Jul → Jul next year). Front month drops after last
trading day; the next listed month becomes the new front.
"""

from __future__ import annotations

import json
import re
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


_FULL_MONTH_NAMES = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def _month_num_from_code(code: str, crop: str) -> Optional[int]:
    letter = (code or "").strip().upper()
    if not letter:
        return None
    pairs = CORN_MONTHS if crop == "Corn" else SOY_MONTHS if crop == "Soybeans" else CORN_MONTHS + SOY_MONTHS
    for c, m in pairs:
        if c == letter:
            return m
    for c, m in CORN_MONTHS + SOY_MONTHS:
        if c == letter:
            return m
    return None


def crop_desk_order(crop: str) -> int:
    if crop == "Corn":
        return 0
    if crop == "Soybeans":
        return 1
    return 2


def futures_month_expiry(crop: str, raw: Optional[str]) -> Optional[date]:
    """Last trading day for a stored futures month label (desk sort / display)."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None

    m = re.match(r"^([A-Z])\s+(\w+)\s+'(\d{2})$", s)
    if m:
        letter, month_word, yy = m.group(1), m.group(2).lower(), int(m.group(3))
        month_num = _FULL_MONTH_NAMES.get(month_word)
        if month_num is None:
            month_num = _month_num_from_code(letter, crop)
        if month_num:
            return last_trading_day(2000 + yy, month_num)

    m2 = re.match(r"^([A-Z])\s+'(\d{2})$", s)
    if m2:
        letter, yy = m2.group(1), int(m2.group(2))
        month_num = _month_num_from_code(letter, crop)
        if month_num:
            return last_trading_day(2000 + yy, month_num)

    m3 = re.match(r"^([A-Z])\s*(\d{2})$", s.replace("'", "").replace(" ", ""))
    if m3:
        letter, yy = m3.group(1), int(m3.group(2))
        month_num = _month_num_from_code(letter, crop)
        if month_num:
            return last_trading_day(2000 + yy, month_num)

    return None


def futures_month_choices(crop: str, as_of: Optional[date] = None) -> list[dict[str, str]]:
    """Dropdown options: CME month letter + full month name + year (e.g. Z December '26)."""
    full_names = {
        1: "January",
        2: "February",
        3: "March",
        4: "April",
        5: "May",
        6: "June",
        7: "July",
        8: "August",
        9: "September",
        10: "October",
        11: "November",
        12: "December",
    }
    out: list[dict[str, str]] = []
    for row in strip_contracts(crop, as_of=as_of):
        code = row["month_code"]
        name = full_names.get(row["month"], MONTH_NAMES.get(row["month"], ""))
        yy = yy_short(row["year"])
        label = f"{code} {name} '{yy}"
        out.append({"value": label, "label": label, "crop": crop, "short": row["short"]})
    return out


def strip_contracts(
    crop: str,
    as_of: Optional[date] = None,
    horizon_months: int = 12,
) -> list[dict[str, Any]]:
    """
    Active listed contracts from the current unexpired front month through
    the same calendar month one year later by default (e.g. Jul'26 … Jul'27).
    horizon_months=18 covers ~1.5 years of deferreds. Front rolls after last trade day.
    """
    as_of = as_of or date.today()
    months = CORN_MONTHS if crop == "Corn" else SOY_MONTHS
    root = "ZC" if crop == "Corn" else "ZS"
    horizon = max(1, int(horizon_months or 12))

    candidates: list[dict[str, Any]] = []
    for y in range(as_of.year - 1, as_of.year + 4):
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
    end_ym = front["year"] * 12 + front["month"] + horizon

    out: list[dict[str, Any]] = []
    for c in candidates:
        if c["year"] * 12 + c["month"] <= end_ym:
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


_YAHOO_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def _yahoo_chart_hosts() -> list[str]:
    return [
        "https://query2.finance.yahoo.com",
        "https://query1.finance.yahoo.com",
    ]


def _ticker_candidates(ticker: str) -> list[str]:
    """Yahoo sometimes wants ZCU26.CBT, sometimes ZCU26."""
    t = (ticker or "").strip()
    if not t:
        return []
    out = [t]
    if t.endswith(".CBT"):
        out.append(t[: -len(".CBT")])
    elif "=" not in t and "." not in t:
        out.append(f"{t}.CBT")
    # unique preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def _parse_chart_payload(data: dict[str, Any], ticker: str) -> Optional[dict[str, Any]]:
    try:
        result = (((data or {}).get("chart") or {}).get("result") or [None])[0]
        if not result:
            return None
        meta = result.get("meta") or {}
        last = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        open_px = meta.get("regularMarketOpen") or meta.get("open")
        high_52 = meta.get("fiftyTwoWeekHigh")
        low_52 = meta.get("fiftyTwoWeekLow")

        # Fill gaps from daily bars when meta is thin
        try:
            quote = ((result.get("indicators") or {}).get("quote") or [None])[0] or {}
            closes = [c for c in (quote.get("close") or []) if c is not None]
            opens = [c for c in (quote.get("open") or []) if c is not None]
            highs = [c for c in (quote.get("high") or []) if c is not None]
            lows = [c for c in (quote.get("low") or []) if c is not None]
            if last is None and closes:
                last = closes[-1]
            if prev is None and len(closes) > 1:
                prev = closes[-2]
            if open_px is None and opens:
                open_px = opens[-1]
            if high_52 is None and highs:
                high_52 = max(highs)
            if low_52 is None and lows:
                low_52 = min(lows)
        except Exception:
            pass

        if last is None:
            return None

        # USX = cents/bu for CBOT grains; USD already $/bu
        currency = (meta.get("currency") or "").upper()
        raw_last = float(last)
        if currency == "USX":
            price = round(raw_last / 100.0, 4)
            prev_d = round(float(prev) / 100.0, 4) if prev is not None else None
            open_d = round(float(open_px) / 100.0, 4) if open_px is not None else None
            high_d = round(float(high_52) / 100.0, 4) if high_52 is not None else None
            low_d = round(float(low_52) / 100.0, 4) if low_52 is not None else None
        else:
            price = _to_dollars_per_bu(raw_last)
            prev_d = _to_dollars_per_bu(float(prev)) if prev is not None else None
            open_d = _to_dollars_per_bu(float(open_px)) if open_px is not None else None
            high_d = _to_dollars_per_bu(float(high_52)) if high_52 is not None else None
            low_d = _to_dollars_per_bu(float(low_52)) if low_52 is not None else None

        change = None
        if prev_d is not None:
            change = round(price - prev_d, 4)

        return {
            "price": price,
            "change": change,
            "open": open_d,
            "prev_close": prev_d,
            "high_52w": high_d,
            "low_52w": low_d,
            "ticker": ticker,
            "source": "yahoo_delayed",
            "as_of": datetime.now(timezone.utc),
            "currency": currency or None,
        }
    except Exception:
        return None


def _http_get_json(url: str, *, timeout: float = 20.0) -> tuple[int, Any]:
    """GET JSON. Prefer curl_cffi (Chrome TLS) — plain httpx often gets Yahoo 429s."""
    headers = {
        "User-Agent": _YAHOO_UA,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        from curl_cffi import requests as creq

        r = creq.get(url, headers=headers, impersonate="chrome", timeout=timeout)
        status = int(getattr(r, "status_code", 0) or 0)
        if status != 200:
            return status, None
        try:
            return status, r.json()
        except Exception:
            return status, None
    except Exception:
        pass
    try:
        import httpx

        with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
            r = client.get(url)
            if r.status_code != 200:
                return r.status_code, None
            return r.status_code, r.json()
    except Exception:
        pass
    try:
        import urllib.request

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            status = getattr(resp, "status", 200) or 200
            body = resp.read().decode("utf-8", errors="ignore")
            if status != 200:
                return status, None
            return status, json.loads(body)
    except Exception:
        return 0, None


def _parse_yahoo_quote_html(html: str, ticker: str) -> Optional[dict[str, Any]]:
    """Parse finance.yahoo.com quote page when the chart API is rate-limited."""
    if not html:
        return None
    # Primary price: <… data-testid="qsp-price">473.00 </span>
    m = re.search(
        r'data-testid="qsp-price"[^>]*>\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*<',
        html,
    )
    if not m:
        m = re.search(r'data-testid="qsp-price">\s*([0-9][0-9,]*(?:\.[0-9]+)?)', html)
    if not m:
        return None
    try:
        raw_last = float(m.group(1).replace(",", ""))
    except ValueError:
        return None

    change_raw = None
    mc = re.search(
        r'data-testid="qsp-price-change"[^>]*>\s*([+\-]?[0-9][0-9,]*(?:\.[0-9]+)?)\s*<',
        html,
    )
    if mc:
        try:
            change_raw = float(mc.group(1).replace(",", ""))
        except ValueError:
            change_raw = None

    # CBOT grains on Yahoo HTML are in cents/bu (e.g. 473.00 corn, 1217.25 soy)
    if raw_last > 40:
        price = round(raw_last / 100.0, 4)
        change = round(change_raw / 100.0, 4) if change_raw is not None else None
    else:
        price = round(raw_last, 4)
        change = round(change_raw, 4) if change_raw is not None else None

    prev = round(price - change, 4) if change is not None else None
    return {
        "price": price,
        "change": change,
        "open": None,
        "prev_close": prev,
        "high_52w": None,
        "low_52w": None,
        "ticker": ticker,
        "source": "yahoo_delayed",
        "as_of": datetime.now(timezone.utc),
    }


def _quote_from_yahoo_html(ticker: str) -> Optional[dict[str, Any]]:
    from urllib.parse import quote

    headers = {
        "User-Agent": _YAHOO_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    for cand in _ticker_candidates(ticker):
        enc = quote(cand, safe="")
        url = f"https://finance.yahoo.com/quote/{enc}/"
        try:
            from curl_cffi import requests as creq

            r = creq.get(url, headers=headers, impersonate="chrome", timeout=28.0)
            if int(getattr(r, "status_code", 0) or 0) == 200:
                parsed = _parse_yahoo_quote_html(getattr(r, "text", "") or "", cand)
                if parsed:
                    return parsed
        except Exception:
            pass
        try:
            import httpx

            with httpx.Client(headers=headers, timeout=28.0, follow_redirects=True) as client:
                r = client.get(url)
                if r.status_code == 200:
                    parsed = _parse_yahoo_quote_html(r.text, cand)
                    if parsed:
                        return parsed
        except Exception:
            pass
        try:
            import urllib.request

            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=28) as resp:  # noqa: S310
                body = resp.read().decode("utf-8", errors="ignore")
                parsed = _parse_yahoo_quote_html(body, cand)
                if parsed:
                    return parsed
        except Exception:
            continue
    return None


def _quote_one_impl(ticker: str) -> Optional[dict[str, Any]]:
    """Fetch one delayed Yahoo futures quote via chart API (+ HTML fallback)."""
    from urllib.parse import quote

    saw_rate_limit = False
    for cand in _ticker_candidates(ticker):
        enc = quote(cand, safe="")
        for host in _yahoo_chart_hosts():
            url = f"{host}/v8/finance/chart/{enc}?interval=1d&range=10d"
            status, data = _http_get_json(url, timeout=14.0)
            if status == 200 and data:
                parsed = _parse_chart_payload(data, cand)
                if parsed:
                    return parsed
            if status == 429:
                saw_rate_limit = True
                time.sleep(0.35)
                continue
            if status in (500, 502, 503, 504, 0):
                time.sleep(0.2)
                continue

    # HTML fallback — only some deferred months are listed on Yahoo's HTML pages
    html_q = _quote_from_yahoo_html(ticker)
    if html_q:
        return html_q

    if not saw_rate_limit:
        try:
            import yfinance as yf

            t = yf.Ticker(ticker)
            hist = t.history(period="5d")
            if hist is not None and len(hist) > 0:
                last = float(hist["Close"].iloc[-1])
                prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else None
                open_px = float(hist["Open"].iloc[-1]) if "Open" in hist.columns else None
                price = _to_dollars_per_bu(last)
                return {
                    "price": price,
                    "change": round(price - _to_dollars_per_bu(float(prev)), 4) if prev is not None else None,
                    "open": _to_dollars_per_bu(float(open_px)) if open_px is not None else None,
                    "prev_close": _to_dollars_per_bu(float(prev)) if prev is not None else None,
                    "high_52w": None,
                    "low_52w": None,
                    "ticker": ticker,
                    "source": "yahoo_delayed",
                    "as_of": datetime.now(timezone.utc),
                }
        except Exception:
            return None
    return None


def _quote_one(ticker: str, timeout: float = 25.0) -> Optional[dict[str, Any]]:
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


def _board_from_front_row(front: dict[str, Any], fallback: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Top-of-board quote is always the current front strip month (auto-rolls)."""
    base = dict(fallback or {})
    return {
        "price": front.get("price") if front.get("price") is not None else base.get("price"),
        "change": front.get("change") if front.get("change") is not None else base.get("change"),
        "open": front.get("open") if front.get("open") is not None else base.get("open"),
        "prev_close": front.get("prev_close") if front.get("prev_close") is not None else base.get("prev_close"),
        "high_52w": front.get("high_52w") if front.get("high_52w") is not None else base.get("high_52w"),
        "low_52w": front.get("low_52w") if front.get("low_52w") is not None else base.get("low_52w"),
        "ticker": front.get("ticker") or base.get("ticker"),
        "label": front.get("label") or base.get("label"),
        "short": front.get("short") or base.get("short"),
        "source": "yahoo_delayed",
        "as_of": base.get("as_of") or datetime.now(timezone.utc),
    }


def fetch_nearby_quotes(max_seconds: float = 90.0) -> dict[str, Any]:
    """Front-month board + full ~12-month strip for Corn and Soybeans.

    Strip is the source of truth for the board: after each listed month's last
    trading day, ``strip_contracts`` rolls the window and the new front month
    becomes the top board quote automatically.
    """
    out: dict[str, Any] = {
        "error": None,
        "as_of": datetime.now(timezone.utc),
        "strip": {"Corn": [], "Soybeans": []},
        "Corn": None,
        "Soybeans": None,
    }

    deadline = time.monotonic() + max(20.0, float(max_seconds or 90.0))
    timed_out = False
    any_ok = False
    incomplete = False

    # Build work list: every strip month for both crops (front first within each crop)
    jobs: list[tuple[str, dict[str, Any]]] = []
    for crop in ("Corn", "Soybeans"):
        for meta in strip_contracts(crop):
            jobs.append((crop, meta))

    # Fetch strip months sequentially with a short pause (curl_cffi handles Yahoo TLS)
    by_crop: dict[str, list[dict[str, Any]]] = {"Corn": [], "Soybeans": []}
    for crop, meta in jobs:
        remaining = deadline - time.monotonic()
        if remaining <= 0.4:
            timed_out = True
            incomplete = True
            by_crop[crop].append({**meta, "price": None, "change": None})
            continue
        q = _quote_one(meta["ticker"], timeout=min(16.0, max(4.0, remaining)))
        row = {
            **meta,
            "price": q["price"] if q else None,
            "change": q["change"] if q else None,
            "open": q.get("open") if q else None,
            "prev_close": q.get("prev_close") if q else None,
            "high_52w": q.get("high_52w") if q else None,
            "low_52w": q.get("low_52w") if q else None,
        }
        by_crop[crop].append(row)
        if q is not None:
            any_ok = True
            time.sleep(0.18)
        else:
            incomplete = True
            time.sleep(0.12)

    out["strip"] = by_crop

    # Board = current front month from the strip (not Yahoo continuous, which can lag)
    for crop in ("Corn", "Soybeans"):
        rows = by_crop.get(crop) or []
        front = next((r for r in rows if r.get("is_front")), rows[0] if rows else None)
        if front and front.get("price") is not None:
            out[crop] = _board_from_front_row(front)
            any_ok = True
        else:
            # Fallback: continuous nearby if front month quote failed
            remaining = deadline - time.monotonic()
            cont = None
            if remaining > 2.0:
                cont = _quote_one(TICKERS[crop], timeout=min(14.0, remaining))
            if cont and cont.get("price") is not None:
                label = (front or {}).get("label") or "Nearby"
                short = (front or {}).get("short") or "front"
                ticker = (front or {}).get("ticker") or cont.get("ticker")
                out[crop] = {
                    **cont,
                    "label": label,
                    "short": short,
                    "ticker": ticker,
                }
                any_ok = True
            else:
                out[crop] = None
                if front:
                    incomplete = True

    if not any_ok:
        out["error"] = (
            "quote fetch timed out"
            if timed_out
            else "quote fetch failed (Yahoo rate limit or offline) — wait a minute and try Refresh again"
        )
    elif (timed_out or incomplete) and not out["error"]:
        priced = sum(
            1
            for crop in ("Corn", "Soybeans")
            for r in (out["strip"].get(crop) or [])
            if r.get("price") is not None
        )
        total = sum(len(out["strip"].get(crop) or []) for crop in ("Corn", "Soybeans"))
        if priced < total:
            out["error"] = f"partial strip ({priced}/{total} months) — hit Refresh again to fill gaps"
    return out


def _weekly_history(ticker: str) -> list[dict[str, Any]]:
    """18 months of weekly nearby closes from Yahoo chart (delayed)."""
    import urllib.request

    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        "?interval=1wk&range=18mo"
    )
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 BeamFarmOps/hold-sell"}
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
        res = (data.get("chart") or {}).get("result") or []
        if not res:
            return []
        row = res[0]
        ts = row.get("timestamp") or []
        closes = ((row.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        out: list[dict[str, Any]] = []
        for t, close in zip(ts, closes):
            if close is None:
                continue
            day = datetime.fromtimestamp(int(t), tz=timezone.utc).date().isoformat()
            out.append({"date": day, "price": _to_dollars_per_bu(float(close))})
        return out
    except Exception:
        return []


def fetch_hold_sell_quotes(max_seconds: float = 45.0) -> dict[str, Any]:
    """~1.5 year listed strip plus 18 months of weekly nearby history."""
    out: dict[str, Any] = {
        "error": None,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "source": "yahoo_delayed",
        "strip": {"Corn": [], "Soybeans": []},
        "history": {"Corn": [], "Soybeans": []},
    }
    deadline = time.monotonic() + max(8.0, float(max_seconds or 45.0))
    timed_out = False
    any_price = False
    for crop in ("Corn", "Soybeans"):
        rows: list[dict[str, Any]] = []
        for meta in strip_contracts(crop, horizon_months=18):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                rows.append({**meta, "price": None, "change": None})
                continue
            q = _quote_one(meta["ticker"], timeout=min(8.0, remaining))
            price = q["price"] if q else None
            change = q["change"] if q else None
            if price is not None:
                any_price = True
            rows.append({**meta, "price": price, "change": change})
            time.sleep(0.12 if q is not None else 0.08)
        out["strip"][crop] = rows

    remaining = deadline - time.monotonic()
    if remaining > 1.5:
        out["history"]["Corn"] = _weekly_history(TICKERS["Corn"])
        out["history"]["Soybeans"] = _weekly_history(TICKERS["Soybeans"])

    if timed_out and not any_price:
        out["error"] = "quote fetch timed out"
    elif not any_price:
        out["error"] = "quote fetch failed (rate limit or offline)"
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
        months = None
        try:
            y1, m1 = int(a.get("year")), int(a.get("month"))
            y2, m2 = int(b.get("year")), int(b.get("month"))
            months = (y2 - y1) * 12 + (m2 - m1)
            if months <= 0:
                months = None
        except (TypeError, ValueError):
            months = None
        out.append(
            {
                "from_label": a.get("short") or a.get("label"),
                "to_label": b.get("short") or b.get("label"),
                "from_month": a.get("month"),
                "to_month": b.get("month"),
                "from_year": a.get("year"),
                "to_year": b.get("year"),
                "months": months,
                "spread": spread,
                "cents": round(spread * 100, 1) if spread is not None else None,
                "is_carry": spread is not None and spread > 0,
                "is_inverse": spread is not None and spread < 0,
                "is_flat": spread is not None and spread == 0,
                "carry_cost_cents": None,
                "net_cents": None,
                "rate_per_bu_mo": None,
            }
        )
    return out


def apply_carry_costs_to_spreads(
    spreads: list[dict[str, Any]],
    rate_per_bu_mo: Optional[float],
) -> list[dict[str, Any]]:
    """Attach farm carry cost (¢/bu) and net spread after carry for each link."""
    rate = float(rate_per_bu_mo) if rate_per_bu_mo is not None else None
    for sp in spreads:
        sp["rate_per_bu_mo"] = rate
        months = sp.get("months")
        cents = sp.get("cents")
        if rate is None or months is None or months <= 0:
            sp["carry_cost_cents"] = None
            sp["net_cents"] = None
            continue
        # rate is $/bu/mo → cents/bu for the months to the next listed contract
        cost = round(rate * float(months) * 100.0, 1)
        sp["carry_cost_cents"] = cost
        sp["net_cents"] = round(float(cents) - cost, 1) if cents is not None else None
    return spreads


def strip_spreads_map(
    strip: dict[str, list],
    rates_by_crop: Optional[dict[str, Optional[float]]] = None,
) -> dict[str, list]:
    rates_by_crop = rates_by_crop or {}
    out: dict[str, list] = {}
    for crop in ("Corn", "Soybeans"):
        spreads = calendar_spreads(strip.get(crop) or [])
        apply_carry_costs_to_spreads(spreads, rates_by_crop.get(crop))
        out[crop] = spreads
    return out


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
                    merged.append({**meta, "price": None, "change": None})
            if merged:
                merged[0]["is_front"] = True
                for r in merged[1:]:
                    r["is_front"] = False
            cleaned[crop] = merged
        return cleaned
    except (json.JSONDecodeError, TypeError, KeyError):
        return empty


def front_strip_quote(settings: Any, crop: str) -> Optional[dict[str, Any]]:
    """Current front strip month with a price (falls back to first priced month)."""
    rows = (strip_from_settings(settings).get(crop) or []) if settings else []
    for row in rows:
        if row.get("is_front") and row.get("price") is not None:
            return row
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
