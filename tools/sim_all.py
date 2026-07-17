"""End-to-end simulation of Beam Farm Ops modules against a live server."""
from __future__ import annotations

import re
import sys
import time
from datetime import date

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8002"
TODAY = date.today().isoformat()
TAG = str(int(time.time()))[-6:]
fails: list[tuple[str, int, str]] = []
ok = 0


def check(name: str, r: httpx.Response, expect: int = 200) -> bool:
    global ok
    if r.status_code != expect:
        fails.append((name, r.status_code, r.text[:240].replace("\n", " ")))
        print(f"FAIL {name} {r.status_code}")
        return False
    ok += 1
    print(f"OK   {name}")
    return True


def first_option(html: str, name: str) -> str:
    m = re.search(
        rf'name="{name}"[\s\S]*?<option value="(\d+)"',
        html,
    )
    return m.group(1) if m else ""


def first_href_id(html: str, prefix: str) -> str:
    m = re.search(rf'{prefix}/(\d+)', html)
    return m.group(1) if m else ""


def main() -> int:
    global ok
    with httpx.Client(base_url=BASE, timeout=45, follow_redirects=True) as c:
        check("login", c.post("/login", data={"username": "admin", "password": "farm2026"}))

        gets = [
            "/",
            "/fields",
            "/fields/sheet",
            "/fields/new",
            "/parties",
            "/trials",
            "/insights",
            "/panorama",
            "/bins",
            "/bins/sheet",
            "/risk",
            "/library",
            "/purchases",
            "/invoices",
            "/settlements",
            "/trucking",
            "/tools",
            "/equipment",
            "/equipment/new",
            "/balance",
            "/upload",
            "/scan",
            "/photos",
            "/export",
            "/activity",
            "/team",
            "/settings",
            "/balance/print",
            "/insights/ai-briefing",
            "/export/workbook",
        ]
        for p in gets:
            check(f"GET {p}", c.get(p))

        fields_html = c.get("/fields").text
        fid = first_href_id(fields_html, "/fields")
        # prefer a real field name link under /fields/<id> not /fields/new
        m = re.search(r'href="/fields/(\d+)"', fields_html)
        fid = m.group(1) if m else fid
        print("field", fid)

        if fid:
            check("GET field detail", c.get(f"/fields/{fid}"))
            check(
                "POST yield",
                c.post(f"/fields/{fid}/yield", data={"expected_yield": "205"}),
            )
            check(
                "POST operation",
                c.post(
                    f"/fields/{fid}/operation",
                    data={
                        "op_date": TODAY,
                        "op_type": "till",
                        "description": "sim till",
                        "cost": "40",
                    },
                ),
            )
            check(
                "POST soil",
                c.post(
                    f"/fields/{fid}/soil",
                    data={
                        "test_date": TODAY,
                        "lab": "SimLab",
                        "ph": "6.4",
                        "p": "35",
                        "k": "160",
                        "om": "3.1",
                        "notes": "sim",
                        "recommendations": "Build K",
                    },
                ),
            )

        check(
            "POST party",
            c.post(
                "/parties/save",
                data={
                    "name": "Sim Landlord",
                    "party_type": "landlord",
                    "phone": "",
                    "email": "",
                    "notes": "",
                },
            ),
        )

        if fid:
            check(
                "POST trial",
                c.post(
                    "/trials/create",
                    data={
                        "field_id": fid,
                        "name": "Sim Hybrid Trial",
                        "crop": "Corn",
                        "question": "A vs B",
                        "factor_tested": "hybrid",
                        "design_notes": "2 strips",
                        "grain_price": "4.5",
                    },
                ),
            )
            trials = c.get("/trials").text
            tid = first_href_id(trials, "/trials")
            if tid:
                check(
                    "POST treatment",
                    c.post(
                        f"/trials/{tid}/treatment",
                        data={"name": "Hybrid A", "is_control": "1", "description": "ctrl"},
                    ),
                )
                detail = c.get(f"/trials/{tid}").text
                treat = first_option(detail, "treatment_id")
                check(
                    "POST note",
                    c.post(
                        f"/trials/{tid}/note",
                        data={
                            "note_date": TODAY,
                            "title": "Scout",
                            "body": "Looks good",
                            "treatment_id": treat or "",
                        },
                    ),
                )
                if treat:
                    check(
                        "POST result",
                        c.post(
                            f"/trials/{tid}/result",
                            data={
                                "treatment_id": treat,
                                "rep_label": "1",
                                "harvest_date": TODAY,
                                "yield_bu_ac": "212",
                                "moisture": "15",
                                "test_weight": "56",
                                "notes": "",
                            },
                        ),
                    )
                check(
                    "POST conclusion",
                    c.post(
                        f"/trials/{tid}/conclusion",
                        data={
                            "conclusion": "A wins",
                            "status": "complete",
                            "grain_price": "4.5",
                        },
                    ),
                )

        check(
            "POST bin",
            c.post(
                "/bins/create",
                data={"name": f"Sim Bin {TAG}", "crop": "Corn", "capacity_bu": "40000"},
            ),
        )
        bins = c.get("/bins").text
        bid = first_option(bins, "bin_id")
        if bid:
            check(
                "POST fill shrink",
                c.post(
                    "/bins/move",
                    data={
                        "move_type": "fill",
                        "bin_id": bid,
                        "to_bin_id": "",
                        "field_id": "",
                        "owner_name": "Me",
                        "crop": "Corn",
                        "wet_bu": "1000",
                        "net_bu": "",
                        "moisture": "17",
                        "ticket_number": "SIM-T1",
                        "destination": "",
                        "contract_id": "",
                        "notes": "sim",
                        "move_date": TODAY,
                    },
                ),
            )
            check(
                "POST bin condition",
                c.post(
                    "/bins/condition",
                    data={
                        "bin_id": bid,
                        "note_date": TODAY,
                        "moisture": "15",
                        "temperature": "52",
                        "notes": "ok",
                    },
                ),
            )

        check(
            "POST hta contract",
            c.post(
                "/risk/contract",
                data={
                    "crop": "Corn",
                    "contract_type": "hta",
                    "buyer": "Sim Elevator",
                    "bushels": "5000",
                    "cash_price": "",
                    "futures_price": "4.60",
                    "basis": "",
                    "futures_month": "Z26",
                    "delivery_end": "",
                    "notes": "sim hta",
                },
            ),
        )
        check(
            "POST basis contract",
            c.post(
                "/risk/contract",
                data={
                    "crop": "Corn",
                    "contract_type": "basis",
                    "buyer": "Sim Elevator",
                    "bushels": "2000",
                    "cash_price": "",
                    "futures_price": "",
                    "basis": "-0.20",
                    "futures_month": "",
                    "delivery_end": "",
                    "notes": "sim basis",
                },
            ),
        )
        check(
            "POST cop",
            c.post(
                "/risk/cop",
                data={
                    "corn_cost_per_ac": "900",
                    "soy_cost_per_ac": "650",
                    "corn_price_assumption": "4.5",
                    "soy_price_assumption": "11",
                },
            ),
        )
        risk = c.get("/risk")
        check("GET marketing", risk)
        if "Marketing and Storage" not in risk.text or "mkt-bar-fill" not in risk.text:
            fails.append(("marketing chart", risk.status_code, "missing chart markup"))
            print("FAIL marketing chart markup")
        check(
            "POST quotes manual",
            c.post(
                "/risk/quotes",
                data={
                    "corn_futures": "4.55",
                    "soy_futures": "11.20",
                    "corn_local_basis": "-0.20",
                    "soy_local_basis": "-0.35",
                },
            ),
        )
        check("POST quotes refresh", c.post("/risk/quotes/refresh"))
        check(
            "POST stress",
            c.post(
                "/risk/stress",
                data={"corn_stress_shock": "0.50", "soy_stress_shock": "1.00"},
            ),
        )
        check(
            "POST carry",
            c.post(
                "/risk/carry",
                data={
                    "carry_interest_apr": "7",
                    "corn_storage_per_bu_mo": "0.03",
                    "soy_storage_per_bu_mo": "0.04",
                    "corn_shrink_per_bu_mo": "0",
                    "soy_shrink_per_bu_mo": "0",
                },
            ),
        )
        risk = c.get("/risk")
        check("GET marketing desk", risk)
        for needle in (
            "Futures board",
            "Risk tracker",
            "Contract desk",
            "Cost of carry",
            "Best / worst",
            "Full-year futures strip",
        ):
            if needle not in risk.text:
                fails.append((f"desk has {needle}", 0, "missing"))
                print(f"FAIL desk has {needle}")
            else:
                ok += 1
                print(f"OK   desk has {needle}")

        # contract event
        cid = first_option(risk.text, "contract_id")
        if cid:
            check(
                "POST contract event",
                c.post(
                    "/risk/event",
                    data={
                        "contract_id": cid,
                        "event_type": "partial_price",
                        "event_date": TODAY,
                        "bushels": "1000",
                        "price": "4.55",
                        "futures_month": "Z26",
                        "notes": "sim",
                    },
                ),
            )

        check(
            "POST insurance",
            c.post(
                "/risk/insurance",
                data={
                    "crop": "Corn",
                    "policy_type": "RP",
                    "coverage_level": "85",
                    "acres": "100",
                    "premium": "4200",
                    "guarantee_bu": "18000",
                    "notes": "",
                },
            ),
        )
        check(
            "POST target",
            c.post(
                "/risk/target",
                data={
                    "crop": "Corn",
                    "target_pct": "40",
                    "by_date": TODAY,
                    "price_floor": "4.4",
                    "notes": "",
                },
            ),
        )

        check(
            "POST hybrid",
            c.post(
                "/library/hybrid",
                data={
                    "name": "Sim Hybrid 99",
                    "crop": "Corn",
                    "brand": "Sim",
                    "maturity": "112",
                    "traits": "VT2",
                    "unit_label": "bag",
                    "cost_per_unit": "285",
                    "cost_per_acre": "95",
                },
            ),
        )
        check(
            "POST spray",
            c.post(
                "/library/spray",
                data={
                    "name": "Sim Mix 1",
                    "timing": "post",
                    "crop": "Corn",
                    "notes": "sim",
                    "line_product": ["Glyphosate", "Atrazine"],
                    "line_rate": ["32", "1"],
                    "line_rate_unit": ["oz/ac", "qt/ac"],
                    "line_unit": ["gal", "gal"],
                    "line_cost_unit": ["0.25", "12"],
                    "line_cost_acre": ["", ""],
                },
            ),
        )
        lib = c.get("/library").text
        hid = first_option(lib, "hybrid_id")
        sid = first_option(lib, "spray_mix_id")
        if fid and hid:
            check(
                "POST assign hybrid",
                c.post(
                    "/library/assign-hybrid",
                    data={"field_id": fid, "hybrid_id": hid, "rate": "32k"},
                ),
            )
        if fid and sid:
            check(
                "POST assign spray",
                c.post(
                    "/library/assign-spray",
                    data={"field_id": fid, "spray_mix_id": sid, "timing_label": "V5"},
                ),
            )
        if fid:
            check(
                "POST plan",
                c.post(
                    "/library/plan",
                    data={
                        "field_id": fid,
                        "plan_type": "planting",
                        "title": "Plant sim",
                        "details": "sim",
                        "target_date": TODAY,
                    },
                ),
            )

        check(
            "POST product",
            c.post(
                "/purchases/product",
                data={"name": f"Sim Chem {TAG}", "category": "chemical", "unit": "gal"},
            ),
        )
        buys = c.get("/purchases").text
        pid = first_option(buys, "product_id")
        if pid:
            check(
                "POST buy",
                c.post(
                    "/purchases/buy",
                    data={
                        "product_id": pid,
                        "purchase_date": TODAY,
                        "vendor": "SimCo",
                        "quantity": "100",
                        "total_cost": "2500",
                    },
                ),
            )
            if fid:
                check(
                    "POST assign product",
                    c.post(
                        "/purchases/assign",
                        data={
                            "product_id": pid,
                            "field_id": fid,
                            "quantity": "10",
                            "assign_date": TODAY,
                        },
                    ),
                )
                check(
                    "POST return product",
                    c.post(
                        "/purchases/return",
                        data={
                            "product_id": pid,
                            "field_id": fid,
                            "quantity": "2",
                            "return_date": TODAY,
                            "notes": "leftover",
                        },
                    ),
                )

        inv = c.get("/invoices").text
        party = first_option(inv, "party_id")
        check(
            "POST invoice",
            c.post(
                "/invoices/create",
                data={
                    "party_id": party,
                    "invoice_date": TODAY,
                    "description": "Custom plant",
                    "quantity": "80",
                    "rate": "35",
                },
            ),
        )

        check(
            "POST settlement",
            c.post(
                "/settlements/create",
                data={
                    "title": "Sim Settlement",
                    "party_id": party,
                    "settlement_date": TODAY,
                    "notes": "sim",
                },
            ),
        )
        settles = c.get("/settlements").text
        set_id = first_href_id(settles, "/settlements")
        if set_id:
            check("GET settlement", c.get(f"/settlements/{set_id}"))
            check(
                "POST settle line",
                c.post(
                    f"/settlements/{set_id}/line",
                    data={
                        "description": "Cash Rent/Property Taxes",
                        "amount": "1200",
                        "field_id": fid or "",
                    },
                ),
            )
            check("POST autofill rent", c.post(f"/settlements/{set_id}/autofill-rent"))

        check(
            "POST truck rate",
            c.post(
                "/trucking/rate",
                data={
                    "destination": "Cargill",
                    "hauler": "Sim Haul",
                    "rate_per_bu": "0.12",
                    "rate_per_load": "",
                    "miles": "40",
                    "notes": "",
                },
            ),
        )
        check(
            "POST truck load",
            c.post(
                "/trucking/load",
                data={
                    "load_date": TODAY,
                    "crop": "Corn",
                    "destination": "Cargill",
                    "hauler": "Sim Haul",
                    "bushels": "900",
                    "rate_paid": "108",
                    "ticket_number": "L-SIM",
                    "notes": "",
                },
            ),
        )

        check(
            "POST sprayer",
            c.post(
                "/tools/sprayer",
                data={
                    "tank_gal": "1000",
                    "gpa": "15",
                    "acres": "80",
                    "field_id": fid or "",
                    "product_rate": "32",
                    "product_unit": "oz/ac",
                    "save_defaults": "1",
                },
            ),
        )

        check(
            "POST equipment",
            c.post(
                "/equipment/save",
                data={
                    "name": "Sim Tractor 8R",
                    "category": "tractor",
                    "make": "JD",
                    "model": "8R",
                    "year": "2020",
                    "serial_number": "SIM123",
                    "status": "active",
                    "finance_status": "owned",
                    "purchase_date": "2020-01-15",
                    "purchase_price": "300000",
                    "market_value": "210000",
                    "salvage_value": "50000",
                    "useful_life_years": "10",
                    "hours": "1500",
                    "payment_amount": "",
                    "payment_frequency": "annual",
                    "loan_balance": "",
                    "lender_name": "",
                    "lease_end_date": "",
                    "annual_insurance": "2500",
                    "annual_taxes": "600",
                    "annual_housing": "900",
                    "notes": "sim",
                },
            ),
        )
        equip = c.get("/equipment").text
        eid = first_href_id(equip, "/equipment")
        if eid:
            check("GET equipment", c.get(f"/equipment/{eid}"))
            check(
                "POST maintenance",
                c.post(
                    f"/equipment/{eid}/maintenance",
                    data={
                        "service_date": TODAY,
                        "description": "Oil change",
                        "cost": "350",
                        "hours_at_service": "1500",
                        "vendor": "Dealer",
                        "notes": "",
                    },
                ),
            )
            check(
                "POST fuel",
                c.post(
                    f"/equipment/{eid}/fuel",
                    data={
                        "fill_date": TODAY,
                        "gallons": "80",
                        "cost": "280",
                        "hours_at_fill": "1502",
                        "notes": "",
                    },
                ),
            )
            check(
                "POST equip event",
                c.post(
                    f"/equipment/{eid}/event",
                    data={
                        "event_type": "other",
                        "event_date": TODAY,
                        "amount": "0",
                        "counterparty": "",
                        "notes": "sim note",
                    },
                ),
            )

        check(
            "POST balance item",
            c.post(
                "/balance/item",
                data={
                    "as_of_date": TODAY,
                    "side": "asset",
                    "category": "cash",
                    "label": "Sim Operating Cash",
                    "amount": "75000",
                    "is_current": "1",
                    "notes": "",
                },
            ),
        )
        check(
            "POST snapshot",
            c.post(
                "/balance/snapshot",
                data={
                    "as_of_date": TODAY,
                    "label": "Sim Snapshot",
                    "assets_total": "500000",
                    "liabilities_total": "200000",
                    "equity": "300000",
                    "working_capital": "80000",
                },
            ),
        )

        check(
            "POST panorama connect",
            c.post("/panorama/connect", data={"organization_code": "SIM-ORG"}),
        )
        check("POST settings", c.post("/settings/farm", data={"farm_name_value": "Beam Farm Ops"}))

        # final sweep of key pages after writes
        for p in ["/", "/risk", "/bins", "/fields", f"/fields/{fid}" if fid else "/fields", "/equipment", "/balance"]:
            check(f"FINAL {p}", c.get(p))

    print("---")
    print(f"OK {ok}  FAIL {len(fails)}")
    for name, code, body in fails:
        print(f"  {name} [{code}] {body[:160]}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
