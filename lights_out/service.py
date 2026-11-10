"""Lights Out's behaviour, kept free of Hermes imports so it can be unit-tested.

The Hermes adapter (the package ``__init__.py``) passes in a ``run_terminal`` callable (Hermes's
own terminal tool, through ``ctx.dispatch_tool``), an ``ask`` callable (``ctx.ask_choice``) and
an optional ``llm`` callable. Everything that decides is here and is deterministic.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable, Optional

from . import parse as P
from .keep import KeepError, parse_keep, resolve_tz
from .ledger import Ledger, State, utcnow
from .matcher import Match, load_rules, match_command
from .pricing import Price, load_prices, price_for
from .receipt import money, render, session_notice, short_id, summarize, cost_until

log = logging.getLogger("lights-out")

KIND_NAMES = {"ec2": "EC2", "ebs": "EBS volume", "eip": "Elastic IP", "nat": "NAT gateway",
              "elb": "load balancer", "rds": "RDS", "s3": "S3 bucket"}
# Turn-off order: things that hold others first (LB, NAT before the EIP they use; instances before volumes)
OFF_ORDER = {"elb": 0, "nat": 1, "ec2": 2, "rds": 3, "ebs": 4, "eip": 5, "s3": 6}
NOT_FOUND = re.compile(
    r"InvalidInstanceID\.NotFound|InvalidVolume\.NotFound|InvalidAllocationID\.NotFound|NatGatewayNotFound|"
    r"NatGatewayMalformed|LoadBalancerNotFound|DBInstanceNotFound|NoSuchBucket|\(404\)|Not Found", re.I)
GONE_STATES = {"terminated", "deleted", "shutting-down"}
# When a turn-off fails for a known reason, say what to run (Lights Out doesn't run it for you).
DELETE_HINTS = {
    "rds": (re.compile(r"protected DB|DeletionProtection", re.I),
            "deletion protection is on; to allow the delete: aws rds modify-db-instance "
            "--db-instance-identifier {id} --no-deletion-protection --apply-immediately"),
    "ec2": (re.compile(r"OperationNotPermitted|DisableApiTermination", re.I),
            "termination protection is on; to allow it: aws ec2 modify-instance-attribute "
            "--instance-id {id} --no-disable-api-termination"),
}


class LightsOut:
    def __init__(self, ledger_path: Path, *, tz_name: str = "", style: str = "bill",
                 rules: dict | None = None, prices: dict | None = None,
                 clock: Callable[[], datetime] = utcnow):
        self.ledger = Ledger(ledger_path)
        self.tz_name = tz_name
        self.style = style if style in ("bill", "meter") else "bill"
        self.rules = rules or load_rules()
        self.prices = prices or load_prices()
        self.clock = clock

    # -- helpers ---------------------------------------------------------------------------
    @property
    def tz(self) -> tzinfo:
        try:
            return resolve_tz(self.tz_name)
        except KeepError:
            return resolve_tz("")

    @property
    def hpm(self) -> float:
        return float(self.prices.get("hours_per_month", 730))

    def state(self) -> State:
        return self.ledger.replay()

    def describe_price(self, m: Match, pr: Price) -> str:
        name = KIND_NAMES.get(m.kind, m.kind)
        what = f"{pr.count} {name} {pr.size}".replace("  ", " ") if m.kind != "s3" else "1 S3 bucket"
        if not pr.known:
            return f"{what}: {pr.note}, billed until deleted"
        mo = pr.hourly * self.hpm * pr.count
        each = f" ({pr.count} × ${pr.hourly * self.hpm:,.2f})" if pr.count > 1 else ""
        extra = f" {pr.note}" if pr.note else ""
        return f"{what}: about ${mo:,.2f}/month{each}{extra}, billed until deleted"

    # -- pre_tool_call ---------------------------------------------------------------------
    def pre_terminal(self, command: Any) -> Optional[dict]:
        """``approve`` directive for a billable create; ``None`` for anything else."""
        try:
            creates = [m for m in match_command(command, self.rules) if m.action == "create"]
        except Exception as e:  # never let a matcher bug wave a create through
            if isinstance(command, str) and re.search(r"\baws\b", command):
                log.exception("lights-out matcher failed")
                return {"action": "approve", "rule_key": "lights-out:unchecked",
                        "message": f"Lights Out couldn't check this aws command ({type(e).__name__}); asking you to be safe."}
            return None
        if not creates:
            return None
        parts = [self.describe_price(m, price_for(m, self.prices)) for m in creates]
        services = sorted({m.service for m in creates})
        msg = ("Lights Out: this starts " + "; ".join(parts) + ". "
               "To keep it past this session: /lights keep <id> <date>.")
        return {"action": "approve", "message": msg, "rule_key": f"lights-out:{'+'.join(services)}:create"}

    def pre_keep(self, args: dict) -> dict:
        """Hermes proposed a keep (``lights_keep``): send it to the approval gate, or block a bad one."""
        ok, info = self._check_keep(args.get("id", ""), args.get("until", ""))
        if not ok:
            return {"action": "block", "message": f"Lights Out: {info}"}
        r, until = info
        c = cost_until(r, self.clock(), until)
        why = str(args.get("why") or "").strip()
        local = until.astimezone(self.tz)
        msg = (f"Hermes proposes to KEEP {KIND_NAMES.get(r.kind, r.kind)} {r.id} ({r.size}) running until "
               f"{local:%d %b %Y %H:%M} {local.tzname()}: ~${money(c)} by then"
               + (f". Reason given: {why[:120]}" if why else "")
               + ". It will not be flagged as a leftover until then. Approve only if you asked for this.")
        return {"action": "approve", "message": msg, "rule_key": f"lights-out:keep:{r.id}:{local.date().isoformat()}"}

    def _check_keep(self, rid: str, when: str):
        st = self.state()
        r = st.find(str(rid).strip())
        if r is None:
            return False, f"no running resource {rid!r} in the ledger (see /lights)"
        if r.status != "running":
            return False, f"{r.id} is already {r.status}"
        try:
            until = parse_keep(str(when), self.clock(), self.tz)
        except KeepError as e:
            return False, str(e)
        return True, (r, until)

    # -- post_tool_call --------------------------------------------------------------------
    def post_terminal(self, command: Any, result: Any, session: str,
                      run_terminal: Optional[Callable[[str], Any]] = None) -> list[dict]:
        """Write what a terminal call created or deleted. Returns the ledger records written.
        If a create ran but its output had no ID (``--query ... --output text``), and *run_terminal*
        is given, list that resource type read-only and pair it, only when the counts match exactly."""
        written: list[dict] = []
        matches = match_command(command, self.rules)
        if not matches:
            return written
        seen: set[str] = set()
        for m in matches:
            oc = P.outcome(m, result)
            if m.action == "create":
                pr = price_for(m, self.prices)
                if oc.status == "created":
                    for rid in oc.ids:
                        if rid in seen:
                            continue
                        seen.add(rid)
                        written.append(self.ledger.created(
                            rid=rid, kind=m.kind, size=pr.size, hourly=pr.hourly, note=pr.note,
                            delete_cmd=m.rule["delete"].format(id=rid), session=session,
                            command=m.segment, region=m.region))
                elif oc.status == "unknown":
                    written.append(self.ledger.unknown(kind=m.kind, command=m.segment, reason=oc.reason,
                                                       session=session, size=pr.size, hourly=pr.hourly))
            elif oc.status == "deleted":
                st = self.state()
                for rid in oc.ids:
                    if rid in st.resources and st.resources[rid].status == "running":
                        written.append(self.ledger.deleted(rid=rid, session=session, command=m.segment))
        if run_terminal is not None and any(w.get("event") == "unknown" for w in written):
            try:
                self._resolve_unknowns(run_terminal, self.state(), session, [])
            except Exception:
                log.exception("lights-out: could not resolve an unknown create")
        return written

    # -- keeps -----------------------------------------------------------------------------
    def keep(self, rid: str, when: str, why: str, *, by: str, session: str) -> str:
        ok, info = self._check_keep(rid, when)
        if not ok:
            return f"Lights Out: {info}"
        r, until = info
        self.ledger.keep(rid=r.id, until=until, why=why, by=by, session=session)
        local = until.astimezone(self.tz)
        c = cost_until(r, self.clock(), until)
        return (f"Kept on purpose: {KIND_NAMES.get(r.kind, r.kind)} {r.id} ({r.size}) until "
                f"{local:%d %b %Y %H:%M} {local.tzname()}, ~${money(c)} by then.")

    def unkeep(self, rid: str, session: str) -> str:
        st = self.state()
        r = st.find(rid.strip())
        if r is None or r.keep_until is None:
            return f"Lights Out: {rid!r} has no keep."
        self.ledger.unkeep(rid=r.id, session=session)
        return f"Keep removed: {r.id} counts as still running again."

    # -- receipt ---------------------------------------------------------------------------
    def receipt(self, *, color: bool = False, sentence: str = "", session: str = "") -> str:
        st = self.state()
        turned_off = 0
        if session:
            turned_off = sum(1 for r in st.resources.values()
                             if r.status == "deleted" and r.ended_at is not None and r.session == session)
        return render(st, self.clock(), self.tz, prices_read=str(self.prices["read"]),
                      region=str(self.prices["region"]), hours_per_month=self.hpm, color=color,
                      summary_sentence=sentence, style=self.style, turned_off=turned_off)

    def facts(self) -> str:
        s = summarize(self.state(), self.clock(), self.hpm)
        return json.dumps({
            "still_running_unplanned": [f"{r.kind} {r.size}" for r in s.unplanned],
            "kept_past_date": [f"{r.kind} {r.size}" for r in s.expired],
            "kept_on_purpose": [f"{r.kind} {r.size} until {r.keep_until.astimezone(self.tz):%d %b}" for r in s.kept],
            "unknown_creates": len(s.unknowns),
            "monthly_unplanned_usd": round(s.monthly_unplanned, 2),
            "monthly_kept_usd": round(s.monthly_kept, 2),
            "all_clear": s.all_clear,
        })

    def summary_sentence(self, llm: Optional[Callable[[str], str]]) -> str:
        """One optional plain-language line from the user's model. Dropped unless every number in it
        is one of the receipt's own numbers, so it can't contradict the receipt."""
        if llm is None:
            return ""
        facts = self.facts()
        try:
            text = llm(facts) or ""
        except Exception as e:
            log.info("lights-out: summary sentence skipped (%s)", type(e).__name__)
            return ""
        text = " ".join(str(text).split())
        if not text or len(text) > 220:
            return ""
        allowed = set(re.findall(r"\d+(?:\.\d+)?", facts.replace(",", "")))
        for num in re.findall(r"\d+(?:\.\d+)?", text.replace(",", "")):
            if num not in allowed:
                return ""
        s = json.loads(facts)
        if s["all_clear"] is False and re.search(r"\b(all clear|nothing (is )?(left|running)|all clean)\b", text, re.I):
            return ""
        return text

    def notice(self, session: str) -> str:
        return session_notice(self.state(), self.clock(), self.tz, session, self.hpm)

    def touched(self, session: str) -> bool:
        """Show a receipt at session end only if Lights Out has something to say."""
        st = self.state()
        if st.running() or st.unknowns or st.damaged:
            return True
        return any(r.session == session for r in st.resources.values())

    # -- status (tool + /lights) -----------------------------------------------------------
    def status(self) -> dict:
        now = self.clock()
        s = summarize(self.state(), now, self.hpm)

        def row(r):
            d = {"id": r.id, "kind": r.kind, "size": r.size,
                 "usd_per_month": None if r.hourly is None else round(r.hourly * self.hpm, 2),
                 "turn_off_command": r.delete_cmd, "created": r.created_at.isoformat(timespec="seconds")}
            if r.keep_until:
                d["kept_until"] = r.keep_until.astimezone(self.tz).isoformat(timespec="minutes")
                d["keep_reason"] = r.keep_why
            return d
        return {
            "all_clear": s.all_clear,
            "still_running_unplanned": [row(r) for r in s.unplanned],
            "kept_past_date": [row(r) for r in s.expired],
            "kept_on_purpose": [row(r) for r in s.kept],
            "unknown": [{"kind": u.kind, "command": u.command, "reason": u.reason} for u in s.unknowns],
            "usd_per_month_unplanned": round(s.monthly_unplanned, 2),
            "usd_per_month_kept": round(s.monthly_kept, 2),
            "unreadable_ledger_lines": s.damaged,
            "prices": f"AWS {self.prices['region']} on-demand, read {self.prices['read']}",
            "note": "Only a person can set a keep: lights_keep asks them first.",
        }

    # -- /lights out -----------------------------------------------------------------------
    def out_targets(self) -> list:
        s = summarize(self.state(), self.clock(), self.hpm)
        return sorted(s.unplanned + s.expired, key=lambda r: (OFF_ORDER.get(r.kind, 9), r.created_at))

    def lights_out(self, run_terminal: Callable[[str], Any], ask: Callable[[str, list], Optional[str]],
                   *, confirmed: bool, session: str, color: bool = False) -> str:
        targets = self.out_targets()
        if not targets:
            return "Lights Out: nothing to turn off.\n\n" + self.receipt(session=session, color=color)
        total = sum((r.hourly or 0) * self.hpm for r in targets)
        listing = "\n".join(f"  {r.kind:<4} {short_id(r.id, 40):<40} {r.delete_cmd}" for r in targets)
        if not confirmed:
            answer = ask(f"Turn off {len(targets)} resource{'s' if len(targets) != 1 else ''} "
                         f"(${total:,.2f}/month)?\n{listing}",
                         [{"value": "yes", "label": "Yes, turn them off"}, {"value": "no", "label": "No"}])
            if answer != "yes":
                return "Lights Out: nothing turned off (no yes given).\n" + listing
        lines = []
        for r in targets:
            res = run_terminal(r.delete_cmd)
            out, code, err = P.terminal_result(res)
            if code == 0:
                self.ledger.deleted(rid=r.id, session=session, command=r.delete_cmd)
                lines.append(f"  off   {r.kind:<4} {short_id(r.id, 40)}")
            else:
                why = (err or out or "no output").strip().splitlines()
                lines.append(f"  FAIL  {r.kind:<4} {short_id(r.id, 40)}: {why[-1][:120] if why else ''}")
                hint = DELETE_HINTS.get(r.kind)
                if hint and hint[0].search(f"{out}\n{err}"):
                    lines.append("        " + hint[1].format(id=r.id))
        return "Lights Out: turning off\n" + "\n".join(lines) + "\n\n" + self.receipt(session=session, color=color)

    # -- /lights check ---------------------------------------------------------------------
    def check(self, run_terminal: Callable[[str], Any], *, session: str) -> str:
        """Compare the ledger with the cloud using read-only describe calls. A resource is marked gone
        only on proof (a not-found error or a terminated/deleted state). Failures change nothing."""
        st = self.state()
        rules_by_kind = {r["kind"]: r for r in self.rules["creates"]}
        lines, changed = [], 0
        for r in st.running():
            rule = rules_by_kind.get(r.kind)
            if not rule or not rule.get("describe"):
                continue
            res = run_terminal(rule["describe"].format(id=r.id))
            out, code, err = P.terminal_result(res)
            text = f"{out}\n{err}"
            state = _resource_state(r.kind, out)
            if code == 0 and state in GONE_STATES:
                self.ledger.gone(rid=r.id, session=session, evidence=f"state {state}")
                lines.append(f"  gone     {r.kind:<4} {short_id(r.id, 40)} (state: {state})")
                changed += 1
            elif code not in (0, None) and NOT_FOUND.search(text):
                self.ledger.gone(rid=r.id, session=session, evidence=NOT_FOUND.search(text).group(0))
                lines.append(f"  gone     {r.kind:<4} {short_id(r.id, 40)} (not found)")
                changed += 1
            elif code == 0:
                lines.append(f"  running  {r.kind:<4} {short_id(r.id, 40)}" + (f" ({state})" if state else ""))
            else:
                last = (text.strip().splitlines() or ["no output"])[-1][:100]
                lines.append(f"  ?        {r.kind:<4} {short_id(r.id, 40)}: couldn't check, ledger unchanged ({last})")
        resolved = self._resolve_unknowns(run_terminal, st, session, lines, how="matched by /lights check")
        head = f"Lights Out check: {changed} marked gone, {resolved} unknown{'s' if resolved != 1 else ''} resolved."
        return head + ("\n" + "\n".join(lines) if lines else "\n  (nothing in the ledger to check)")

    def _resolve_unknowns(self, run_terminal, st: State, session: str, lines: list,
                          how: str = "matched by a read-only list right after the create") -> int:
        if not st.unknowns:
            return 0
        rules_by_kind = {r["kind"]: r for r in self.rules["creates"]}
        resolved = 0
        for kind in sorted({u.kind for u in st.unknowns.values()}):
            rule = rules_by_kind.get(kind)
            lister = (self.rules.get("listers") or {}).get(kind)
            if not rule or not lister:
                continue
            out, code, err = P.terminal_result(run_terminal(lister["command"]))
            if code != 0:
                lines.append(f"  ?        could not list {kind} to resolve unknowns, ledger unchanged")
                continue
            live = []
            for d in P.json_documents(out):
                live.extend(P.walk_path(d, lister["path"]))
            known = set(st.resources)
            untracked = [i for i in dict.fromkeys(live) if i not in known]
            unk = sorted((u for u in st.unknowns.values() if u.kind == kind), key=lambda u: u.at)
            if untracked and len(untracked) == len(unk):
                for u, rid in zip(unk, untracked):
                    self.ledger.created(rid=rid, kind=kind, size=u.size, hourly=u.hourly, note="",
                                        delete_cmd=rule["delete"].format(id=rid), session=u.session,
                                        command=u.command, region="")
                    self.ledger.resolved(uid=u.uid, session=session, how=how, rid=rid)
                    lines.append(f"  found    {kind:<4} {short_id(rid, 40)} (was unknown)")
                    resolved += 1
            else:
                lines.append(f"  ?        {len(unk)} unknown {kind} create(s), {len(untracked)} untracked {kind} "
                             "in the account: can't pair them safely; check by hand")
        return resolved


def _resource_state(kind: str, output: str) -> str:
    for d in P.json_documents(output):
        for path in ("Reservations[].Instances[].State.Name", "Volumes[].State", "NatGateways[].State",
                     "LoadBalancers[].State.Code", "DBInstances[].DBInstanceStatus"):
            vals = P.walk_path(d, path)
            if vals:
                return vals[0]
        if kind == "eip" and isinstance(d, dict) and d.get("Addresses") == []:
            return "deleted"
    return ""
