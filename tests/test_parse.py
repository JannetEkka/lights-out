import json

from conftest import fixture, result_of
from lights_out.matcher import match_command
from lights_out.parse import json_documents, outcome, terminal_result, walk_path


def oc(name, cmd=None):
    f = fixture(name)
    m = match_command(cmd or f["command"])[0]
    return outcome(m, result_of(name))


def test_run_instances_two_ids():
    o = oc("ec2_run_instances")
    assert o.status == "created" and len(o.ids) == 2 and all(i.startswith("i-") for i in o.ids)


def test_run_instances_text_output_uses_pattern():
    o = oc("ec2_run_instances_text")
    assert o.status == "created" and len(o.ids) == 1 and o.ids[0].startswith("i-")


def test_volume_eip_nat_elb():
    assert oc("ebs_create_volume").ids[0].startswith("vol-")
    assert oc("eip_allocate").ids[0].startswith("eipalloc-")
    nat = oc("nat_create")
    assert nat.ids[0].startswith("nat-") and len(nat.ids) == 1   # not the EIP inside the output
    assert oc("elb_create").ids[0].startswith("arn:aws:elasticloadbalancing:")


def test_rds_and_s3():
    assert oc("rds_create").ids == ["db-test"]
    assert oc("s3api_create").ids == ["report-bucket-1"]
    assert oc("s3_mb").ids == ["report-bucket-2"]


def test_unknown_type_still_recorded_by_id():
    o = oc("ec2_bad_type")
    assert o.status == "created" and o.ids


def test_plain_text_without_id_is_unknown():
    m = match_command("aws ec2 run-instances --image-id a --instance-type t3.micro")[0]
    o = outcome(m, json.dumps({"output": "Instance launched OK", "exit_code": 0}))
    assert o.status == "unknown" and o.ids == []


def test_failed_create():
    m = match_command("aws ec2 run-instances --image-id a --instance-type t3.micro")[0]
    o = outcome(m, json.dumps({"output": "An error occurred (InvalidAMIID.Malformed)", "exit_code": 255}))
    assert o.status == "failed"


def test_blocked_call_is_not_run():
    m = match_command("aws ec2 run-instances --image-id a --instance-type t3.micro")[0]
    o = outcome(m, json.dumps({"error": "BLOCKED: approval required"}))
    assert o.status == "not_run"


def test_s3_mb_wrong_bucket_name_not_recorded():
    m = match_command("aws s3 mb s3://other")[0]
    assert outcome(m, result_of("s3_mb")).status == "unknown"


def test_delete_outcomes():
    f = fixture("ec2_terminate")
    m = match_command(f["command"])[0]
    assert outcome(m, result_of("ec2_terminate")).status == "deleted"
    m2 = match_command("aws ec2 terminate-instances --instance-ids i-1")[0]
    assert outcome(m2, json.dumps({"output": "error", "exit_code": 255})).status == "failed"


def test_terminal_result_shapes():
    assert terminal_result('{"output": "x", "exit_code": 0}') == ("x", 0, "")
    assert terminal_result("not json")[1] is None
    assert terminal_result({"output": "y", "exit_code": "3"}) == ("y", 3, "")


def test_json_documents_concatenated():
    docs = json_documents('{"a": 1}\nnoise\n[1, 2]{"b": {"c": 3}}')
    assert docs == [{"a": 1}, [1, 2], {"b": {"c": 3}}]


def test_walk_path_nested_lists():
    d = {"Reservations": [{"Instances": [{"InstanceId": "i-1"}, {"InstanceId": "i-2"}]}]}
    assert walk_path(d, "Reservations[].Instances[].InstanceId") == ["i-1", "i-2"]
