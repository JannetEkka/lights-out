import json

from lights_out.ledger import Ledger


def make(tmp_path):
    L = Ledger(tmp_path / "l.jsonl")
    L.created(rid="i-1", kind="ec2", size="t3.large", hourly=0.0832, note="", delete_cmd="d1", session="s1", command="c")
    L.created(rid="vol-1", kind="ebs", size="8 GiB gp2", hourly=0.001, note="", delete_cmd="d2", session="s1", command="c")
    return L


def test_missing_file_is_empty(tmp_path):
    st = Ledger(tmp_path / "nope.jsonl").replay()
    assert st.resources == {} and st.damaged == 0


def test_replay_created_deleted(tmp_path):
    L = make(tmp_path)
    L.deleted(rid="vol-1", session="s1")
    st = L.replay()
    assert [r.id for r in st.running()] == ["i-1"] and st.resources["vol-1"].status == "deleted"


def test_damaged_line_skipped_and_counted(tmp_path):
    L = make(tmp_path)
    with open(L.path, "a") as f:
        f.write('{"event": "created", "id": "i-2"\n')     # cut off mid-line
        f.write("garbage\n")
        f.write(json.dumps({"v": 1, "event": "keep", "id": "i-404", "at": "2026-11-10T00:00:00+00:00",
                            "until": "2026-12-01T00:00:00+00:00"}) + "\n")   # keep for unknown id
    L.deleted(rid="i-1", session="s2")
    st = L.replay()
    assert st.damaged == 3
    assert st.running()[0].id == "vol-1"      # lines after the damage still replay


def test_keep_and_unkeep(tmp_path):
    from datetime import datetime, timezone
    L = make(tmp_path)
    L.keep(rid="i-1", until=datetime(2026, 12, 15, 18, 29, tzinfo=timezone.utc), why="judging", by="you", session="s1")
    assert L.replay().resources["i-1"].keep_why == "judging"
    L.unkeep(rid="i-1", session="s1")
    assert L.replay().resources["i-1"].keep_until is None


def test_unknown_and_resolved(tmp_path):
    L = make(tmp_path)
    u = L.unknown(kind="ec2", command="aws ec2 run-instances", reason="no id", session="s1")
    assert len(L.replay().unknowns) == 1
    L.resolved(uid=u["uid"], session="s1", how="by hand")
    assert L.replay().unknowns == {}


def test_lines_are_append_only(tmp_path):
    L = make(tmp_path)
    before = L.path.read_text()
    L.deleted(rid="i-1", session="s1")
    assert L.path.read_text().startswith(before)


def test_find_by_prefix(tmp_path):
    L = make(tmp_path)
    assert L.replay().find("vol").id == "vol-1"
