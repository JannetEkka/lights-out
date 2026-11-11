"""Append-only JSON-lines ledger. The current state is rebuilt by replaying every line.

Events: ``created``, ``deleted``, ``gone`` (``/lights check`` saw it missing), ``keep``,
``unkeep``, ``unknown`` (a create ran but no ID could be read) and ``resolved`` (an unknown
cleared by ``/lights check`` or by hand). A line that can't be read is skipped and counted;
the receipt says so. A missing file is an empty ledger.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = 1
_LOCK = threading.Lock()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Resource:
    id: str
    kind: str
    size: str
    hourly: Optional[float]
    note: str
    delete_cmd: str
    created_at: datetime
    session: str
    command: str = ""
    region: str = ""
    status: str = "running"          # running | deleted | gone
    ended_at: Optional[datetime] = None
    keep_until: Optional[datetime] = None
    keep_why: str = ""
    keep_by: str = ""

    def kept(self, now: datetime) -> bool:
        return self.keep_until is not None and self.keep_until > now

    def keep_expired(self, now: datetime) -> bool:
        return self.keep_until is not None and self.keep_until <= now


@dataclass
class Unknown:
    uid: str
    kind: str
    command: str
    reason: str
    at: datetime
    session: str
    size: str = ""
    hourly: Optional[float] = None


@dataclass
class State:
    resources: dict[str, Resource] = field(default_factory=dict)
    unknowns: dict[str, Unknown] = field(default_factory=dict)
    damaged: int = 0
    lines: int = 0

    def running(self) -> list[Resource]:
        return [r for r in self.resources.values() if r.status == "running"]

    def find(self, rid: str) -> Optional[Resource]:
        if rid in self.resources:
            return self.resources[rid]
        hits = [r for r in self.resources.values() if r.id.endswith(rid) or r.id.startswith(rid)]
        return hits[0] if len(hits) == 1 else None


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)

    # -- writing ---------------------------------------------------------------------------
    def append(self, event: str, **fields: Any) -> dict:
        rec = {"v": SCHEMA, "event": event, "at": fields.pop("at", None) or iso(utcnow()), **fields}
        line = json.dumps(rec, ensure_ascii=False, sort_keys=True)
        with _LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        return rec

    def created(self, *, rid: str, kind: str, size: str, hourly: Optional[float], note: str,
                delete_cmd: str, session: str, command: str, region: str = "") -> dict:
        return self.append("created", id=rid, kind=kind, size=size, hourly=hourly, note=note,
                           delete_cmd=delete_cmd, session=session, command=command[:500], region=region)

    def deleted(self, *, rid: str, session: str, command: str = "") -> dict:
        return self.append("deleted", id=rid, session=session, command=command[:500])

    def gone(self, *, rid: str, session: str, evidence: str) -> dict:
        return self.append("gone", id=rid, session=session, evidence=evidence[:300])

    def keep(self, *, rid: str, until: datetime, why: str, by: str, session: str) -> dict:
        return self.append("keep", id=rid, until=iso(until), why=why[:200], by=by, session=session)

    def unkeep(self, *, rid: str, session: str) -> dict:
        return self.append("unkeep", id=rid, session=session)

    def unknown(self, *, kind: str, command: str, reason: str, session: str, size: str = "",
                hourly: Optional[float] = None) -> dict:
        return self.append("unknown", uid=uuid.uuid4().hex[:8], kind=kind, command=command[:500],
                           reason=reason, session=session, size=size, hourly=hourly)

    def resolved(self, *, uid: str, session: str, how: str, rid: str = "") -> dict:
        return self.append("resolved", uid=uid, session=session, how=how[:200], id=rid)

    # -- reading ---------------------------------------------------------------------------
    def replay(self) -> State:
        st = State()
        try:
            fh = open(self.path, encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return st
        with fh:
            for raw in fh:
                if not raw.strip():
                    continue
                st.lines += 1
                try:
                    rec = json.loads(raw)
                    if not isinstance(rec, dict) or "event" not in rec:
                        raise ValueError("not an event")
                    _apply(st, rec)
                except Exception:
                    st.damaged += 1
        return st


def _apply(st: State, rec: dict) -> None:
    ev = rec["event"]
    at = parse_iso(rec["at"])
    if ev == "created":
        st.resources[rec["id"]] = Resource(
            id=rec["id"], kind=rec["kind"], size=rec.get("size", ""),
            hourly=(float(rec["hourly"]) if rec.get("hourly") is not None else None),
            note=rec.get("note", ""), delete_cmd=rec["delete_cmd"], created_at=at,
            session=rec.get("session", ""), command=rec.get("command", ""), region=rec.get("region", ""))
    elif ev in ("deleted", "gone"):
        r = st.resources.get(rec["id"])
        if r is not None:
            r.status, r.ended_at = ev, at
    elif ev == "keep":
        r = st.resources.get(rec["id"])
        if r is None:
            raise ValueError(f"keep for unknown id {rec['id']}")
        r.keep_until, r.keep_why, r.keep_by = parse_iso(rec["until"]), rec.get("why", ""), rec.get("by", "")
    elif ev == "unkeep":
        r = st.resources.get(rec["id"])
        if r is not None:
            r.keep_until, r.keep_why, r.keep_by = None, "", ""
    elif ev == "unknown":
        st.unknowns[rec["uid"]] = Unknown(
            uid=rec["uid"], kind=rec["kind"], command=rec.get("command", ""), reason=rec.get("reason", ""),
            at=at, session=rec.get("session", ""), size=rec.get("size", ""),
            hourly=(float(rec["hourly"]) if rec.get("hourly") is not None else None))
    elif ev == "resolved":
        st.unknowns.pop(rec["uid"], None)
    else:
        raise ValueError(f"unknown event {ev}")
