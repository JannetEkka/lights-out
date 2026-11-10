"""The session receipt: a fixed template over the ledger state. No model judgment in here.

Sections: still running (red), kept past its date (red), kept on purpose (until a date, with the
cost by then), unknown (amber: a create ran but no ID could be read). The green "All clear"
appears only when nothing is running unplanned, no keep has expired and nothing is unknown.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Iterable, Optional

from .ledger import Resource, State, Unknown

WIDTH = 70
RED, GREEN, AMBER, DIM, BOLD, RESET = "\033[31m", "\033[32m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"


@dataclass
class Summary:
    unplanned: list[Resource]       # running, not kept
    expired: list[Resource]         # running, keep date passed
    kept: list[Resource]            # running, keep date ahead
    unknowns: list[Unknown]
    monthly_unplanned: float        # known prices only
    monthly_kept: float
    unpriced: int                   # billing items whose price is unknown
    damaged: int

    @property
    def all_clear(self) -> bool:
        return not self.unplanned and not self.expired and not self.unknowns

    @property
    def billing_ids(self) -> list[str]:
        return [r.id for r in self.unplanned + self.expired]


def summarize(st: State, now: datetime, hours_per_month: float = 730.0) -> Summary:
    running = st.running()
    unplanned = [r for r in running if r.keep_until is None]
    expired = [r for r in running if r.keep_expired(now)]
    kept = sorted([r for r in running if r.kept(now)], key=lambda r: r.keep_until)
    unknowns = sorted(st.unknowns.values(), key=lambda u: u.at)
    mo = lambda rs: sum((r.hourly or 0.0) * hours_per_month for r in rs)
    unpriced = sum(1 for r in unplanned + expired if r.hourly is None) + sum(1 for u in unknowns)
    return Summary(sorted(unplanned, key=lambda r: r.created_at), sorted(expired, key=lambda r: r.keep_until),
                   kept, unknowns, mo(unplanned + expired), mo(kept), unpriced, st.damaged)


def money(x: Optional[float]) -> str:
    return "price ?" if x is None else f"{x:,.2f}"


def cost_until(r: Resource, now: datetime, until: Optional[datetime] = None) -> Optional[float]:
    end = until or r.keep_until
    if r.hourly is None or end is None:
        return None
    return r.hourly * max(0.0, (end - now).total_seconds() / 3600.0)


def short_id(rid: str, n: int = 28) -> str:
    if rid.startswith("arn:") and "/" in rid:
        parts = rid.split("/")
        rid = parts[-2] if len(parts) >= 3 else parts[-1]
    return rid if len(rid) <= n else rid[: n - 1] + "…"


def _when(dt: datetime, now: datetime, tz: tzinfo) -> str:
    local, today = dt.astimezone(tz), now.astimezone(tz)
    return local.strftime("%H:%M") if local.date() == today.date() else local.strftime("%d %b")


def _keep_label(dt: datetime, tz: tzinfo) -> str:
    local = dt.astimezone(tz)
    return f"{local.strftime('%d %b %H:%M')} {local.tzname() or ''}".strip()


def _row(left: str, right: str, width: int = WIDTH) -> str:
    space = max(1, width - len(left) - len(right))
    return f"{left}{' ' * space}{right}"


class _Paint:
    def __init__(self, on: bool):
        self.on = on

    def __call__(self, text: str, *codes: str) -> str:
        return f"{''.join(codes)}{text}{RESET}" if self.on and codes else text


def render(st: State, now: datetime, tz: tzinfo, *, prices_read: str, region: str = "us-east-1",
           hours_per_month: float = 730.0, color: bool = False, summary_sentence: str = "",
           style: str = "bill", turned_off: int = 0) -> str:
    s = summarize(st, now, hours_per_month)
    if style == "meter":
        return _render_meter(s, now, tz, prices_read, region, color, summary_sentence, turned_off)
    return _render_bill(s, now, tz, prices_read, region, color, summary_sentence, turned_off)


def _footer_lines(s: Summary, paint: _Paint, prices_read: str, region: str, turned_off: int) -> list[str]:
    out = []
    if turned_off:
        out.append(paint(f"Turned off this session: {turned_off}", DIM))
    if s.damaged:
        out.append(paint(f"Ledger had {s.damaged} unreadable line{'s' if s.damaged != 1 else ''} (skipped)", AMBER))
    out.append(paint(f"prices: AWS {region} on-demand, read {prices_read}", DIM))
    return out


def _render_bill(s: Summary, now: datetime, tz: tzinfo, prices_read: str, region: str, color: bool,
                 sentence: str, turned_off: int) -> str:
    p = _Paint(color)
    rule = "─" * WIDTH
    title = " LIGHTS OUT · session receipt "
    pad = (WIDTH - len(title)) // 2
    lines = [("─" * pad) + title + ("─" * (WIDTH - pad - len(title)))]

    def res_line(r: Resource) -> str:
        left = f"  {short_id(r.id):<27} {r.kind:<4}{r.size[:22]:<22}"
        right = money(r.hourly * 730 if r.hourly is not None else None)
        return _row(left + f" {_when(r.created_at, now, tz):>6}", right)

    if s.unplanned or s.expired or (not s.kept and not s.unknowns):
        lines.append(_row("STILL RUNNING", "$/month"))
        for r in s.unplanned:
            lines.append(p(res_line(r), RED))
        for r in s.expired:
            lines.append(p(res_line(r), RED))
            lines.append(p(f"    keep ended {_keep_label(r.keep_until, tz)}" + (f" · {r.keep_why}" if r.keep_why else ""), RED))
        if not s.unplanned and not s.expired:
            lines.append("  nothing")
    if s.kept:
        groups: dict[str, list[Resource]] = {}
        for r in s.kept:
            groups.setdefault(_keep_label(r.keep_until, tz), []).append(r)
        for label, rs in groups.items():
            lines.append(f"KEPT ON PURPOSE (until {label})")
            for r in rs:
                left = f"  {short_id(r.id):<27} {r.kind:<4}{r.size[:14]:<14} {r.keep_why[:12]:<12}"
                c = cost_until(r, now)
                lines.append(_row(left, f"~{money(c)} by then" if c is not None else "price ?"))
    n_unk = len(s.unknowns)
    lines.append(p(_row("UNKNOWN (couldn't read an ID: check by hand)", f"{n_unk} item{'s' if n_unk != 1 else ''}"),
                   AMBER if n_unk else ""))
    for u in s.unknowns:
        lines.append(p(f"  {u.kind:<4} {u.command[:WIDTH - 7]}", AMBER))
    lines.append(rule)
    if s.all_clear:
        total = "All clear · $0/month"
        if s.kept:
            total += f" unplanned · ${s.monthly_kept:,.2f}/month kept"
        lines.append(p(total, GREEN, BOLD))
    else:
        total = f"Billing now: ${s.monthly_unplanned:,.2f}/month"
        if s.unpriced:
            total += f" + {s.unpriced} unpriced"
        tail = "→  /lights out  to turn them off"
        if len(total) + len(tail) + 2 <= WIDTH:
            lines.append(p(_row(total, tail), RED, BOLD))
        else:
            lines.extend([p(total, RED, BOLD), _row("", tail)])
        if s.kept:
            lines.append(f"Kept on purpose: ${s.monthly_kept:,.2f}/month")
    if sentence:
        lines.append(sentence.strip()[:3 * WIDTH])
    lines.extend(_footer_lines(s, p, prices_read, region, turned_off))
    return "\n".join(lines)


def _render_meter(s: Summary, now: datetime, tz: tzinfo, prices_read: str, region: str, color: bool,
                  sentence: str, turned_off: int) -> str:
    """Layout B: a one-line meter, then one line per item."""
    p = _Paint(color)
    head = (p("● ALL CLEAR · $0/month", GREEN, BOLD) if s.all_clear
            else p(f"● STILL BILLING · ${s.monthly_unplanned:,.2f}/month", RED, BOLD))
    lines = [f"Lights Out  {head}"]
    for r in s.unplanned + s.expired:
        tag = "kept past date" if r in s.expired else "running"
        mo = money(r.hourly * 730 if r.hourly is not None else None)
        lines.append(p(f"  ✗ {r.kind} {r.size} {short_id(r.id)} · {tag} · ${mo}/mo", RED))
        lines.append(p(f"      off: {r.delete_cmd}", DIM))
    for r in s.kept:
        c = cost_until(r, now)
        lines.append(f"  ◷ {r.kind} {r.size} {short_id(r.id)} · kept until {_keep_label(r.keep_until, tz)}"
                     + (f" · ~${money(c)} by then" if c is not None else ""))
    for u in s.unknowns:
        lines.append(p(f"  ? {u.kind} · no ID read · {u.command[:40]}", AMBER))
    if not s.all_clear:
        lines.append("  /lights out turns off everything not kept")
    if sentence:
        lines.append("  " + sentence.strip())
    lines.extend("  " + x for x in _footer_lines(s, p, prices_read, region, turned_off))
    return "\n".join(lines)


def session_notice(st: State, now: datetime, tz: tzinfo, current_session: str,
                   hours_per_month: float = 730.0) -> str:
    """At a new session's start: leftovers from earlier sessions and keeps whose date passed."""
    s = summarize(st, now, hours_per_month)
    left = [r for r in s.unplanned if r.session != current_session]
    if not left and not s.expired and not s.unknowns:
        return ""
    parts = []
    if left:
        mo = sum((r.hourly or 0) * hours_per_month for r in left)
        parts.append(f"{len(left)} resource{'s' if len(left) != 1 else ''} from earlier sessions still running "
                     f"(${mo:,.2f}/month): " + ", ".join(f"{r.kind} {short_id(r.id)}" for r in left[:6]))
    if s.expired:
        parts.append(f"{len(s.expired)} keep{'s' if len(s.expired) != 1 else ''} past date: "
                     + ", ".join(f"{r.kind} {short_id(r.id)} (ended {_keep_label(r.keep_until, tz)})" for r in s.expired[:6]))
    if s.unknowns:
        parts.append(f"{len(s.unknowns)} unknown create{'s' if len(s.unknowns) != 1 else ''} to check by hand")
    return "Lights Out: " + "; ".join(parts) + ". /lights shows the ledger, /lights out turns them off."


def ids_named(s: Summary) -> Iterable[str]:
    return [r.id for r in s.unplanned + s.expired + s.kept]
