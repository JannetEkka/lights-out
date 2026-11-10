"""Hook behaviour and failure tests, through the service the Hermes adapter calls."""
import json

import pytest

from conftest import NOW, fixture, result_of
from lights_out import service as service_mod


def terminal(output, code=0):
    return json.dumps({"output": output, "exit_code": code, "error": None})


# -- pre_tool_call ---------------------------------------------------------------------------
def test_create_asks_with_price_and_rule_key(svc):
    d = svc.pre_terminal("aws ec2 run-instances --image-id a --instance-type t3.large")
    assert d["action"] == "approve" and d["rule_key"] == "lights-out:ec2:create"
    assert "1 EC2 t3.large: about $60.74/month" in d["message"] and "/lights keep" in d["message"]


def test_rule_key_is_per_service(svc):
    assert svc.pre_terminal("aws rds create-db-instance --db-instance-identifier d --db-instance-class db.t3.micro --engine postgres")["rule_key"] == "lights-out:rds:create"
    two = svc.pre_terminal("aws s3 mb s3://x && aws ec2 allocate-address")
    assert two["rule_key"] == "lights-out:eip+s3:create" and "S3 bucket" in two["message"] and "Elastic IP" in two["message"]


def test_non_billable_passes(svc):
    assert svc.pre_terminal("aws ec2 describe-instances") is None
    assert svc.pre_terminal("ls -la") is None
    assert svc.pre_terminal("aws ec2 terminate-instances --instance-ids i-1") is None   # deletes: Hermes's normal rules


def test_unknown_price_says_so(svc):
    d = svc.pre_terminal("aws ec2 run-instances --image-id a --instance-type nope.huge")
    assert "price unknown" in d["message"]


def test_matcher_crash_still_asks_for_aws(svc, monkeypatch):
    """Failure test: a hook exception doesn't let an unasked create through."""
    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(service_mod, "match_command", boom)
    d = svc.pre_terminal("aws ec2 run-instances --image-id a --instance-type t3.large")
    assert d["action"] == "approve" and "couldn't check" in d["message"]
    assert svc.pre_terminal("ls -la") is None          # unrelated commands are not held up


# -- post_tool_call --------------------------------------------------------------------------
def test_post_records_created_ids(svc):
    f = fixture("ec2_run_instances")
    written = svc.post_terminal(f["command"], result_of("ec2_run_instances"), "s1")
    st = svc.state()
    assert len(written) == 2 and len(st.running()) == 2
    r = st.running()[0]
    assert r.size == "t3.large" and r.delete_cmd == f"aws ec2 terminate-instances --instance-ids {r.id}"


def test_post_unknown_when_no_id(svc):
    svc.post_terminal("aws ec2 run-instances --image-id a --instance-type t3.micro", terminal("launched!"), "s1")
    st = svc.state()
    assert len(st.unknowns) == 1 and not st.running()


def test_post_nothing_when_blocked(svc):
    svc.post_terminal("aws ec2 run-instances --image-id a --instance-type t3.micro",
                      json.dumps({"error": "BLOCKED: denied by user"}), "s1")
    st = svc.state()
    assert not st.running() and not st.unknowns and st.lines == 0


def test_post_delete_marks_deleted(svc):
    f = fixture("ec2_run_instances")
    svc.post_terminal(f["command"], result_of("ec2_run_instances"), "s1")
    ids = [r.id for r in svc.state().running()]
    svc.post_terminal(f"aws ec2 terminate-instances --instance-ids {ids[0]}", terminal("{}"), "s1")
    assert [r.id for r in svc.state().running()] == ids[1:]


def test_post_ignores_untracked_delete(svc):
    svc.post_terminal("aws ec2 terminate-instances --instance-ids i-elsewhere", terminal("{}"), "s1")
    assert svc.state().lines == 0


# -- keeps -----------------------------------------------------------------------------------
def seeded(svc):
    svc.post_terminal("aws ec2 run-instances --image-id a --instance-type t3.small", terminal('{"Instances":[{"InstanceId":"i-0abc12345"}]}'), "s1")
    return "i-0abc12345"


def test_keep_proposal_goes_to_approval(svc):
    rid = seeded(svc)
    d = svc.pre_keep({"id": rid, "until": "2026-12-15", "why": "judging"})
    assert d["action"] == "approve" and "KEEP" in d["message"] and "by then" in d["message"]
    assert d["rule_key"] == f"lights-out:keep:{rid}:2026-12-15"
    assert svc.state().resources[rid].keep_until is None     # nothing set until a person approves


def test_bad_keep_proposal_is_blocked(svc):
    seeded(svc)
    assert svc.pre_keep({"id": "i-nope", "until": "2w"})["action"] == "block"
    assert svc.pre_keep({"id": "i-0abc12345", "until": "2020-01-01"})["action"] == "block"


def test_person_sets_keep(svc):
    rid = seeded(svc)
    msg = svc.keep(rid, "eod 2026-12-15", "judging", by="you", session="s1")
    assert "until 15 Dec 2026 23:59 IST" in msg
    assert "KEPT ON PURPOSE" in svc.receipt()


# -- receipt + ctx.llm -----------------------------------------------------------------------
def test_receipt_without_llm(svc):
    seeded(svc)
    assert "STILL RUNNING" in svc.receipt(sentence=svc.summary_sentence(None))


def test_llm_down_receipt_still_prints(svc):
    """Failure test: ctx.llm down still prints the receipt."""
    seeded(svc)
    def down(facts):
        raise ConnectionError("provider down")
    assert svc.summary_sentence(down) == ""
    assert "Billing now: $15.18/month" in svc.receipt(sentence="")


def test_llm_sentence_with_invented_number_is_dropped(svc):
    seeded(svc)
    assert svc.summary_sentence(lambda f: "One small server is still running at $99/month.") == ""
    assert svc.summary_sentence(lambda f: "One small server is still running, about $15.18 a month.") != ""


def test_llm_false_all_clear_is_dropped(svc):
    seeded(svc)
    assert svc.summary_sentence(lambda f: "All clear, nothing is left running.") == ""


# -- /lights out -----------------------------------------------------------------------------
def test_lights_out_needs_a_yes(svc):
    seeded(svc)
    ran = []
    out = svc.lights_out(lambda c: ran.append(c), lambda q, ch: None, confirmed=False, session="s1")
    assert ran == [] and "nothing turned off" in out and svc.state().running()


def test_lights_out_skips_kept_and_orders(svc):
    rid = seeded(svc)
    svc.post_terminal("aws ec2 allocate-address", result_of("eip_allocate"), "s1")
    svc.post_terminal("aws elbv2 create-load-balancer --name x", result_of("elb_create"), "s1")
    svc.keep(rid, "2w", "", by="you", session="s1")
    ran = []
    out = svc.lights_out(lambda c: ran.append(c) or terminal("{}"), lambda q, ch: "yes", confirmed=False, session="s1")
    assert ran[0].startswith("aws elbv2 delete-load-balancer") and ran[1].startswith("aws ec2 release-address")
    assert not any(rid in c for c in ran)
    assert "All clear · $0/month unplanned" in out


def test_lights_out_failure_keeps_resource(svc):
    seeded(svc)
    out = svc.lights_out(lambda c: terminal("An error occurred", 255), lambda q, ch: "yes", confirmed=False, session="s1")
    assert "FAIL" in out and svc.state().running()


# -- /lights check ---------------------------------------------------------------------------
def test_check_with_cloud_unreachable_changes_nothing(svc):
    """Failure test: /lights check with moto stopped changes nothing."""
    seeded(svc)
    before = svc.ledger.path.read_text()
    down = terminal("Could not connect to the endpoint URL: \"http://127.0.0.1:5000/\"", 255)
    out = svc.check(lambda c: down, session="s1")
    assert "couldn't check, ledger unchanged" in out and svc.ledger.path.read_text() == before


def test_check_marks_gone_only_on_proof(svc):
    f = fixture("ec2_run_instances")
    svc.post_terminal(f["command"], result_of("ec2_run_instances"), "s1")
    a, b = [r.id for r in svc.state().running()]

    def run(cmd):
        if a in cmd:
            return result_of("ec2_describe_terminated")
        return result_of("ec2_describe_missing")
    out = svc.check(run, session="s1")
    st = svc.state()
    assert st.resources[a].status == "gone" and st.resources[b].status == "gone"
    assert "2 marked gone" in out


def test_check_running_stays(svc):
    seeded(svc)
    run = lambda c: terminal('{"Reservations":[{"Instances":[{"InstanceId":"i-0abc12345","State":{"Name":"running"}}]}]}')
    svc.check(run, session="s1")
    assert svc.state().running()


def test_check_resolves_unknown_when_counts_match(svc):
    svc.post_terminal("aws ec2 run-instances --image-id a --instance-type t3.micro", terminal("ok"), "s1")
    run = lambda c: terminal('{"Reservations":[{"Instances":[{"InstanceId":"i-0f00f00f00"}]}]}')
    out = svc.check(run, session="s1")
    st = svc.state()
    assert not st.unknowns and st.resources["i-0f00f00f00"].size == "t3.micro" and "1 unknown resolved" in out


def test_status_tool_shape(svc):
    seeded(svc)
    s = svc.status()
    assert s["all_clear"] is False and s["still_running_unplanned"][0]["usd_per_month"] == 15.18
    assert s["still_running_unplanned"][0]["turn_off_command"].startswith("aws ec2 terminate-instances")


def test_session_notice_for_leftovers(svc):
    seeded(svc)
    assert svc.notice("s1") == ""
    note = svc.notice("s2")
    assert "from earlier sessions still running" in note and "15.18" in note


def test_unknown_resolved_right_after_the_create(svc):
    listed = []
    def run(cmd):
        listed.append(cmd)
        return terminal('{"Addresses":[{"AllocationId":"eipalloc-0aa11bb22","PublicIp":"1.2.3.4"}]}')
    svc.post_terminal("aws ec2 allocate-address --query PublicIp --output text", terminal("1.2.3.4"), "s1", run_terminal=run)
    st = svc.state()
    assert listed and listed[0].startswith("aws ec2 describe-addresses")
    assert not st.unknowns and st.resources["eipalloc-0aa11bb22"].status == "running"


def test_unknown_stays_unknown_when_counts_differ(svc):
    run = lambda c: terminal('{"Addresses":[{"AllocationId":"eipalloc-1"},{"AllocationId":"eipalloc-2"}]}')
    svc.post_terminal("aws ec2 allocate-address --query PublicIp --output text", terminal("1.2.3.4"), "s1", run_terminal=run)
    assert len(svc.state().unknowns) == 1
