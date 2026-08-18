"""Editable dropdown / list masters used across hubs for consistent reporting."""

from __future__ import annotations

import re

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import (
    BinShare,
    EquipmentMaintenance,
    Field,
    GrainBin,
    GrainContract,
    GrainMovement,
    Hybrid,
    InputProduct,
    InputPurchase,
    LookupValue,
    TruckLoad,
    TruckingRate,
)

# Canonical categories
CROP = "crop"
OWNERSHIP_MODE = "ownership_mode"
LEASE_TYPE = "lease_type"
PARTY_TYPE = "party_type"
HAULER = "hauler"
DESTINATION = "destination"
FREIGHT_RATE = "freight_rate"
GRAIN_OWNER = "grain_owner"
CONTRACT_TYPE = "contract_type"
VENDOR = "vendor"
PRODUCT_CATEGORY = "product_category"
PRODUCT_UNIT = "product_unit"
HYBRID_BRAND = "hybrid_brand"
HYBRID_TRAIT = "hybrid_trait"
EQUIP_CATEGORY = "equip_category"
EQUIP_STATUS = "equip_status"
FINANCE_TYPE = "finance_type"
DOC_KIND = "doc_kind"
BALANCE_SIDE = "balance_side"
BALANCE_CATEGORY = "balance_category"

CATEGORY_LABELS: dict[str, str] = {
    CROP: "Crops",
    OWNERSHIP_MODE: "Ownership modes",
    LEASE_TYPE: "Lease types",
    PARTY_TYPE: "Party types",
    HAULER: "Trucking companies",
    DESTINATION: "Destinations / elevators",
    FREIGHT_RATE: "Freight rates ($/bu)",
    GRAIN_OWNER: "Grain owners / shares",
    CONTRACT_TYPE: "Contract types",
    VENDOR: "Vendors",
    PRODUCT_CATEGORY: "Product categories",
    PRODUCT_UNIT: "Product units",
    HYBRID_BRAND: "Hybrid / seed brands",
    HYBRID_TRAIT: "Hybrid traits",
    EQUIP_CATEGORY: "Equipment categories",
    EQUIP_STATUS: "Equipment statuses",
    FINANCE_TYPE: "Equipment finance types",
    DOC_KIND: "Document kinds",
    BALANCE_SIDE: "Balance sheet sides",
    BALANCE_CATEGORY: "Balance sheet categories",
}

# Hub Lists page: which categories + related entity links
HUB_LISTS: dict[str, dict] = {
    "fields": {
        "title": "Fields — lists & settings",
        "lede": "Edit the values that appear in Fields dropdowns. Keep names consistent for reports.",
        "module": "fields",
        "active": "fields_ops",
        "back_href": "/fields",
        "categories": [CROP, OWNERSHIP_MODE, LEASE_TYPE, PARTY_TYPE],
        "links": [
            {"href": "/parties", "label": "Parties (landlords, partners, buyers)"},
            {"href": "/fields", "label": "Fields"},
            {"href": "/fields/new", "label": "Add field"},
        ],
    },
    "inputs": {
        "title": "Inputs — lists & settings",
        "lede": "Vendors, product categories, units, and brands used on purchases and the catalog.",
        "module": "library",
        "active": "inputs_ops",
        "back_href": "/inputs/ops",
        "categories": [VENDOR, PRODUCT_CATEGORY, PRODUCT_UNIT, HYBRID_BRAND, HYBRID_TRAIT, CROP],
        "links": [
            {"href": "/inputs", "label": "Seed / chem catalog"},
            {"href": "/inputs/hybrids/sheet", "label": "Hybrid sheet"},
            {"href": "/purchases", "label": "Products & purchases"},
        ],
    },
    "bins": {
        "title": "Bins — lists & settings",
        "lede": "Haulers, destinations, freight rates, crops, and grain owners used on tickets and moves. Partners for “grain for / farmed with” are on Parties.",
        "module": "grain",
        "active": "bins",
        "back_href": "/bins",
        "categories": [HAULER, DESTINATION, FREIGHT_RATE, GRAIN_OWNER, CROP],
        "links": [
            {"href": "/bins", "label": "Bins"},
            {"href": "/bins/new", "label": "Add bin"},
            {"href": "/parties", "label": "Parties (grain for / farmed with)"},
            {"href": "/trucking", "label": "Trucking rate table"},
            {"href": "/lists", "label": "All selection lists"},
        ],
    },
    "marketing": {
        "title": "Marketing — lists & settings",
        "lede": "Contract types, crops, and destinations used on the board and contracts desk.",
        "module": "risk",
        "active": "risk",
        "back_href": "/risk",
        "categories": [CROP, CONTRACT_TYPE, DESTINATION],
        "links": [
            {"href": "/risk", "label": "Board"},
            {"href": "/risk/contracts", "label": "Contracts"},
            {"href": "/risk/carry", "label": "Carry cost"},
            {"href": "/risk/hold-sell", "label": "Grain decisions"},
            {"href": "/risk/contracts/add", "label": "Add contract"},
            {"href": "/risk/contracts/roll", "label": "Roll contract"},
            {"href": "/risk/settings", "label": "Risk settings"},
            {"href": "/marketing/lists", "label": "Lists"},
        ],
    },
    "money": {
        "title": "Money — lists & settings",
        "lede": "Parties and categories used on settlements, invoices, balance sheet, and equipment.",
        "module": "settlements",
        "active": "money_ops",
        "back_href": "/budget/money",
        "categories": [PARTY_TYPE, VENDOR, EQUIP_CATEGORY, EQUIP_STATUS, FINANCE_TYPE, BALANCE_SIDE, BALANCE_CATEGORY],
        "links": [
            {"href": "/parties", "label": "Parties"},
            {"href": "/equipment", "label": "Equipment"},
            {"href": "/balance", "label": "Balance sheet"},
            {"href": "/budget", "label": "Budget board"},
        ],
    },
    "capture": {
        "title": "Capture — lists & settings",
        "lede": "Document kinds and vendors used when filing scans and imports.",
        "module": "upload",
        "active": "capture_ops",
        "back_href": "/capture",
        "categories": [DOC_KIND, VENDOR, PRODUCT_CATEGORY],
        "links": [
            {"href": "/scan", "label": "Scan docs"},
            {"href": "/purchases", "label": "Products"},
            {"href": "/parties", "label": "Parties"},
        ],
    },
    "admin": {
        "title": "All selection lists",
        "lede": "Edit every dropdown list used across the farm. Hide unused values to keep selects clean. Parties (partners, landlords, buyers) are managed separately.",
        "module": "settings",
        "active": "admin_ops",
        "back_href": "/admin",
        "categories": sorted(CATEGORY_LABELS.keys(), key=lambda c: CATEGORY_LABELS[c].lower()),
        "links": [
            {"href": "/parties", "label": "Parties (partners, landlords, buyers)"},
            {"href": "/settings", "label": "Farm settings"},
            {"href": "/team", "label": "Team"},
            {"href": "/bins/lists", "label": "Bins lists"},
            {"href": "/marketing/lists", "label": "Marketing lists"},
            {"href": "/fields/lists", "label": "Fields lists"},
        ],
    },
}

# Defaults seeded when empty
_DEFAULTS: dict[str, list[tuple[str, str | None, float | None, int]]] = {
    # name, label, numeric, sort
    CROP: [
        ("Corn", None, None, 10),
        ("Soybeans", None, None, 20),
        ("None", "None / fallow", None, 90),
    ],
    OWNERSHIP_MODE: [
        ("operated_by_me", "Operated by me", None, 10),
        ("on_shares", "On shares", None, 20),
        ("custom_work", "Custom work", None, 30),
    ],
    LEASE_TYPE: [
        ("cash_rent", "Cash rent", None, 10),
        ("flex_rent", "Flex rent", None, 20),
        ("crop_share", "Crop share", None, 30),
        ("none", "None", None, 90),
    ],
    PARTY_TYPE: [
        ("landlord", "Landlord", None, 10),
        ("partner", "Partner", None, 20),
        ("customer", "Customer (custom work)", None, 30),
        ("buyer", "Grain buyer", None, 40),
        ("other", "Other", None, 90),
    ],
    CONTRACT_TYPE: [
        ("cash", "Cash", None, 10),
        ("forward", "Forward", None, 20),
        ("hta", "HTA", None, 30),
        ("basis", "Basis", None, 40),
        ("dp", "DP", None, 50),
        ("minimum_price", "Minimum price", None, 60),
        ("futures_only", "Futures Only", None, 65),
        ("accumulator", "Accumulator", None, 70),
        ("custom", "Custom", None, 90),
    ],
    PRODUCT_CATEGORY: [
        ("chemical", None, None, 10),
        ("fertilizer", None, None, 20),
        ("seed", None, None, 30),
        ("other", None, None, 90),
    ],
    PRODUCT_UNIT: [
        ("gal", None, None, 10),
        ("lb", None, None, 20),
        ("ton", None, None, 30),
        ("bag", None, None, 40),
        ("unit", None, None, 50),
        ("oz", None, None, 60),
        ("qt", None, None, 70),
    ],
    EQUIP_CATEGORY: [
        ("tractor", None, None, 10),
        ("planter", None, None, 20),
        ("sprayer", None, None, 30),
        ("combine", None, None, 40),
        ("tillage", None, None, 50),
        ("truck", None, None, 60),
        ("trailer", None, None, 70),
        ("other", None, None, 90),
    ],
    EQUIP_STATUS: [
        ("active", "Active", None, 10),
        ("idle", "Idle", None, 20),
        ("sold", "Sold", None, 30),
        ("traded", "Traded", None, 35),
        ("retired", "Retired", None, 40),
    ],
    FINANCE_TYPE: [
        ("owned", "Owned (free & clear)", None, 10),
        ("loan", "Owned — making payments", None, 20),
        ("leased", "Leased", None, 30),
    ],
    DOC_KIND: [
        ("receipt", "Receipt", None, 10),
        ("invoice", "Invoice", None, 20),
        ("scale_ticket", "Scale ticket", None, 30),
        ("contract", "Contract", None, 40),
        ("other", "Other", None, 90),
    ],
    BALANCE_SIDE: [
        ("asset", "Asset", None, 10),
        ("liability", "Liability", None, 20),
    ],
    BALANCE_CATEGORY: [
        ("current", "Current", None, 10),
        ("noncurrent", "Non-current", None, 20),
    ],
    GRAIN_OWNER: [("Me", None, None, 10)],
}

# String cascade on rename: (table, column)
_CASCADE: dict[str, list[tuple[str, str]]] = {
    HAULER: [
        ("grain_movements", "hauler"),
        ("trucking_rates", "hauler"),
        ("truck_loads", "hauler"),
    ],
    DESTINATION: [
        ("grain_movements", "destination"),
        ("trucking_rates", "destination"),
        ("truck_loads", "destination"),
    ],
    VENDOR: [
        ("input_purchases", "vendor"),
        ("equipment_maintenance", "vendor"),
    ],
    GRAIN_OWNER: [
        ("bin_shares", "owner_name"),
        ("grain_movements", "owner_name"),
    ],
    CROP: [
        ("fields", "crop"),
        ("grain_bins", "crop"),
        ("grain_movements", "crop"),
        ("grain_contracts", "crop"),
        ("hybrids", "crop"),
        ("truck_loads", "crop"),
        ("crop_trials", "crop"),
    ],
    PRODUCT_CATEGORY: [("input_products", "category")],
    PRODUCT_UNIT: [("input_products", "unit")],
    HYBRID_BRAND: [("hybrids", "brand")],
    CONTRACT_TYPE: [("grain_contracts", "contract_type")],
    EQUIP_CATEGORY: [("equipment", "category")],
    EQUIP_STATUS: [("equipment", "status")],
    FINANCE_TYPE: [("equipment", "finance_status")],
    OWNERSHIP_MODE: [("fields", "ownership_mode")],
    LEASE_TYPE: [("fields", "lease_type")],
    PARTY_TYPE: [("parties", "party_type")],
}


def items(db: Session, category: str, *, active_only: bool = True) -> list[LookupValue]:
    q = select(LookupValue).where(LookupValue.category == category)
    if active_only:
        q = q.where(LookupValue.is_active == 1)
    q = q.order_by(LookupValue.sort_order, LookupValue.name)
    return list(db.scalars(q))


def names(db: Session, category: str) -> list[str]:
    return [row.name for row in items(db, category)]


def options(db: Session, category: str) -> list[dict]:
    """Dropdown options: value=name, label=display."""
    return [{"value": row.name, "label": row.display} for row in items(db, category)]


def freight_rates(db: Session) -> list[float]:
    rates: list[float] = []
    for row in items(db, FREIGHT_RATE):
        if row.numeric_value is not None:
            rates.append(round(float(row.numeric_value), 4))
        else:
            try:
                rates.append(round(float(row.name), 4))
            except ValueError:
                continue
    return sorted(set(rates))


def ensure(
    db: Session,
    category: str,
    name: str,
    *,
    label: str | None = None,
    numeric_value: float | None = None,
    notes: str | None = None,
    commit: bool = False,
) -> LookupValue | None:
    raw = (name or "").strip()
    if not raw or raw == "—":
        return None
    existing = db.scalar(
        select(LookupValue).where(
            LookupValue.category == category,
            LookupValue.name == raw,
        )
    )
    if existing is None:
        # Pending inserts in this session are invisible to the SELECT above
        for obj in db.new:
            if (
                isinstance(obj, LookupValue)
                and obj.category == category
                and (obj.name or "").strip() == raw
            ):
                existing = obj
                break
    if existing:
        if existing.is_active != 1:
            existing.is_active = 1
        if label and not existing.label:
            existing.label = label
        if numeric_value is not None and existing.numeric_value is None:
            existing.numeric_value = numeric_value
        if notes and not existing.notes:
            existing.notes = notes
        if commit:
            db.commit()
        return existing
    row = LookupValue(
        category=category,
        name=raw,
        label=(label.strip() if label else None),
        numeric_value=numeric_value,
        notes=(notes.strip() if notes else None),
        sort_order=100,
        is_active=1,
    )
    db.add(row)
    if commit:
        db.commit()
    return row


def ensure_freight(db: Session, rate: float, *, commit: bool = False) -> LookupValue | None:
    rate = round(float(rate), 4)
    name = f"{rate:.4f}".rstrip("0").rstrip(".")
    if "." not in name:
        name = f"{rate:.2f}"
    # Prefer a stable 4-decimal name for uniqueness
    canon = f"{rate:.4f}"
    return ensure(db, FREIGHT_RATE, canon, label=f"${rate:.4f}/bu", numeric_value=rate, commit=commit)


def deactivate(db: Session, item_id: int) -> LookupValue | None:
    row = db.get(LookupValue, item_id)
    if not row:
        return None
    row.is_active = 0
    db.commit()
    return row


def rename(db: Session, item_id: int, new_name: str, new_label: str | None = None) -> LookupValue | None:
    row = db.get(LookupValue, item_id)
    if not row:
        return None
    new_name = (new_name or "").strip()
    if not new_name:
        return row
    old = row.name
    if new_name != old:
        clash = db.scalar(
            select(LookupValue).where(
                LookupValue.category == row.category,
                LookupValue.name == new_name,
                LookupValue.id != row.id,
            )
        )
        if clash:
            return None
        for table, col in _CASCADE.get(row.category, []):
            db.execute(
                text(f"UPDATE {table} SET {col} = :new WHERE {col} = :old"),
                {"new": new_name, "old": old},
            )
        row.name = new_name
        if row.category == FREIGHT_RATE:
            try:
                row.numeric_value = float(new_name)
                row.label = f"${float(new_name):.4f}/bu"
            except ValueError:
                pass
    if new_label is not None:
        row.label = new_label.strip() or None
    db.commit()
    return row


def grouped_for_hub(db: Session, hub: str) -> dict[str, list[LookupValue]]:
    cfg = HUB_LISTS.get(hub) or {}
    out: dict[str, list[LookupValue]] = {}
    for cat in cfg.get("categories", []):
        out[cat] = items(db, cat, active_only=False)
    return out


def context_lists(db: Session) -> dict[str, list]:
    """Lightweight lists for form dropdowns (injected into templates)."""
    return {
        "crop": names(db, CROP) or ["Corn", "Soybeans", "None"],
        "hauler": names(db, HAULER),
        "destination": names(db, DESTINATION),
        "freight_rate": freight_rates(db),
        "grain_owner": names(db, GRAIN_OWNER) or ["Me"],
        "vendor": names(db, VENDOR),
        "product_category": names(db, PRODUCT_CATEGORY) or ["chemical", "fertilizer", "seed", "other"],
        "product_unit": names(db, PRODUCT_UNIT) or ["gal", "lb", "ton", "bag", "unit"],
        "hybrid_brand": names(db, HYBRID_BRAND),
        "hybrid_trait": names(db, HYBRID_TRAIT),
        "contract_type": options(db, CONTRACT_TYPE),
        "ownership_mode": options(db, OWNERSHIP_MODE),
        "lease_type": options(db, LEASE_TYPE),
        "party_type": options(db, PARTY_TYPE),
        "equip_category": names(db, EQUIP_CATEGORY),
        "equip_status": options(db, EQUIP_STATUS),
        "finance_type": options(db, FINANCE_TYPE),
        "doc_kind": options(db, DOC_KIND),
        "balance_side": options(db, BALANCE_SIDE),
        "balance_category": options(db, BALANCE_CATEGORY),
    }


def seed_lookups(db: Session) -> None:
    """Ensure defaults + harvest distinct values from existing records."""
    for category, rows in _DEFAULTS.items():
        for name, label, numeric, sort in rows:
            existing = db.scalar(
                select(LookupValue).where(
                    LookupValue.category == category,
                    LookupValue.name == name,
                )
            )
            if existing:
                continue
            db.add(
                LookupValue(
                    category=category,
                    name=name,
                    label=label,
                    numeric_value=numeric,
                    sort_order=sort,
                    is_active=1,
                )
            )
    db.flush()

    def _harvest(category: str, values: list[str | None]) -> None:
        for v in values:
            if v and str(v).strip() and str(v).strip() != "—":
                ensure(db, category, str(v).strip())

    _harvest(HAULER, [r.hauler for r in db.scalars(select(TruckingRate))])
    _harvest(HAULER, [r.hauler for r in db.scalars(select(TruckLoad))])
    _harvest(HAULER, [r.hauler for r in db.scalars(select(GrainMovement))])
    _harvest(DESTINATION, [r.destination for r in db.scalars(select(TruckingRate))])
    _harvest(DESTINATION, [r.destination for r in db.scalars(select(TruckLoad))])
    _harvest(DESTINATION, [r.destination for r in db.scalars(select(GrainMovement))])
    _harvest(VENDOR, [r.vendor for r in db.scalars(select(InputPurchase))])
    _harvest(VENDOR, [r.vendor for r in db.scalars(select(EquipmentMaintenance))])
    _harvest(GRAIN_OWNER, [r.owner_name for r in db.scalars(select(BinShare))])
    _harvest(GRAIN_OWNER, [r.owner_name for r in db.scalars(select(GrainMovement))])
    _harvest(CROP, [r.crop for r in db.scalars(select(Field))])
    _harvest(CROP, [r.crop for r in db.scalars(select(GrainBin))])
    _harvest(HYBRID_BRAND, [r.brand for r in db.scalars(select(Hybrid)) if r.brand])
    trait_vals: list[str] = []
    for r in db.scalars(select(Hybrid)):
        if not r.traits:
            continue
        trait_vals.append(r.traits.strip())
        for part in re.split(r"[,;/|]+", r.traits):
            if part.strip():
                trait_vals.append(part.strip())
    _harvest(HYBRID_TRAIT, trait_vals)
    _harvest(PRODUCT_CATEGORY, [r.category for r in db.scalars(select(InputProduct))])
    _harvest(PRODUCT_UNIT, [r.unit for r in db.scalars(select(InputProduct))])
    _harvest(CONTRACT_TYPE, [r.contract_type for r in db.scalars(select(GrainContract))])

    for r in db.scalars(select(TruckingRate)):
        if r.rate_per_bu is not None:
            ensure_freight(db, float(r.rate_per_bu))
    for r in db.scalars(select(GrainMovement)):
        if r.freight_per_bu is not None:
            ensure_freight(db, float(r.freight_per_bu))

    db.commit()
