"""Record the demo: one real Hermes session against a fresh moto, saved as an asciicast v2 file
(demo/demo.cast) with markers at each step. Nothing is mocked; the harness answers each approval
prompt "yes" like a person would. Render it with demo/render.py.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pexpect

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "bench"))
import harness as H  # noqa: E402

HOME = "/home/user/hh"
COLS, ROWS = 118, 32
TURNS = [
    ("turn1", "Launch a t3.large EC2 instance (image ami-12345678) to test my script on, plus a test Postgres "
              "database (db.t3.micro, 20 GB, user testadmin, password Test12345!), with the aws CLI. "
              "Tell me when both are ready."),
    ("turn2", "Also launch a t3.small demo server (image ami-12345678) for the hackathon judges. It must stay "
              "up until judging ends on 2026-12-15, so keep it until then."),
    ("out", "/lights out"),
]


def main() -> None:
    ledger = Path(HOME) / "plugin-data" / "lights-out" / "ledger.jsonl"
    ledger.unlink(missing_ok=True)
    H.stop_gateways(HOME)
    moto = H.Moto(HERE / "moto.log")
    work = HERE / "work"
    work.mkdir(exist_ok=True)
    env = H.base_env(HOME, moto.url)
    cast = open(HERE / "demo.cast", "w")
    cast.write(json.dumps({"version": 2, "width": COLS, "height": ROWS, "timestamp": int(time.time()),
                           "env": {"TERM": "xterm-256color"}, "title": "Lights Out demo"}) + "\n")
    t0 = time.time()

    def ev(kind: str, data: str) -> None:
        cast.write(json.dumps([round(time.time() - t0, 4), kind, data]) + "\n")
        cast.flush()

    child = pexpect.spawn(H.HERMES, ["chat", "--cli", "--in", str(work)], env=env, encoding="utf-8",
                          codec_errors="replace", timeout=30, dimensions=(ROWS, COLS))
    buf = ""
    boxes = 0

    def pump(secs: float) -> bool:
        nonlocal buf
        got, end = False, time.time() + secs
        while time.time() < end:
            try:
                data = child.read_nonblocking(65536, timeout=0.3)
                buf += data
                ev("o", data)
                got = True
            except pexpect.TIMEOUT:
                pass
            except pexpect.EOF:
                return got
        return got

    def wait_turn(idle: float = 20.0) -> None:
        nonlocal boxes
        last = time.time()
        while time.time() - last < 900:
            if pump(0.5):
                last = time.time()
            text = H.clean(buf)
            n = text.count("Allow once")
            if n > boxes:
                boxes = n
                pump(2.0)
                ev("m", "approval")
                time.sleep(1.5)          # a person reads the price
                child.send("\r")
                ev("m", "approved")
                last = time.time()
                continue
            n_pick = text.count("Yes, turn them off")
            if n_pick and "picked" not in globals():
                globals()["picked"] = True
                pump(1.5)
                ev("m", "confirm")
                child.send("\r")
                last = time.time()
                continue
            if time.time() - last > idle and "Working…" not in H.clean(buf[-3000:])[-400:]:
                return

    pump(8)
    for name, text in TURNS:
        ev("m", name)
        for ch in text:                  # typed, so the viewer sees the ask
            child.send(ch)
            time.sleep(0.01)
        time.sleep(0.5)
        child.send("\r")
        wait_turn()
        ev("m", name + "_done")
    if "picked" not in globals() and "turning off" not in H.clean(buf):
        ev("m", "out_yes")
        child.send("/lights out yes\r")
        wait_turn()
    ev("m", "end")
    child.send("/exit\r")
    pump(8)
    child.terminate(force=True)
    cast.close()
    print("moto after:", H.moto_inventory(moto.url))
    moto.stop()
    H.stop_gateways(HOME)


if __name__ == "__main__":
    main()
