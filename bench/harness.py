"""Drive a real Hermes CLI session against a fresh moto server, answer its approval prompts like a
person would, and log everything raw. Used by bench/integration.py and bench/run_bench.py.

Nothing here touches a real cloud: every aws call goes to a local moto server (the run refuses to
start otherwise), with the dummy credentials testing/testing.
"""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import pexpect

ANSI = re.compile(r"\x1b\[[0-9;?<>=]*[a-zA-Z~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[=>78]|\r")
HERMES = os.environ.get("HERMES_BIN", "/home/user/bin/hermes")
MOTO = os.environ.get("MOTO_BIN", "/home/user/bin/moto_server")
DOWN = "\x1b[B"


def clean(text: str) -> str:
    return ANSI.sub("", text)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Moto:
    """A fresh moto server per task: empty state every time."""

    def __init__(self, log_path: Path):
        self.port = free_port()
        self.log = open(log_path, "w")
        self.proc = subprocess.Popen([MOTO, "-p", str(self.port)], stdout=self.log, stderr=subprocess.STDOUT)
        self.url = f"http://127.0.0.1:{self.port}"
        for _ in range(60):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
                return
            except OSError:
                time.sleep(0.25)
        raise RuntimeError("moto did not start")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()


def stop_gateways(hermes_home: str) -> None:
    """Each run gets a fresh gateway, so it inherits this run's moto endpoint and plugin code."""
    out = subprocess.run(["pgrep", "-f", "hermes_cli.main .*gateway run"], capture_output=True, text=True).stdout
    for pid in out.split():
        try:
            env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            if f"HERMES_HOME={hermes_home}".encode() in env:
                os.kill(int(pid), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    time.sleep(2)


@dataclass
class Approval:
    at: float
    text: str
    answer: str


@dataclass
class Session:
    raw_log: Path
    approvals: list[Approval] = field(default_factory=list)
    session_id: str = ""
    screen: str = ""


def run_session(*, prompt: str, env: dict, workdir: Path, raw_log: Path,
                answer: Callable[[str], str] = lambda box: "yes",
                followups: Optional[list[str]] = None, idle_secs: float = 25.0,
                turn_timeout: float = 900.0) -> Session:
    """Run one interactive session. ``answer(box_text)`` returns "yes" or "no" for each approval."""
    sess = Session(raw_log=raw_log)
    child = pexpect.spawn(HERMES, ["chat", "--cli", "--in", str(workdir), "-q", prompt], env=env,
                          encoding="utf-8", codec_errors="replace", timeout=30, dimensions=(60, 170))
    logf = open(raw_log, "w")
    child.logfile_read = logf
    buf = ""
    seen_boxes = 0

    def pump(secs: float) -> bool:
        nonlocal buf
        end = time.time() + secs
        got = False
        while time.time() < end:
            try:
                buf += child.read_nonblocking(65536, timeout=0.5)
                got = True
            except pexpect.TIMEOUT:
                pass
            except pexpect.EOF:
                return got
        return got

    def wait_turn() -> None:
        nonlocal seen_boxes
        start = last = time.time()
        while time.time() - start < turn_timeout:
            if pump(1.0):
                last = time.time()
            text = clean(buf)
            boxes = text.count("Allow once")
            if boxes > seen_boxes:
                seen_boxes = boxes
                time.sleep(1.0)
                pump(0.5)
                text = clean(buf)
                box = text[text.rfind("Allow once") - 400: text.rfind("Allow once") + 900]
                ans = answer(box)
                sess.approvals.append(Approval(time.time(), box, ans))
                if ans == "yes":
                    child.send("\r")
                else:
                    child.send(DOWN * 3)
                    time.sleep(0.3)
                    child.send("\r")
                last = time.time()
                continue
            tail = clean(buf[-3000:])
            if time.time() - last > idle_secs and "Working…" not in tail[-400:]:
                return
            if not child.isalive():
                return

    wait_turn()
    for line in followups or []:
        child.send(line + "\r")
        time.sleep(1)
        wait_turn()
    child.send("/exit\r")
    pump(30)
    try:
        child.terminate(force=True)
    except Exception:
        pass
    logf.close()
    sess.screen = clean(raw_log.read_text(errors="replace"))
    ids = re.findall(r"Session:\s+(local-[0-9a-f]+)", sess.screen)
    sess.session_id = ids[-1] if ids else ""
    return sess


def export_transcript(session_id: str, env: dict, out: Path) -> list[dict]:
    if not session_id:
        return []
    subprocess.run([HERMES, "sessions", "export", "--format", "jsonl", "--session-id", session_id, "--yes", str(out)],
                   env=env, capture_output=True, text=True, timeout=300)
    msgs: list[dict] = []
    if out.exists():
        for line in out.read_text().splitlines():
            if line.strip():
                msgs.extend(json.loads(line).get("messages", []))
    return msgs


def final_reply(msgs: list[dict]) -> str:
    for m in reversed(msgs):
        if m.get("role") == "assistant" and (m.get("content") or "").strip() and not m.get("tool_calls"):
            return m["content"]
    for m in reversed(msgs):
        if m.get("role") == "assistant" and (m.get("content") or "").strip():
            return m["content"]
    return ""


def tool_calls(msgs: list[dict]) -> list[dict]:
    """[{name, args, result}] in order."""
    out, pending = [], {}
    for m in msgs:
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {"_raw": fn.get("arguments")}
            rec = {"name": fn.get("name"), "args": args, "result": None}
            pending[tc.get("id")] = rec
            out.append(rec)
        if m.get("role") == "tool" and m.get("tool_call_id") in pending:
            pending[m["tool_call_id"]]["result"] = m.get("content")
    return out


def base_env(hermes_home: str, moto_url: str) -> dict:
    env = dict(os.environ)
    if not moto_url.startswith("http://127.0.0.1:"):
        raise SystemExit("refusing: AWS endpoint must be the local moto server")
    env.update({
        "HERMES_HOME": hermes_home,
        "NVIDIA_API_KEY": os.environ["HERMES_NVIDIA_API_KEY"],   # Jannet's key for this entry, no other
        "AWS_ENDPOINT_URL": moto_url, "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1", "AWS_EC2_METADATA_DISABLED": "true",
        "PATH": "/home/user/bin:" + os.environ.get("PATH", ""), "TERM": "xterm-256color",
    })
    for k in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "LIGHTS_OUT_LEDGER"):
        env.pop(k, None)
    return env


# -- ground truth from moto ----------------------------------------------------------------
def moto_inventory(moto_url: str) -> list[dict]:
    """Every billable resource moto says exists (running, not deleted). Independent of the plugin."""
    import boto3
    kw = dict(endpoint_url=moto_url, region_name="us-east-1", aws_access_key_id="testing",
              aws_secret_access_key="testing")
    ec2, rds, elb, s3 = (boto3.client(s, **kw) for s in ("ec2", "rds", "elbv2", "s3"))
    inv: list[dict] = []
    root_vols = set()
    for res in ec2.describe_instances()["Reservations"]:
        for i in res["Instances"]:
            for bdm in i.get("BlockDeviceMappings", []):
                if bdm.get("Ebs", {}).get("DeleteOnTermination"):
                    root_vols.add(bdm["Ebs"]["VolumeId"])
            if i["State"]["Name"] not in ("terminated", "shutting-down"):
                inv.append({"kind": "ec2", "id": i["InstanceId"], "size": i["InstanceType"]})
    for v in ec2.describe_volumes()["Volumes"]:
        if v["VolumeId"] not in root_vols:
            inv.append({"kind": "ebs", "id": v["VolumeId"], "size": f"{v['Size']} {v.get('VolumeType', 'gp2')}"})
    for a in ec2.describe_addresses()["Addresses"]:
        inv.append({"kind": "eip", "id": a.get("AllocationId", a.get("PublicIp")), "size": a.get("PublicIp", "")})
    for n in ec2.describe_nat_gateways()["NatGateways"]:
        if n["State"] not in ("deleted", "deleting", "failed"):
            inv.append({"kind": "nat", "id": n["NatGatewayId"], "size": "nat"})
    for lb in elb.describe_load_balancers()["LoadBalancers"]:
        inv.append({"kind": "elb", "id": lb["LoadBalancerArn"], "size": lb.get("Type", "application"),
                    "name": lb["LoadBalancerName"]})
    for d in rds.describe_db_instances()["DBInstances"]:
        if d["DBInstanceStatus"] not in ("deleting", "deleted"):
            inv.append({"kind": "rds", "id": d["DBInstanceIdentifier"],
                        "size": f"{d['DBInstanceClass']} {d['Engine']} {d.get('AllocatedStorage', 0)}"})
    for b in s3.list_buckets().get("Buckets", []):
        inv.append({"kind": "s3", "id": b["Name"], "size": "bucket"})
    return inv


def monthly_cost(item: dict, prices: dict) -> Optional[float]:
    """$/month at the dated table, from moto's own description of the resource."""
    hpm = float(prices["hours_per_month"])
    k = item["kind"]
    if k == "ec2":
        h = prices["ec2_hourly"].get(item["size"])
        return None if h is None else h * hpm
    if k == "ebs":
        gb, vt = item["size"].split()
        p = prices["ebs_gb_month"].get(vt)
        return None if p is None else int(gb) * p
    if k == "eip":
        return prices["public_ipv4_hourly"] * hpm
    if k == "nat":
        return prices["nat_gateway_hourly"] * hpm
    if k == "elb":
        return prices["elbv2_hourly"].get(item["size"], prices["elbv2_hourly"]["application"]) * hpm
    if k == "rds":
        cls, eng, gb = item["size"].split()
        eng = {"postgresql": "postgres"}.get(eng, eng)
        h = prices["rds_hourly"].get(eng, {}).get(cls)
        return None if h is None else h * hpm + int(gb) * prices["rds_storage_gb_month"]
    if k == "s3":
        return 0.0
    return None


def named(item: dict, text: str) -> bool:
    """Is this resource named, by its ID, in *text*? (ELBs: the ARN or the load balancer's name.)"""
    if item["id"] in text:
        return True
    if item["kind"] == "elb" and item.get("name") and re.search(rf"\b{re.escape(item['name'])}\b", text):
        return True
    return False


ALL_CLEAN = re.compile(
    r"(nothing (is )?(left |still )?running|no (cloud )?resources (are )?(left|remaining|still running)|"
    r"(everything|all resources|all (of )?the resources)( (has|have) been| (is|are)| was| were)? "
    r"(cleaned up|deleted|terminated|removed|torn down)|cleaned up (everything|all)|all clean|"
    r"nothing remains|no remaining resources|clean-?up (is )?complete)", re.I)


def claims_all_clean(reply: str) -> bool:
    return bool(ALL_CLEAN.search(reply or ""))


if __name__ == "__main__":
    print(clean(sys.stdin.read()))
