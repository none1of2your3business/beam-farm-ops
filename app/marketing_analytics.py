"""Grain marketing rankings, exposure, stress dollars, and cost of carry."""

from __future__ import annotations

from typing import Any, Optional


def normalize_contract_type(contract: Any) -> str:
    """Normalize stored type labels for futures/basis sold rules."""
    raw = (getattr(contract, "contract_type", None) or "").strip().lower()
    key = "".join(ch if ch.isalnum() else "_" for ch in raw)
    while "__" in key:
        key = key.replace("__", "_")
    key = key.strip("_")
    aliases = {
        "delayed_price": "dp",
        "delayedpricing": "dp",
        "dp": "dp",
        "basis": "basis",
        "no_basis_established": "basis",
        "nobasisestablished": "basis",
        "cash": "cash",
        "cash_sale": "cash",
        "minimum_price": "minimum_price",
        "min_price": "minimum_price",
        "minprice": "minimum_price",
    }
    return aliases.get(key, key)


def futures_locked(contract: Any) -> bool:
    """Bushels that count as futures sold.

    Rules:
    - Cash → yes (also basis)
    - Basis → no
    - DP → no
    - Everything else → yes
    """
    t = normalize_contract_type(contract)
    if t in ("dp", "basis"):
        return False
    return True


def basis_locked(contract: Any) -> bool:
    """Bushels that count as basis sold.

    Rules:
    - Cash → yes (also futures)
    - Basis → yes
    - DP → no
    - Everything else → no
    """
    t = normalize_contract_type(contract)
    return t in ("cash", "basis")


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


def carry_per_bu_day(rate_per_bu_mo: Optional[float]) -> Optional[float]:
    """Convert monthly $/bu rate to a daily rate (30.437-day month)."""
    if rate_per_bu_mo is None:
        return None
    return float(rate_per_bu_mo) / 30.437


def carry_accrued(
    bu: float,
    rate_per_bu_mo: Optional[float],
    days: int,
) -> Optional[float]:
    """Accumulated storage cost: current bu × daily rate × days since carry start."""
    if rate_per_bu_mo is None or bu <= 0 or days <= 0:
        return 0.0 if bu <= 0 else None
    daily = carry_per_bu_day(rate_per_bu_mo)
    if daily is None:
        return None
    return round(max(0.0, bu) * daily * max(0, int(days)), 2)


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


def contract_desk_sort_key(contract: Any) -> tuple:
    from app import cme_quotes

    crop = getattr(contract, "crop", "") or ""
    exp = cme_quotes.futures_month_expiry(crop, getattr(contract, "futures_month", None))
    exp_ord = exp.toordinal() if exp else 99999999
    return (cme_quotes.crop_desk_order(crop), exp_ord, getattr(contract, "id", 0))


def _contract_short_label(c: Any) -> str:
    num = getattr(c, "number_label", None) or getattr(c, "contract_number", None) or f"#{getattr(c, 'id', '?')}"
    buyer = getattr(c, "buyer", None) or getattr(c, "contract_type", "") or ""
    month = getattr(c, "futures_month", None) or ""
    parts = [str(num)]
    if buyer:
        parts.append(str(buyer))
    if month:
        parts.append(str(month))
    return " · ".join(parts)


def desk_analysis(
    contracts: list[Any],
    *,
    expected_by_crop: dict[str, float],
    futures_by_crop: dict[str, Optional[float]],
    market_by_crop: dict[str, Optional[float]],
    breakeven_by_crop: dict[str, Optional[float]],
    crops: tuple[str, ...] = ("Corn", "Soybeans"),
) -> list[dict[str, Any]]:
    """Per-crop marketing snapshot for the contract desk analysis panel."""
    by_crop: dict[str, list[Any]] = {crop: [] for crop in crops}
    for c in contracts:
        crop = getattr(c, "crop", "") or ""
        if crop in by_crop:
            by_crop[crop].append(c)

    out: list[dict[str, Any]] = []
    for crop in crops:
        items = by_crop.get(crop) or []
        open_items = [c for c in items if (getattr(c, "status", "open") or "open") == "open"]
        priced: list[tuple[Any, float, float]] = []
        for c in open_items:
            px = equiv_price(c)
            bu = float(getattr(c, "bushels", 0) or 0)
            if px is None or bu <= 0:
                continue
            priced.append((c, px, bu))

        sold_bu = sum(float(getattr(c, "bushels", 0) or 0) for c in open_items)
        fut_bu = sum(float(getattr(c, "bushels", 0) or 0) for c in open_items if futures_locked(c))
        bas_bu = sum(float(getattr(c, "bushels", 0) or 0) for c in open_items if basis_locked(c))
        expected = float(expected_by_crop.get(crop) or 0)
        unsold = max(0.0, expected - sold_bu) if expected > 0 else 0.0

        total_bu = sum(bu for _c, _px, bu in priced)
        avg = round(sum(px * bu for _c, px, bu in priced) / total_bu, 4) if total_bu > 0 else None

        futures = futures_by_crop.get(crop)
        market = market_by_crop.get(crop)
        be = breakeven_by_crop.get(crop)
        vs_fut = round(avg - float(futures), 3) if avg is not None and futures is not None else None
        vs_mkt = round(avg - float(market), 3) if avg is not None and market is not None else None
        vs_cop = round(avg - float(be), 3) if avg is not None and be is not None else None

        best = max(priced, key=lambda t: t[1]) if priced else None
        worst = min(priced, key=lambda t: t[1]) if priced else None
        if best and worst and getattr(best[0], "id", None) == getattr(worst[0], "id", None):
            # Single priced contract — show as best only
            worst = None
            spread = 0.0
        else:
            spread = round(best[1] - worst[1], 3) if best and worst else None

        basis_vals: list[tuple[float, float]] = []
        for c in open_items:
            bas = getattr(c, "basis", None)
            bu = float(getattr(c, "bushels", 0) or 0)
            if bas is None or bu <= 0:
                continue
            basis_vals.append((float(bas), bu))
        bas_total = sum(bu for _b, bu in basis_vals)
        avg_basis = (
            round(sum(b * bu for b, bu in basis_vals) / bas_total, 4) if bas_total > 0 else None
        )

        missing_month = sum(
            1
            for c in open_items
            if futures_locked(c) and not (getattr(c, "futures_month", None) or "").strip()
        )
        unpriced = sum(1 for c in open_items if equiv_price(c) is None)

        sold_pct = round(100 * sold_bu / expected, 1) if expected > 0 else None
        fut_pct = round(100 * fut_bu / expected, 1) if expected > 0 else None
        bas_pct = round(100 * bas_bu / expected, 1) if expected > 0 else None

        tip = None
        if avg is None:
            tip = "Add prices on open contracts to unlock averages and rankings."
        elif vs_fut is not None and vs_fut < -0.05:
            tip = "Booked average is under the board — unsold bushels can still lift the farm average if the market rallies, or lock in if you need floor protection."
        elif vs_fut is not None and vs_fut > 0.05:
            tip = "You're ahead of the board on sold grain — protect the edge; don't let open bushels give it back."
        elif sold_pct is not None and sold_pct < 30 and expected > 0:
            tip = "Light sales vs expected production — decide intentionally (scale-up plan) instead of waiting by default."
        elif bas_pct is not None and fut_pct is not None and fut_pct - bas_pct > 25:
            tip = "Futures are more locked than basis — watch local basis; a weak basis can erase a good futures sale."
        elif spread is not None and spread >= 0.25:
            tip = f"${spread:.2f}/bu separates your best and worst sales — timing and type matter; review what worked."
        elif vs_cop is not None and vs_cop < 0:
            tip = "Average sale is below COP breakeven — focus remaining sales on covering cost, not chasing highs."
        else:
            tip = "Keep futures and basis decisions separate: know what each contract locked, and what is still open."

        def _pick(row: Optional[tuple[Any, float, float]]) -> Optional[dict[str, Any]]:
            if not row:
                return None
            c, px, bu = row
            return {
                "label": _contract_short_label(c),
                "price": px,
                "bushels": bu,
                "id": getattr(c, "id", None),
                "vs_fut": round(px - float(futures), 3) if futures is not None else None,
                "vs_mkt": round(px - float(market), 3) if market is not None else None,
            }

        out.append(
            {
                "crop": crop,
                "open_count": len(open_items),
                "priced_count": len(priced),
                "sold_bu": round(sold_bu, 1),
                "expected_bu": round(expected, 1),
                "unsold_bu": round(unsold, 1),
                "sold_pct": sold_pct,
                "futures_locked_bu": round(fut_bu, 1),
                "basis_locked_bu": round(bas_bu, 1),
                "futures_pct": fut_pct,
                "basis_pct": bas_pct,
                "avg_price": avg,
                "futures": float(futures) if futures is not None else None,
                "market": float(market) if market is not None else None,
                "breakeven": float(be) if be is not None else None,
                "vs_futures": vs_fut,
                "vs_market": vs_mkt,
                "vs_cop": vs_cop,
                "best": _pick(best),
                "worst": _pick(worst),
                "spread": spread,
                "avg_basis": avg_basis,
                "missing_futures_month": missing_month,
                "unpriced_count": unpriced,
                "tip": tip,
            }
        )
    return out


def _field_share_rows(field: Any) -> list[Any]:
    """Real FieldShare rows, or a legacy Me + party synthesis when shares are empty."""
    shares = list(getattr(field, "shares", None) or [])
    if shares:
        return shares

    class _Synth:
        def __init__(self, *, is_me: int, party_id, partner_name: str, share_pct: float, sort_order: int, party=None):
            self.is_me = is_me
            self.party_id = party_id
            self.partner_name = partner_name
            self.share_pct = share_pct
            self.sort_order = sort_order
            self.party = party
            self.id = 0

        @property
        def display_name(self) -> str:
            if self.is_me:
                return self.partner_name.strip() or "Me"
            if self.party is not None and getattr(self.party, "name", None):
                return self.party.name
            return self.partner_name.strip() or "Partner"

    rows: list[Any] = []
    my_pct = getattr(field, "my_share_pct", None)
    mode = getattr(field, "ownership_mode", None) or ""
    party = getattr(field, "party", None)
    party_id = getattr(field, "party_id", None)
    if my_pct is not None or mode == "on_shares" or party_id:
        rows.append(
            _Synth(
                is_me=1,
                party_id=None,
                partner_name="Me",
                share_pct=float(my_pct) if my_pct is not None else 100.0,
                sort_order=0,
            )
        )
    if party_id:
        other = max(0.0, 100.0 - float(my_pct)) if my_pct is not None else 0.0
        rows.append(
            _Synth(
                is_me=0,
                party_id=party_id,
                partner_name=getattr(party, "name", None) or "Partner",
                share_pct=other,
                sort_order=1,
                party=party,
            )
        )
    if not rows:
        rows.append(_Synth(is_me=1, party_id=None, partner_name="Me", share_pct=100.0, sort_order=0))
    return rows


def _party_name_tokens(name: str) -> list[str]:
    import re

    return [t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if len(t) >= 4]


def match_party_for_field_name(field_name: str, parties: list[Any]) -> Any | None:
    """Unique party whose distinctive name token appears in the field name.

    Example: field "Simpson Even" + party "Ed Simpson" → match on "simpson".
    """
    fname = (field_name or "").lower()
    if not fname or not parties:
        return None
    scored: list[tuple[int, int, Any]] = []
    for party in parties:
        tokens = _party_name_tokens(getattr(party, "name", "") or "")
        hits = [t for t in tokens if t in fname]
        if not hits:
            continue
        scored.append((sum(len(t) for t in hits), max(len(t) for t in hits), party))
    if not scored:
        return None
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    best = scored[0]
    # Ambiguous if another party ties the top score
    for other in scored[1:]:
        if other[0] == best[0] and other[1] == best[1]:
            oid = getattr(other[2], "id", None)
            bid = getattr(best[2], "id", None)
            if oid != bid:
                return None
    return best[2]


def resolve_field_partners(
    field: Any,
    parties: list[Any] | None = None,
) -> tuple[list[Any], list[Any], bool]:
    """Return (partners, all_share_rows, matched_by_name).

    If the field is on shares but has no partner linked, try matching a party
    from the field name (Simpson Odd → Ed Simpson).
    """
    shares = _field_share_rows(field)
    partners = [
        s
        for s in shares
        if int(getattr(s, "is_me", 0) or 0) != 1
        and (getattr(s, "party_id", None) or (getattr(s, "partner_name", None) or "").strip())
    ]
    if partners:
        return partners, shares, False

    mode = (getattr(field, "ownership_mode", None) or "").strip()
    my_pct = getattr(field, "my_share_pct", None)
    looks_shared = mode == "on_shares" or (my_pct is not None and float(my_pct) < 99.5)
    if not looks_shared:
        return [], shares, False

    party = None
    matched_by_name = False
    field_pid = getattr(field, "party_id", None)
    if field_pid and parties:
        party = next((p for p in parties if getattr(p, "id", None) == field_pid), None)
    if party is None and parties:
        party = match_party_for_field_name(getattr(field, "name", "") or "", parties)
        matched_by_name = party is not None
    if party is None:
        return [], shares, False

    me_pct = float(my_pct) if my_pct is not None else 50.0
    other = max(0.0, 100.0 - me_pct)

    class _Synth:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.id = 0

        @property
        def display_name(self) -> str:
            if self.is_me:
                return "Me"
            if self.party is not None and getattr(self.party, "name", None):
                return self.party.name
            return self.partner_name or "Partner"

    # Rebuild shares with inferred partner so callers see a normal Me + partner pair
    me_row = next((s for s in shares if int(getattr(s, "is_me", 0) or 0) == 1), None)
    new_shares = [
        me_row
        or _Synth(is_me=1, party_id=None, partner_name="Me", share_pct=me_pct, sort_order=0, party=None)
    ]
    partner_row = _Synth(
        is_me=0,
        party_id=getattr(party, "id", None),
        partner_name=getattr(party, "name", None) or "Partner",
        share_pct=other,
        sort_order=1,
        party=party,
    )
    new_shares.append(partner_row)
    return [partner_row], new_shares, matched_by_name


def _my_acres_on_field(field: Any, shares: list[Any]) -> float:
    mine = getattr(field, "acres_mine", None)
    if mine is not None:
        return float(mine or 0)
    me = next((s for s in shares if int(getattr(s, "is_me", 0) or 0) == 1), None)
    pct = float(getattr(me, "share_pct", None) or getattr(field, "my_share_pct", None) or 100.0)
    return round(float(getattr(field, "acres_total", None) or 0) * (pct / 100.0), 4)


def _crop_bucket() -> dict[str, float]:
    return {
        "expected": 0.0,
        "sold": 0.0,
        "futures_bu": 0.0,
        "basis_bu": 0.0,
        "acres": 0.0,
    }


def _collect_parties(fields: list[Any], contracts: list[Any], parties: list[Any] | None) -> list[Any]:
    by_id: dict[int, Any] = {}
    for p in parties or []:
        pid = getattr(p, "id", None)
        if isinstance(pid, int):
            by_id[pid] = p
    for field in fields:
        p = getattr(field, "party", None)
        if p is not None and isinstance(getattr(p, "id", None), int):
            by_id[p.id] = p
        for s in getattr(field, "shares", None) or []:
            sp = getattr(s, "party", None)
            if sp is not None and isinstance(getattr(sp, "id", None), int):
                by_id[sp.id] = sp
    for c in contracts:
        wp = getattr(c, "with_party", None)
        if wp is not None and isinstance(getattr(wp, "id", None), int):
            by_id[wp.id] = wp
    return list(by_id.values())


def ownership_sold_breakdown(
    fields: list[Any],
    contracts: list[Any],
    *,
    crops: tuple[str, ...] = ("Corn", "Soybeans"),
    parties: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """% sold by ownership: Me-only farms, then each share partner.

    Expected bu = my share of field production on farms in that ownership.
    Sold bu = contracts tagged with that with_party_id (None = Me).

    Share fields with no partner linked are matched to a party by field name
    when possible (e.g. Simpson Even → Ed Simpson).
    """
    party_list = _collect_parties(fields, contracts, parties)
    groups: dict[Any, dict[str, Any]] = {}

    def _ensure(key: Any, *, label: str, party_id: Optional[int], me_pcts: list[float]) -> dict:
        if key not in groups:
            groups[key] = {
                "key": "me" if key is None else f"party-{key}",
                "party_id": party_id,
                "label": label,
                "is_me": key is None,
                "me_pcts": list(me_pcts),
                "field_count": 0,
                "matched_by_name": False,
                "by_crop": {crop: _crop_bucket() for crop in crops},
            }
        else:
            groups[key]["me_pcts"].extend(me_pcts)
        return groups[key]

    _ensure(None, label="Me (my farms)", party_id=None, me_pcts=[])

    for field in fields:
        crop = getattr(field, "crop", None)
        if crop not in crops:
            continue
        partners, shares, by_name = resolve_field_partners(field, party_list)
        me_row = next((s for s in shares if int(getattr(s, "is_me", 0) or 0) == 1), None)
        me_pct = float(getattr(me_row, "share_pct", None) or getattr(field, "my_share_pct", None) or 100.0)
        my_acres = _my_acres_on_field(field, shares)
        yld = getattr(field, "expected_yield", None)
        bu = round(my_acres * float(yld), 1) if yld is not None else 0.0

        mode = (getattr(field, "ownership_mode", None) or "").strip()
        looks_shared = mode == "on_shares" or me_pct < 99.5

        if not partners:
            if looks_shared:
                # Shared field but partner still unknown — keep out of Me-only bucket
                key = f"unlinked:{(getattr(field, 'name', None) or getattr(field, 'id', 'field'))}".strip().lower()
                g = _ensure(
                    key,
                    label=f"Shared · {getattr(field, 'name', None) or 'field'} (partner not linked)",
                    party_id=None,
                    me_pcts=[me_pct],
                )
            else:
                g = _ensure(None, label="Me (my farms)", party_id=None, me_pcts=[me_pct])
            g["field_count"] += 1
            bucket = g["by_crop"][crop]
            bucket["expected"] = round(bucket["expected"] + bu, 1)
            bucket["acres"] = round(bucket["acres"] + my_acres, 2)
        else:
            # Multi-partner: my expected bu counts on every linked partner card
            # (same rule as "fields linked to that partner"). Do not split by partner %.
            partners_sorted = sorted(
                partners,
                key=lambda s: (int(getattr(s, "sort_order", 0) or 0), getattr(s, "id", 0) or 0),
            )
            for p in partners_sorted:
                p_pid = getattr(p, "party_id", None)
                p_key = (
                    p_pid
                    if p_pid is not None
                    else f"name:{(getattr(p, 'display_name', None) or 'Partner').strip().lower()}"
                )
                g = _ensure(
                    p_key,
                    label=f"With {p.display_name}",
                    party_id=p_pid if isinstance(p_pid, int) else None,
                    me_pcts=[me_pct],
                )
                if by_name:
                    g["matched_by_name"] = True
                g["field_count"] += 1
                bucket = g["by_crop"][crop]
                bucket["expected"] = round(bucket["expected"] + bu, 1)
                bucket["acres"] = round(bucket["acres"] + my_acres, 2)

    # Contracts: Me = with_party_id None; partner = matching party_id
    for c in contracts:
        crop = getattr(c, "crop", None)
        if crop not in crops:
            continue
        wid = getattr(c, "with_party_id", None)
        if wid is None:
            g = groups.get(None)
        else:
            if wid not in groups:
                name = None
                wp = getattr(c, "with_party", None)
                if wp is not None:
                    name = getattr(wp, "name", None)
                _ensure(wid, label=f"With {name or f'Party #{wid}'}", party_id=wid, me_pcts=[])
            g = groups.get(wid)
        if not g:
            continue
        bu = float(getattr(c, "bushels", 0) or 0)
        bucket = g["by_crop"][crop]
        bucket["sold"] = round(bucket["sold"] + bu, 1)
        if futures_locked(c):
            bucket["futures_bu"] = round(bucket["futures_bu"] + bu, 1)
        if basis_locked(c):
            bucket["basis_bu"] = round(bucket["basis_bu"] + bu, 1)

    out: list[dict[str, Any]] = []
    ordered_keys = [None] + sorted(
        (k for k in groups if k is not None),
        key=lambda k: (0 if not str(k).startswith("unlinked:") else 1, str(groups[k]["label"]).lower()),
    )
    for key in ordered_keys:
        g = groups[key]
        pcts = [p for p in g["me_pcts"] if p is not None]
        sold_total = sum(g["by_crop"][c]["sold"] for c in crops)
        exp_total = sum(g["by_crop"][c]["expected"] for c in crops)
        if g["is_me"]:
            subtitle = "Fields you farm 100% · contracts tagged Me"
        elif g["field_count"] > 0 and pcts:
            lo, hi = min(pcts), max(pcts)
            lo_i, hi_i = int(round(lo)), int(round(hi))
            share_txt = f"my {lo_i}%" if lo_i == hi_i else f"my {lo_i}–{hi_i}%"
            subtitle = f"{g['field_count']} field{'s' if g['field_count'] != 1 else ''} · {share_txt} of expected"
            if g.get("matched_by_name"):
                subtitle += " · matched from field name"
        elif sold_total > 0 and exp_total <= 0:
            subtitle = (
                f"{sold_total:,.0f} bu on contracts · no shared fields linked yet "
                "(edit those fields and set the share partner)"
            )
        elif str(key).startswith("unlinked:"):
            subtitle = "On shares, but no partner linked on the field yet"
        else:
            subtitle = "No fields or contracts for this ownership yet"

        crop_cards = []
        for crop in crops:
            b = g["by_crop"][crop]
            expected = b["expected"]
            sold = b["sold"]
            fut = b["futures_bu"]
            bas = b["basis_bu"]
            pct = round(100 * sold / expected, 1) if expected > 0 else None
            fut_pct = round(100 * fut / expected, 1) if expected > 0 else None
            bas_pct = round(100 * bas / expected, 1) if expected > 0 else None
            if expected <= 0 and sold <= 0:
                continue
            crop_cards.append(
                {
                    "crop": crop,
                    "expected": expected,
                    "sold": sold,
                    "pct": pct,
                    "futures_pct": fut_pct,
                    "basis_pct": bas_pct,
                    "acres": b["acres"],
                    "oversold": bool(expected > 0 and sold > expected + 0.05),
                }
            )
        if not crop_cards and not g["is_me"]:
            continue
        out.append(
            {
                "key": g["key"],
                "party_id": g["party_id"],
                "label": g["label"],
                "is_me": g["is_me"],
                "subtitle": subtitle,
                "field_count": g["field_count"],
                "crops": crop_cards
                or [
                    {
                        "crop": crop,
                        "expected": 0.0,
                        "sold": 0.0,
                        "pct": None,
                        "futures_pct": None,
                        "basis_pct": None,
                        "acres": 0.0,
                    }
                    for crop in crops
                ],
            }
        )
    return out


def fields_missing_share_partner(fields: list[Any]) -> list[dict[str, Any]]:
    """On-share fields with no partner linked — for the quick-assign UI."""
    out: list[dict[str, Any]] = []
    for field in fields:
        mode = (getattr(field, "ownership_mode", None) or "").strip()
        my_pct = getattr(field, "my_share_pct", None)
        looks_shared = mode == "on_shares" or (my_pct is not None and float(my_pct) < 99.5)
        if not looks_shared:
            continue
        real_partners = [
            s
            for s in (getattr(field, "shares", None) or [])
            if int(getattr(s, "is_me", 0) or 0) != 1 and getattr(s, "party_id", None)
        ]
        if real_partners or getattr(field, "party_id", None):
            continue
        yld = getattr(field, "expected_yield", None)
        acres = float(getattr(field, "acres_mine", None) or 0)
        bu = round(acres * float(yld), 1) if yld is not None else None
        out.append(
            {
                "id": getattr(field, "id", None),
                "name": getattr(field, "name", None) or f"Field #{getattr(field, 'id', '?')}",
                "crop": getattr(field, "crop", None) or "—",
                "my_share_pct": float(my_pct) if my_pct is not None else 50.0,
                "acres_mine": acres,
                "acres_total": float(getattr(field, "acres_total", None) or 0),
                "expected_bu": bu,
            }
        )
    out.sort(key=lambda r: ((r.get("crop") or ""), (r.get("name") or "").lower()))
    return out


def link_field_to_share_partner(
    db: Any,
    field: Any,
    party: Any,
    *,
    me_pct: float | None = None,
) -> None:
    """Set/replace field shares as Me + one partner (typical 50/50 crop share)."""
    set_field_share_partners(db, field, [party], me_pct=me_pct)


def set_field_share_partners(
    db: Any,
    field: Any,
    parties: list[Any],
    *,
    me_pct: float | None = None,
    partner_pcts: list[float] | None = None,
) -> None:
    """Set/replace field shares as Me + one or more partners.

    partner_pcts (optional) aligns with parties. If omitted, remaining % after Me
    is split evenly across partners. Existing partner % are preserved when the
    same party set is rewritten with matching partner_pcts provided by caller.
    """
    from app.models import FieldShare

    clean_parties = [p for p in parties if p is not None and getattr(p, "id", None)]
    # Dedupe by id, keep order
    seen: set[int] = set()
    uniq: list[Any] = []
    for p in clean_parties:
        pid = int(p.id)
        if pid in seen:
            continue
        seen.add(pid)
        uniq.append(p)
    clean_parties = uniq

    for existing in list(getattr(field, "shares", None) or []):
        db.delete(existing)
    db.flush()

    pct_me = float(me_pct) if me_pct is not None else float(getattr(field, "my_share_pct", None) or 50.0)
    pct_me = max(0.0, min(100.0, pct_me))
    remaining = max(0.0, 100.0 - pct_me)

    pcts: list[float] = []
    if partner_pcts and len(partner_pcts) == len(clean_parties) and clean_parties:
        raw = [max(0.0, float(x)) for x in partner_pcts]
        total = sum(raw)
        if total > 0 and abs(total - remaining) > 0.05:
            # Scale partner % to fit remaining after Me
            scale = remaining / total if total else 0.0
            pcts = [round(x * scale, 2) for x in raw]
        else:
            pcts = [round(x, 2) for x in raw]
        drift = remaining - sum(pcts)
        if pcts and abs(drift) >= 0.01:
            pcts[-1] = round(pcts[-1] + drift, 2)
    elif clean_parties:
        each = round(remaining / len(clean_parties), 2)
        pcts = [each] * len(clean_parties)
        drift = remaining - sum(pcts)
        if pcts and abs(drift) >= 0.01:
            pcts[-1] = round(pcts[-1] + drift, 2)

    field.ownership_mode = "on_shares"
    field.my_share_pct = pct_me
    field.party_id = getattr(clean_parties[0], "id", None) if clean_parties else None
    if field.acres_total:
        field.acres_mine = round(float(field.acres_total or 0) * (pct_me / 100.0), 2)

    db.add(
        FieldShare(
            field_id=field.id,
            partner_name="Me",
            share_pct=pct_me,
            is_me=1,
            sort_order=0,
        )
    )
    for i, party in enumerate(clean_parties):
        db.add(
            FieldShare(
                field_id=field.id,
                party_id=party.id,
                partner_name=getattr(party, "name", None) or "Partner",
                share_pct=pcts[i] if i < len(pcts) else 0.0,
                is_me=0,
                sort_order=i + 1,
            )
        )


def heal_missing_field_share_partners(db: Any, fields: list[Any], parties: list[Any]) -> int:
    """Persist Me + partner FieldShare rows when on-share fields have no partner linked
    but party_id is set, or the field name uniquely matches a party (Simpson → Ed Simpson).

    Returns number of fields healed.
    """
    healed = 0
    party_by_id = {getattr(p, "id", None): p for p in parties if getattr(p, "id", None)}
    for field in fields:
        existing = list(getattr(field, "shares", None) or [])
        real_partners = [
            s
            for s in existing
            if int(getattr(s, "is_me", 0) or 0) != 1 and getattr(s, "party_id", None)
        ]
        if real_partners:
            continue
        mode = (getattr(field, "ownership_mode", None) or "").strip()
        my_pct = getattr(field, "my_share_pct", None)
        looks_shared = mode == "on_shares" or (my_pct is not None and float(my_pct) < 99.5)
        if not looks_shared:
            continue
        party = None
        field_pid = getattr(field, "party_id", None)
        if field_pid:
            party = party_by_id.get(field_pid)
        if party is None:
            party = match_party_for_field_name(getattr(field, "name", "") or "", parties)
        if party is None:
            continue
        link_field_to_share_partner(
            db,
            field,
            party,
            me_pct=float(my_pct) if my_pct is not None else 50.0,
        )
        healed += 1
    return healed


def desk_rows(
    contracts: list[Any],
    events_by: dict[int, list],
    breakeven_by_crop: dict[str, Optional[float]],
    market_by_crop: dict[str, Optional[float]],
) -> list[dict]:
    from app import cme_quotes

    rows = []
    for c in contracts:
        price = equiv_price(c)
        crop = getattr(c, "crop", "") or ""
        be = breakeven_by_crop.get(crop)
        mkt = market_by_crop.get(crop)
        left = bushels_left(c)
        fut_exp = cme_quotes.futures_month_expiry(crop, getattr(c, "futures_month", None))
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
                "futures_expiry": fut_exp,
            }
        )

    rows.sort(key=lambda row: contract_desk_sort_key(row["c"]))
    return rows
