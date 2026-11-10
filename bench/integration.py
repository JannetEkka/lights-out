"""Integration test: real Hermes + moto + the NVIDIA model.

  1. One create gets the approval prompt; answer NO  -> nothing exists in moto, nothing in the ledger.
  2. Same create; answer YES                          -> the ledger has a line with moto's instance ID.
  3. /lights out (answered yes)                       -> moto has nothing running.

Run: python bench/integration.py   (raw logs land in bench/integration/)
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import harness as H  # noqa: E402
from lights_out.ledger import Ledger  # noqa: E402

HOME = "/home/user/hh"
LEDGER = Path(HOME) / "plugin-data" / "lights-out" / "ledger.jsonl"
OUT = Path(__file__).resolve().parent / "integration"
PROMPT = ("Launch one t3.large EC2 instance with image ami-12345678 using the aws CLI, then tell me its "
          "instance ID. If it can't be launched, just tell me why; don't retry.")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    work = OUT / "work"
    work.mkdir(exist_ok=True)
    LEDGER.unlink(missing_ok=True)
    moto = H.Moto(OUT / "moto.log")
    results = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "checks": []}

    def check(name, ok, detail):
        results["checks"].append({"check": name, "pass": bool(ok), "detail": detail})
        print(("PASS " if ok else "FAIL ") + name + ": " + json.dumps(detail)[:300])

    try:
        env = H.base_env(HOME, moto.url)
        # 1: answer no
        H.stop_gateways(HOME)
        s1 = H.run_session(prompt=PROMPT, env=env, workdir=work, raw_log=OUT / "1-no.log", answer=lambda b: "no")
        inv = H.moto_inventory(moto.url)
        st = Ledger(LEDGER).replay()
        lo_boxes = [a for a in s1.approvals if "Lights Out: this starts" in a.text]
        check("no: approval prompt shown with price", bool(lo_boxes) and "$60.74/month" in lo_boxes[0].text,
              {"approvals": len(s1.approvals)})
        check("no: nothing in moto", inv == [], inv)
        check("no: nothing created in ledger", not st.resources, [r.id for r in st.resources.values()])

        # 2: answer yes, then /lights out
        H.stop_gateways(HOME)
        s2 = H.run_session(prompt=PROMPT, env=env, workdir=work, raw_log=OUT / "2-yes.log",
                           answer=lambda b: "yes", followups=["/lights out yes"])
        st = Ledger(LEDGER).replay()
        created = [r for r in st.resources.values()]
        import boto3
        ec2 = boto3.client("ec2", endpoint_url=moto.url, region_name="us-east-1",
                           aws_access_key_id="testing", aws_secret_access_key="testing")
        all_ids = [i["InstanceId"] for r in ec2.describe_instances()["Reservations"] for i in r["Instances"]]
        check("yes: ledger ID is moto's instance ID", len(created) == 1 and created[0].id in all_ids,
              {"ledger": [r.id for r in created], "moto": all_ids})
        inv = H.moto_inventory(moto.url)
        check("/lights out: moto has nothing running", inv == [], inv)
        check("/lights out: ledger shows it deleted", created and created[0].status == "deleted",
              [r.status for r in created])
        check("receipt printed after the turn", "LIGHTS OUT · session receipt" in s2.screen, "")
        check("green all clear after /lights out", "All clear · $0/month" in s2.screen, "")
        for n, s in (("1-no", s1), ("2-yes", s2)):
            (OUT / f"{n}.approvals.json").write_text(json.dumps([a.__dict__ for a in s.approvals], indent=1))
            H.export_transcript(s.session_id, env, OUT / f"{n}.transcript.jsonl")
    finally:
        moto.stop()
        H.stop_gateways(HOME)
    results["ledger"] = LEDGER.read_text() if LEDGER.exists() else ""
    (OUT / "results.json").write_text(json.dumps(results, indent=1))
    ok = all(c["pass"] for c in results["checks"])
    print("INTEGRATION", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
