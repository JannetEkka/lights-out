from datetime import timedelta

from conftest import NOW
from lights_out.keep import parse_keep
from lights_out.ledger import Ledger
from lights_out.receipt import render, summarize
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def R(L, **kw):
    return render(L.replay(), NOW, IST, prices_read="2026-11-10", **kw)


def add(L, rid, kind="ec2", size="t3.large", hourly=0.0832):
    L.created(rid=rid, kind=kind, size=size, hourly=hourly, note="", delete_cmd=f"del {rid}", session="s1", command="c")


def test_empty_ledger_is_green(tmp_path):
    out = R(Ledger(tmp_path / "l"))
    assert "All clear · $0/month" in out and "Billing now" not in out


def test_running_is_red_with_total(tmp_path):
    L = Ledger(tmp_path / "l")
    add(L, "i-0a1b2c")
    add(L, "db-test", "rds", "db.t3.micro postgres", 0.017)
    out = R(L, color=True)
    assert "\033[31m" in out and "Billing now: $73.15/month" in out and "All clear" not in out
    assert "i-0a1b2c" in out and "60.74" in out and "12.41" in out


def test_unknown_blocks_green(tmp_path):
    L = Ledger(tmp_path / "l")
    L.unknown(kind="ec2", command="aws ec2 run-instances --output text", reason="no id", session="s1")
    out = R(L)
    assert "All clear" not in out and "1 item" in out and "unpriced" in out


def test_kept_on_purpose_with_cost_by_then(tmp_path):
    L = Ledger(tmp_path / "l")
    add(L, "i-9f8e7d", size="t3.small", hourly=0.0208)
    L.keep(rid="i-9f8e7d", until=parse_keep("2026-12-15", NOW, IST), why="demo-server", by="you", session="s1")
    out = R(L)
    assert "KEPT ON PURPOSE (until 15 Dec 23:59 IST)" in out
    hours = (parse_keep("2026-12-15", NOW, IST) - NOW).total_seconds() / 3600
    assert f"~{0.0208 * hours:,.2f} by then" in out
    assert "All clear · $0/month unplanned" in out          # kept is not a leftover


def test_keep_past_date_is_red_again(tmp_path):
    L = Ledger(tmp_path / "l")
    add(L, "i-old", size="t3.small", hourly=0.0208)
    L.keep(rid="i-old", until=NOW - timedelta(hours=1), why="judging", by="you", session="s1")
    s = summarize(L.replay(), NOW)
    assert [r.id for r in s.expired] == ["i-old"] and not s.all_clear
    assert "keep ended" in R(L)


def test_deleted_turns_green(tmp_path):
    L = Ledger(tmp_path / "l")
    add(L, "i-1")
    L.deleted(rid="i-1", session="s1")
    out = R(L, color=True, turned_off=1)
    assert "\033[32m" in out and "All clear · $0/month" in out and "Turned off this session: 1" in out


def test_damaged_lines_reported(tmp_path):
    L = Ledger(tmp_path / "l")
    L.path.write_text("garbage\n")
    assert "Ledger had 1 unreadable line" in R(L)


def test_price_unknown_shown_not_guessed(tmp_path):
    L = Ledger(tmp_path / "l")
    add(L, "i-x", size="nope.huge", hourly=None)
    out = R(L)
    assert "price ?" in out and "Billing now: $0.00/month + 1 unpriced" in out


def test_meter_layout(tmp_path):
    L = Ledger(tmp_path / "l")
    add(L, "i-1")
    out = R(L, style="meter")
    assert "STILL BILLING · $60.74/month" in out and "off: del i-1" in out


def test_prices_line_and_no_color_by_default(tmp_path):
    out = R(Ledger(tmp_path / "l"))
    assert "prices: AWS us-east-1 on-demand, read 2026-11-10" in out and "\033[" not in out
