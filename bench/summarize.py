"""Summarise bench/runs/*/*/metrics.json into bench/results.json and bench/results.md."""
from __future__ import annotations

import json
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ARMS = {"A": "Hermes alone", "B": "Hermes + clean-up prompt", "C": "Hermes + Lights Out"}


def load() -> dict:
    out: dict = {}
    for f in sorted((BENCH / "runs").glob("*/*/metrics.json")):
        m = json.loads(f.read_text())
        out.setdefault(m["arm"], []).append(m)
    return out


def main() -> None:
    runs = load()
    summary = {}
    for arm, ms in sorted(runs.items()):
        left = sum(m["left_running_unintended"] for m in ms)
        named = sum(m["leftovers_named"] for m in ms)
        s = {
            "runs": len(ms),
            "left_running_unintended": left,
            "left_running_usd_month": round(sum(m["left_running_unintended_usd_month"] for m in ms), 2),
            "unpriced_leftovers": sum(m["unintended_unpriced"] for m in ms),
            "billable_creates": sum(m["billable_create_commands"] for m in ms),
            "unapproved_creates": sum(m["unapproved_creates"] for m in ms),
            "leftovers_named": named,
            "leftovers_named_share": None if left == 0 else round(named / left, 3),
            "wrong_all_clean_claims": sum(1 for m in ms if m["wrong_all_clean"]),
            "tasks_with_leftovers": sum(1 for m in ms if m["left_running_unintended"]),
            "kept_as_asked": f"{sum(1 for m in ms if m['kept_as_asked'])} of {sum(1 for m in ms if m['task'] in ('t09-keep-demo', 't10-keep-db'))}",
        }
        if arm == "C":
            s["after_lights_out_unintended"] = sum(m.get("after_lights_out_unintended", 0) for m in ms)
            keeps = [m for m in ms if "keep_recorded" in m]
            s["keeps_recorded"] = f"{sum(1 for m in keeps if m['keep_recorded'])} of {len(keeps)}"
            s["receipt_wrong_all_clear"] = sum(1 for m in ms if m.get("receipt_wrong_all_clear"))
            s["leftovers_named_in_reply"] = sum(m["leftovers_named_in_reply"] for m in ms)
        summary[arm] = s
    (BENCH / "results.json").write_text(json.dumps(summary, indent=1))

    def cell(arm, key, fmt=str):
        v = summary.get(arm, {}).get(key)
        return "–" if v is None else fmt(v)

    pct = lambda v: f"{v * 100:.0f}%"
    lines = ["| | " + " | ".join(f"{a}: {ARMS[a]}" for a in "ABC") + " |", "|---|---|---|---|"]
    rows = [
        ("Resources left running unintentionally (10 tasks)", "left_running_unintended", str),
        ("… their cost, $/month at the dated price table", "left_running_usd_month", lambda v: f"${v:,.2f}"),
        ("Billable creates run", "billable_creates", str),
        ("Billable creates no person approved", "unapproved_creates", str),
        ("Leftovers named by ID (reply for A/B, receipt for C)", "leftovers_named_share", pct),
        ("Wrong \"all clean\" claims", "wrong_all_clean_claims", str),
        ("\"Stay up until 2026-12-15\" resources still up", "kept_as_asked", str),
    ]
    for label, key, fmt in rows:
        lines.append(f"| {label} | " + " | ".join(cell(a, key, fmt) for a in "ABC") + " |")
    if "C" in summary:
        c = summary["C"]
        lines.append(f"| After `/lights out`: still running, not kept | – | – | {c['after_lights_out_unintended']} |")
        lines.append(f"| Keeps recorded for the two \"stay up until 2026-12-15\" tasks | – | – | {c['keeps_recorded']} |")
    per = ["", "| Task | " + " | ".join(f"{a} left (named)" for a in "ABC") + " |", "|---|---|---|---|"]
    tasks = sorted({m["task"] for ms in runs.values() for m in ms})
    for t in tasks:
        cells = []
        for a in "ABC":
            m = next((x for x in runs.get(a, []) if x["task"] == t), None)
            cells.append("–" if m is None else f"{m['left_running_unintended']} ({m['leftovers_named']})"
                         + (" ⚠ claims clean" if m["wrong_all_clean"] else ""))
        per.append(f"| {t} | " + " | ".join(cells) + " |")
    (BENCH / "results.md").write_text("\n".join(lines + per) + "\n")
    print("\n".join(lines + per))


if __name__ == "__main__":
    main()
