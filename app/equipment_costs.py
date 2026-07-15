"""Equipment cost helpers (DIRTI-inspired ownership + maintenance → $/acre)."""

from __future__ import annotations

from datetime import date

from app.models import Equipment, EquipmentMaintenance


def annual_payment_total(equip: Equipment) -> float:
    if equip.finance_status not in ("loan", "leased"):
        return 0.0
    amt = equip.payment_amount or 0.0
    freq = (equip.payment_frequency or "annual").lower()
    if freq == "monthly":
        return amt * 12
    if freq == "quarterly":
        return amt * 4
    return amt


def economic_depreciation(equip: Equipment) -> float:
    if equip.finance_status == "leased":
        return 0.0
    price = equip.purchase_price
    life = equip.useful_life_years
    if not price or not life or life <= 0:
        return 0.0
    salvage = equip.salvage_value or 0.0
    return max(0.0, (price - salvage) / life)


def maintenance_in_year(equip: Equipment, year: int) -> float:
    total = 0.0
    for m in equip.maintenance or []:
        if m.service_date and m.service_date.year == year:
            total += m.cost or 0.0
    return total


def equipment_annual_costs(equip: Equipment, year: int | None = None) -> dict:
    year = year or date.today().year
    maint = maintenance_in_year(equip, year)
    payments = annual_payment_total(equip)
    insurance = equip.annual_insurance or 0.0
    taxes = equip.annual_taxes or 0.0
    housing = getattr(equip, "annual_housing", 0.0) or 0.0
    dep = economic_depreciation(equip)
    cash = maint + payments + insurance + taxes + housing
    total = cash + dep
    return {
        "maintenance": round(maint, 2),
        "payments": round(payments, 2),
        "insurance": round(insurance, 2),
        "taxes": round(taxes, 2),
        "housing": round(housing, 2),
        "depreciation": round(dep, 2),
        "cash_cost": round(cash, 2),
        "total_cost": round(total, 2),
    }


def cost_per_acre(total_cost: float, acres: float) -> float | None:
    if not acres or acres <= 0:
        return None
    return round(total_cost / acres, 2)
