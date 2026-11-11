"""Re-score saved runs (A/B) from their raw files with the current matcher; nothing is re-run.
Uses each run's saved transcript.jsonl, approvals.json and moto snapshot (moto_end)."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(BENCH.parent))
import harness as H  # noqa: E402
import run_bench as R  # noqa: E402
from lights_out.ledger import State  # noqa: E402
from lights_out.pricing import load_prices  # noqa: E402
from lights_out.yamlish import load_yaml  # noqa: E402

tasks = {t["id"]: t for t in load_yaml((BENCH / "tasks.yaml").read_text())}
for arm in sys.argv[1:] or ["A", "B"]:
    for f in sorted((BENCH / "runs" / arm).glob("*/metrics.json")):
        old = json.loads(f.read_text())
        msgs = []
        tj = f.parent / "transcript.jsonl"
        for line in tj.read_text().splitlines() if tj.exists() else []:
            msgs.extend(json.loads(line).get("messages", []))
        appr = json.loads((f.parent / "approvals.json").read_text())
        sess = SimpleNamespace(session_id=old["session_id"], approvals=[SimpleNamespace(**a) for a in appr])
        inv = [{k: v for k, v in it.items() if k != "usd_month"} for it in old["moto_end"]]
        new = R.score(tasks[old["task"]], arm, inv, None, msgs, sess, "", State(), load_prices())
        new["prompt"], new["finished"] = old["prompt"], old["finished"]
        changed = {k: (old[k], new[k]) for k in ("billable_create_commands", "unapproved_creates", "left_running_unintended") if old[k] != new[k]}
        if changed:
            print(arm, old["task"], changed)
        f.write_text(json.dumps(new, indent=1, default=str))
