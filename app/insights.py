"""Trial summaries and AI briefing pack builders."""

from __future__ import annotations

from statistics import mean

from app.models import CropTrial, Field


def treatment_summary(trial: CropTrial) -> list[dict]:
    rows = []
    price = trial.grain_price
    for t in sorted(trial.treatments, key=lambda x: (x.sort_order, x.id)):
        yields = [r.yield_bu_ac for r in t.results if r.yield_bu_ac is not None]
        avg_y = mean(yields) if yields else None
        rev = round(avg_y * price, 2) if avg_y is not None and price else None
        rows.append(
            {
                "treatment": t,
                "n": len(yields),
                "avg_yield": round(avg_y, 1) if avg_y is not None else None,
                "min_yield": round(min(yields), 1) if yields else None,
                "max_yield": round(max(yields), 1) if yields else None,
                "revenue_ac": rev,
            }
        )
    control = next((r for r in rows if r["treatment"].is_control), None)
    for r in rows:
        if control and r["avg_yield"] is not None and control["avg_yield"] is not None:
            r["yield_delta"] = round(r["avg_yield"] - control["avg_yield"], 1)
            if r["revenue_ac"] is not None and control["revenue_ac"] is not None:
                r["rev_delta"] = round(r["revenue_ac"] - control["revenue_ac"], 2)
            else:
                r["rev_delta"] = None
        else:
            r["yield_delta"] = None
            r["rev_delta"] = None
    return rows


def build_ai_briefing(
    farm_name: str,
    year_label: str,
    fields: list[Field],
    trials: list[CropTrial],
) -> str:
    lines = [
        f"# Farm AI briefing — {farm_name}",
        f"Crop year: {year_label}",
        "",
        "You are advising a Midwest corn/soybean grower. Look for trends, weak spots,",
        "and practical ways to raise better crops and improve profit. Be specific.",
        "Do not invent data that is not below. Flag where more data is needed.",
        "",
        "## Fields",
    ]
    for f in fields:
        lines.append(
            f"- {f.name}: crop={f.crop}, my_acres={f.acres_mine}, "
            f"ownership={f.ownership_mode}, rent=${f.rent_per_acre}/ac, "
            f"expected_yield={f.expected_yield or 'n/a'}"
        )
    lines.append("")
    lines.append("## Crop trials")
    if not trials:
        lines.append("- No trials recorded yet.")
    for trial in trials:
        lines.append(f"### {trial.name} ({trial.status})")
        lines.append(f"Field: {trial.field.name if trial.field else trial.field_id}")
        lines.append(f"Crop: {trial.crop}")
        lines.append(f"Question: {trial.question or 'n/a'}")
        lines.append(f"Factor: {trial.factor_tested or 'n/a'}")
        lines.append(f"Grain price used: {trial.grain_price or 'n/a'}")
        summary = treatment_summary(trial)
        for row in summary:
            t = row["treatment"]
            ctrl = " [control]" if t.is_control else ""
            lines.append(
                f"- Treatment {t.name}{ctrl}: avg_yield={row['avg_yield']}, "
                f"n={row['n']}, yield_delta={row['yield_delta']}, "
                f"rev$/ac={row['revenue_ac']}, rev_delta={row['rev_delta']}"
            )
        if trial.notes:
            lines.append("Notes:")
            for n in sorted(trial.notes, key=lambda x: x.note_date):
                lines.append(f"  - {n.note_date}: {n.title or ''} {n.body[:300]}")
        if trial.conclusion:
            lines.append(f"Grower conclusion: {trial.conclusion}")
        lines.append("")
    lines.append("## Ask")
    lines.append(
        "1) What should this grower double down on next year?\n"
        "2) Which trials need better design/replication?\n"
        "3) Where is money likely being left on the table?"
    )
    return "\n".join(lines)
