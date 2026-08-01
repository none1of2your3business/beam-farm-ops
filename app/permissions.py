"""Role-based access: owner, agronomist, accountant, viewer."""

from fastapi.responses import RedirectResponse

ROLE_LABELS = {
    "owner": "Owner / Admin",
    "agronomist": "Agronomist",
    "accountant": "Accountant",
    "viewer": "Viewer",
}

# Modules each role can open
ROLE_MODULES = {
    "owner": {
        "dashboard",
        "fields",
        "parties",
        "equipment",
        "balance",
        "upload",
        "settings",
        "team",
        "trials",
        "insights",
        "panorama",
        "grain",
        "risk",
        "library",
        "purchases",
        "invoices",
        "settlements",
        "trucking",
        "tools",
        "activity",
        "budget",
    },
    "agronomist": {
        "dashboard",
        "fields",
        "parties",
        "trials",
        "insights",
        "library",
        "panorama",
        "tools",
    },
    "accountant": {
        "dashboard",
        "fields",
        "parties",
        "equipment",
        "balance",
        "upload",
        "purchases",
        "invoices",
        "risk",
        "grain",
        "settlements",
        "trucking",
        "activity",
        "budget",
    },
    "viewer": {"dashboard", "fields", "equipment", "balance", "risk", "insights", "budget"},
}


def can_access(role: str | None, module: str) -> bool:
    role = role or "viewer"
    return module in ROLE_MODULES.get(role, set())


def can_edit_finance(role: str | None) -> bool:
    return (role or "") in ("owner", "accountant")


def can_manage_team(role: str | None) -> bool:
    return (role or "") == "owner"


def deny() -> RedirectResponse:
    return RedirectResponse("/", status_code=303)
