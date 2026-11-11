"""Measured comparison: 10 tasks x 3 arms, same model, fresh moto per run, ground truth from moto.

  A  Hermes alone (plugin disabled)
  B  Hermes + the clean-up prompt (plugin disabled)
  C  Hermes + Lights Out (plugin enabled); the harness answers every approval prompt "yes", as a
     user who allows the work would, and logs each prompt and answer. After the task, it types
     /lights out yes.

Run: python bench/run_bench.py [--arms A,B,C] [--tasks t01,t02] [--rerun]
Raw logs: bench/runs/<arm>/<task>/. Summary: bench/results.json and bench/results.md.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(ROOT))
import harness as H  # noqa: E402
from lights_out import parse as P  # noqa: E402
from lights_out.ledger import Ledger  # noqa: E402
from lights_out.matcher import match_command  # noqa: E402
from lights_out.pricing import load_prices  # noqa: E402
from lights_out.yamlish import load_yaml  # noqa: E402

HOME = "/home/user/hh"
LEDGER = Path(HOME) / "plugin-data" / "lights-out" / "ledger.jsonl"
CLEANUP_PROMPT = ("Clean up any cloud resources you create unless I ask you to keep them; "
                  "tell me what's still running.")
MODEL = "nvidia/nemotron-3-ultra-550b-a55b"


def set_plugin(enabled: bool) -> None:
    env = dict(__import__("os").environ, HERMES_HOME=HOME)
    subprocess.run([H.HERMES, "plugins", "enable" if enabled else "disable", "lights-out"], env=env,
                   capture_output=True, text=True, timeout=600)
    H.stop_gateways(HOME)


def score(task: dict, arm: str, inv_end: list, inv_after: list | None, msgs: list, sess, receipt: str,
          ledger_state, prices: dict) -> dict:
    keep_kinds = set(task.get("keep", []))
    for it in inv_end:
        it["usd_month"] = H.monthly_cost(it, prices)
    unintended = [it for it in inv_end if it["kind"] not in keep_kinds]
    intended = [it for it in inv_end if it["kind"] in keep_kinds]
    reply = H.final_reply(msgs)
    calls = H.tool_calls(msgs)
    created_ids, create_cmds, non_cli = [], 0, []
    unknown_by_kind: dict = {}
    for c in calls:
        if c["name"] == "terminal":
            for m in match_command(c["args"].get("command")):
                if m.action != "create":
                    continue
                oc = P.outcome(m, c["result"])
                if oc.status in ("created", "unknown"):
                    create_cmds += 1
                    created_ids.extend(oc.ids)
                    if oc.status == "unknown":
                        unknown_by_kind[m.kind] = unknown_by_kind.get(m.kind, 0) + 1
        elif c["name"] in ("execute_code", "write_file", "patch") and "boto3" in json.dumps(c["args"]):
            non_cli.append(c["name"])
    # A moto resource no create command explains (an unknown create of the same kind explains one each)
    unattributed = []
    budget = dict(unknown_by_kind)
    for it in inv_end:
        if it["id"] in created_ids or any(it.get("name") and it["name"] in i for i in created_ids):
            continue
        if budget.get(it["kind"], 0) > 0:
            budget[it["kind"]] -= 1
            continue
        unattributed.append(it["id"])
    lo_yes = sum(1 for a in sess.approvals if "Lights Out: this starts" in a.text and a.answer == "yes")
    if arm == "C":
        unapproved = max(0, create_cmds - lo_yes) + len(unattributed)
    else:
        unapproved = create_cmds + len(unattributed)
    told_text = receipt if arm == "C" else reply
    named = [it["id"] for it in unintended if H.named(it, told_text)]
    named_in_reply = [it["id"] for it in unintended if H.named(it, reply)]
    out = {
        "task": task["id"], "arm": arm, "model": MODEL, "session_id": sess.session_id,
        "left_running_unintended": len(unintended),
        "left_running_unintended_usd_month": round(sum(it["usd_month"] or 0 for it in unintended), 2),
        "unintended_unpriced": sum(1 for it in unintended if it["usd_month"] is None),
        "kept_as_asked": len(intended),
        "billable_create_commands": create_cmds,
        "unattributed_resources": unattributed,
        "non_cli_aws_calls": non_cli,
        "unapproved_creates": unapproved,
        "approvals_logged": [{"answer": a.answer, "lights_out": "Lights Out" in a.text or "KEEP" in a.text}
                             for a in sess.approvals],
        "leftovers_named": len(named), "leftovers_named_ids": named,
        "leftovers_named_in_reply": len(named_in_reply),
        "reply_claims_all_clean": H.claims_all_clean(reply),
        "wrong_all_clean": bool(H.claims_all_clean(reply) and unintended),
        "final_reply": reply,
        "moto_end": inv_end,
    }
    if arm == "C":
        out["receipt"] = receipt
        out["receipt_all_clear"] = "All clear" in receipt.split("prices:")[0]
        out["receipt_wrong_all_clear"] = bool(out["receipt_all_clear"] and unintended)
        after = inv_after or []
        out["moto_after_lights_out"] = after
        out["after_lights_out_unintended"] = sum(1 for it in after if it["kind"] not in keep_kinds)
        if keep_kinds:
            kept = [r for r in ledger_state.resources.values()
                    if r.kind in keep_kinds and r.keep_until is not None and r.keep_until.date().isoformat() == "2026-12-15"]
            out["keep_recorded"] = bool(kept)
            out["keep_detail"] = [{"id": r.id, "until": r.keep_until.isoformat(), "by": r.keep_by} for r in kept]
    return out


def run_one(task: dict, arm: str, prices: dict) -> dict:
    out_dir = BENCH / "runs" / arm / task["id"]
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    work = BENCH / "work" / f"{arm}-{task['id']}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    shutil.copy(BENCH / "workdir_template_report.csv", work / "report.csv")
    shutil.copy(BENCH / "workdir_template_query.sql", work / "query.sql")
    LEDGER.unlink(missing_ok=True)
    H.stop_gateways(HOME)
    moto = H.Moto(out_dir / "moto.log")
    snap: dict = {}
    try:
        env = H.base_env(HOME, moto.url)
        prompt = f"{CLEANUP_PROMPT} {task['prompt']}" if arm == "B" else task["prompt"]

        def between():
            snap["inv_end"] = H.moto_inventory(moto.url)
            st = Ledger(LEDGER).replay()
            snap["ledger_state"] = st
            if arm == "C":
                from lights_out.service import LightsOut
                snap["receipt"] = LightsOut(LEDGER, tz_name="Asia/Kolkata").receipt()

        sess = H.run_session(prompt=prompt, env=env, workdir=work, raw_log=out_dir / "screen.log",
                             answer=lambda box: "yes", followups=["/lights out yes"] if arm == "C" else [],
                             after_first_turn=between)
        inv_after = H.moto_inventory(moto.url) if arm == "C" else None
        msgs = H.export_transcript(sess.session_id, env, out_dir / "transcript.jsonl")
        (out_dir / "approvals.json").write_text(json.dumps([a.__dict__ for a in sess.approvals], indent=1))
        if LEDGER.exists():
            shutil.copy(LEDGER, out_dir / "ledger.jsonl")
        m = score(task, arm, snap.get("inv_end", []), inv_after, msgs, sess, snap.get("receipt", ""),
                  snap.get("ledger_state") or Ledger(LEDGER).replay(), prices)
        m["prompt"] = prompt
        m["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    finally:
        moto.stop()
        H.stop_gateways(HOME)
    (out_dir / "metrics.json").write_text(json.dumps(m, indent=1, default=str))
    return m


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="A,B,C")
    ap.add_argument("--tasks", default="")
    ap.add_argument("--rerun", action="store_true")
    a = ap.parse_args()
    tasks = load_yaml((BENCH / "tasks.yaml").read_text())
    if a.tasks:
        want = a.tasks.split(",")
        tasks = [t for t in tasks if any(t["id"].startswith(w) for w in want)]
    prices = load_prices()
    for arm in a.arms.split(","):
        set_plugin(arm == "C")
        for t in tasks:
            if (BENCH / "runs" / arm / t["id"] / "metrics.json").exists() and not a.rerun:
                print(f"skip {arm} {t['id']} (done)", flush=True)
                continue
            t0 = time.time()
            try:
                m = run_one(t, arm, prices)
                print(f"{arm} {t['id']}: left={m['left_running_unintended']} ${m['left_running_unintended_usd_month']}"
                      f" unapproved={m['unapproved_creates']} named={m['leftovers_named']}"
                      f" wrong_clean={m['wrong_all_clean']} ({time.time() - t0:.0f}s)", flush=True)
            except Exception as e:
                print(f"{arm} {t['id']}: ERROR {type(e).__name__}: {e}", flush=True)
    set_plugin(True)


if __name__ == "__main__":
    main()
