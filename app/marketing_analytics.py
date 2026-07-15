"""Grain marketing rankings, exposure, stress dollars, and cost of carry."""

from __future__ import annotations

from typing import Any, Optional


FUTURES_LOCK_TYPES = frozenset(
    {"hta", "forward", "cash", "minimum_price", "accumulator"}
)


def futures_locked(contract: Any) -> bool:
    t = (getattr(contract, "contract_type", None) or "").lower()
    if t in FUTURES_LOCK_TYPES:
        return True
    if t == "basis":
        return False
    return getattr(contract, "futures_price", None) is not None


def basis_locked(contract: Any) -> bool:
    t = (getattr(contract, "contract_type", None) or "").lower()
    if t == "basis":
        return True
    return getattr(contract, "basis", None) is not None


def equiv_price(contract: Any) -> Optional[float]:
    """Best available $/bu for a contract: cash, else futures+basis, else futures."""
    cash = getattr(contract, "cash_price", None)
    if cash is not None:
        return float(cash)
    fut = getattr(contract, "futures_price", None)
    bas = getattr(contract, "basis", None)
    if fut is not None and bas is not None:
        return float(fut) + float(bas)
    if fut is not None:
        return float(fut)
    return None


def bushels_left(contract: Any) -> float:
    return max(0.0, float(getattr(contract, "bushels", 0) or 0) - float(getattr(contract, "delivered_bu", 0) or 0))


def market_cash_equiv(board: Optional[float], local_basis: Optional[float]) -> Optional[float]:
    if board is None:
        return None
    return float(board) + float(local_basis or 0)


def rank_contracts(
    contracts: list[Any],
    *,
    breakeven_by_crop: dict[str, Optional[float]],
    market_by_crop: dict[str, Optional[float]],
    top_n: int = 3,
) -> dict[str, Any]:
    """Dual rankings: vs COP and vs market. Returns best/worst lists with reasons."""
    vs_cop: list[dict] = []
    vs_mkt: list[dict] = []
    for c in contracts:
        if (getattr(c, "status", "open") or "open") != "open":
            continue
        price = equiv_price(c)
        if price is None:
            continue
        crop = getattr(c, "crop", "") or ""
        be = breakeven_by_crop.get(crop)
        mkt = market_by_crop.get(crop)
        label = f"#{getattr(c, 'id', '?')} {crop} {getattr(c, 'buyer', None) or getattr(c, 'contract_type', '')}"
        if be is not None:
            edge = round(price - be, 3)
            vs_cop.append(
                {
                    "contract": c,
                    "label": label,
                    "price": price,
                    "benchmark": be,
                    "edge": edge,
                    "reason": f"${price:.2f} vs BE ${be:.2f} → {'+' if edge >= 0 else ''}{edge:.2f}/bu",
                }
            )
        if mkt is not None:
            edge = round(price - mkt, 3)
            vs_mkt.append(
                {
                    "contract": c,
                    "label": label,
                    "price": price,
                    "benchmark": mkt,
                    "edge": edge,
                    "reason": f"${price:.2f} vs mkt ${mkt:.2f} → {'+' if edge >= 0 else ''}{edge:.2f}/bu",
                }
            )

    vs_cop.sort(key=lambda r: r["edge"], reverse=True)
    vs_mkt.sort(key=lambda r: r["edge"], reverse=True)
    return {
        "cop_best": vs_cop[:top_n],
        "cop_worst": list(reversed(vs_cop[-top_n:])) if vs_cop else [],
        "market_best": vs_mkt[:top_n],
        "market_worst": list(reversed(vs_mkt[-top_n:])) if vs_mkt else [],
    }


def crop_exposure(
    contracts: list[Any],
    expected_bu: float,
    bin_bu: float,
) -> dict[str, float]:
    open_cs = [c for c in contracts if (getattr(c, "status", "open") or "open") == "open"]
    sold = sum(float(getattr(c, "bushels", 0) or 0) for c in open_cs)
    fut = sum(float(getattr(c, "bushels", 0) or 0) for c in open_cs if futures_locked(c))
    bas = sum(float(getattr(c, "bushels", 0) or 0) for c in open_cs if basis_locked(c))
    unsold = max(0.0, expected_bu - sold)
    futures_open = max(0.0, expected_bu - fut)
    basis_open = max(0.0, expected_bu - bas)
    return {
        "expected": round(expected_bu, 1),
        "sold": round(sold, 1),
        "unsold": round(unsold, 1),
        "futures_locked": round(fut, 1),
        "basis_locked": round(bas, 1),
        "futures_open": round(futures_open, 1),
        "basis_open": round(basis_open, 1),
        "bin_bu": round(bin_bu, 1),
    }


def stress_dollars(exposed_bu: float, shock_per_bu: float) -> float:
    """Dollar hit if price falls by shock_per_bu (pass positive shock magnitude)."""
    return round(max(0.0, exposed_bu) * abs(shock_per_bu), 0)


def money_at_risk(bushels: float, mark_price: Optional[float]) -> Optional[float]:
    if mark_price is None:
        return None
    return round(max(0.0, bushels) * float(mark_price), 0)


def carry_mark_price(
    futures: Optional[float],
    basis: Optional[float],
    mode: str = "cash",
) -> Optional[float]:
    """Mark for interest: futures only, or futures + local basis (cash)."""
    if futures is None:
        return None
    if (mode or "cash").lower() == "futures":
        return float(futures)
    return float(futures) + float(basis or 0)


def carry_per_bu_mo(
    mark_price: Optional[float],
    interest_apr: float,
    storage_per_bu_mo: float,
    shrink_per_bu_mo: float = 0.0,
) -> Optional[float]:
    """Interest opportunity + storage + optional shrink, $/bu/month."""
    if mark_price is None:
        interest = 0.0
    else:
        interest = float(mark_price) * (float(interest_apr) / 100.0) / 12.0
    return round(interest + float(storage_per_bu_mo or 0) + float(shrink_per_bu_mo or 0), 4)


def carry_farm_mo(bin_bu: float, rate_per_bu_mo: Optional[float]) -> Optional[float]:
    if rate_per_bu_mo is None:
        return None
    return round(max(0.0, bin_bu) * float(rate_per_bu_mo), 2)


def build_carry_snapshot(
    settings: Any,
    board: dict[str, Any],
    bin_bu: dict[str, float],
    market_fallback: Optional[dict[str, Optional[float]]] = None,
) -> dict[str, Any]:
    """Full by-crop carry rates + farm monthly $ for headers and bins."""
    market_fallback = market_fallback or {}
    interest = (getattr(settings, "carry_interest_apr", None) if settings else 7.0) or 7.0
    mode = (getattr(settings, "carry_mark_mode", None) if settings else "cash") or "cash"
    carry: dict[str, Any] = {
        "by_crop": {},
        "farm_total_mo": 0.0,
        "interest_apr": interest,
        "mark_mode": mode,
    }
    for crop in ("Corn", "Soybeans"):
        storage = 0.03 if crop == "Corn" else 0.04
        shrink = 0.003 if crop == "Corn" else 0.004
        basis = None
        if settings:
            if crop == "Corn":
                storage = settings.corn_storage_per_bu_mo if settings.corn_storage_per_bu_mo is not None else 0.03
                shrink = settings.corn_shrink_per_bu_mo if settings.corn_shrink_per_bu_mo is not None else 0.003
                basis = settings.corn_local_basis
            else:
                storage = settings.soy_storage_per_bu_mo if settings.soy_storage_per_bu_mo is not None else 0.04
                shrink = settings.soy_shrink_per_bu_mo if settings.soy_shrink_per_bu_mo is not None else 0.004
                basis = settings.soy_local_basis
        futures = None
        if board.get(crop) and board[crop].get("price") is not None:
            futures = board[crop]["price"]
        elif market_fallback.get(crop) is not None:
            futures = market_fallback[crop]
        mark = carry_mark_price(futures, basis, mode)
        interest_mo = (
            round(float(mark) * (float(interest) / 100.0) / 12.0, 4) if mark is not None else 0.0
        )
        rate = carry_per_bu_mo(mark, interest, storage, shrink)
        farm_mo = carry_farm_mo(bin_bu.get(crop) or 0, rate)
        carry["by_crop"][crop] = {
            "futures": futures,
            "basis": basis,
            "mark": mark,
            "storage": storage,
            "shrink": shrink,
            "interest_mo": interest_mo,
            "rate": rate,
            "bin_bu": bin_bu.get(crop) or 0,
            "farm_mo": farm_mo,
        }
        if farm_mo is not None:
            carry["farm_total_mo"] += farm_mo
    carry["farm_total_mo"] = round(carry["farm_total_mo"], 2)
    return carry


def desk_rows(
    contracts: list[Any],
    events_by: dict[int, list],
    breakeven_by_crop: dict[str, Optional[float]],
    market_by_crop: dict[str, Optional[float]],
) -> list[dict]:
    rows = []
    for c in contracts:
        price = equiv_price(c)
        crop = getattr(c, "crop", "") or ""
        be = breakeven_by_crop.get(crop)
        mkt = market_by_crop.get(crop)
        left = bushels_left(c)
        rows.append(
            {
                "c": c,
                "left": left,
                "equiv": price,
                "vs_cop": round(price - be, 3) if price is not None and be is not None else None,
                "vs_mkt": round(price - mkt, 3) if price is not None and mkt is not None else None,
                "events": (events_by.get(getattr(c, "id"), []) or [])[:3],
                "futures_ok": futures_locked(c),
                "basis_ok": basis_locked(c),
            }
        )
    return rows
