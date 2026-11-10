import pytest

from lights_out.matcher import match_command, split_simple_commands
from lights_out.pricing import price_for


def keys(cmd):
    return [(m.action, m.key) for m in match_command(cmd)]


CREATES = [
    ("aws ec2 run-instances --image-id ami-1 --instance-type t3.large", "ec2:create"),
    ("aws ec2 run-instances --instance-type=t3.large --image-id ami-1", "ec2:create"),
    ("aws --region us-east-1 ec2 run-instances --image-id ami-1 --instance-type t3.micro", "ec2:create"),
    ("aws --debug ec2 run-instances --image-id ami-1", "ec2:create"),
    ("aws --output json --profile x ec2 run-instances --count 2 --instance-type t3.small --image-id a", "ec2:create"),
    ("/usr/local/bin/aws ec2 run-instances --image-id ami-1 --instance-type t3.large", "ec2:create"),
    ("AWS_PAGER= aws ec2 run-instances --image-id ami-1 --instance-type t3.large", "ec2:create"),
    ("sudo -E aws ec2 run-instances --image-id ami-1 --instance-type t3.large", "ec2:create"),
    ("timeout 60 aws ec2 run-instances --image-id ami-1 --instance-type t3.large", "ec2:create"),
    ("aws ec2 create-volume --size 100 --volume-type gp3 --availability-zone us-east-1a", "ebs:create"),
    ("aws ec2 allocate-address --domain vpc", "eip:create"),
    ("aws ec2 create-nat-gateway --subnet-id subnet-1 --allocation-id eipalloc-1", "nat:create"),
    ("aws elbv2 create-load-balancer --name lb --subnets s-1 s-2", "elb:create"),
    ("aws rds create-db-instance --db-instance-identifier db --db-instance-class db.t3.micro --engine postgres --allocated-storage 20", "rds:create"),
    ("aws s3api create-bucket --bucket my-bucket", "s3:create"),
    ("aws s3 mb s3://my-bucket", "s3:create"),
    ("bash -c 'aws ec2 run-instances --image-id ami-1 --instance-type t3.large'", "ec2:create"),
    ("sh -lc \"aws s3 mb s3://x\"", "s3:create"),
]


@pytest.mark.parametrize("cmd,key", CREATES)
def test_creates(cmd, key):
    assert keys(cmd) == [("create", key)]


DELETES = [
    ("aws ec2 terminate-instances --instance-ids i-1 i-2", "ec2:delete", ["i-1", "i-2"]),
    ("aws ec2 terminate-instances --instance-ids i-1,i-2", "ec2:delete", ["i-1", "i-2"]),
    ("aws ec2 delete-volume --volume-id vol-1", "ebs:delete", ["vol-1"]),
    ("aws ec2 release-address --allocation-id eipalloc-1", "eip:delete", ["eipalloc-1"]),
    ("aws ec2 delete-nat-gateway --nat-gateway-id nat-1", "nat:delete", ["nat-1"]),
    ("aws elbv2 delete-load-balancer --load-balancer-arn arn:aws:x", "elb:delete", ["arn:aws:x"]),
    ("aws rds delete-db-instance --db-instance-identifier db --skip-final-snapshot", "rds:delete", ["db"]),
    ("aws s3api delete-bucket --bucket b1", "s3:delete", ["b1"]),
    ("aws s3 rb s3://b2 --force", "s3:delete", ["b2"]),
    ("aws s3 rb --force s3://b3/", "s3:delete", ["b3"]),
]


@pytest.mark.parametrize("cmd,key,ids", DELETES)
def test_deletes(cmd, key, ids):
    ms = match_command(cmd)
    assert [(m.action, m.key) for m in ms] == [("delete", key)]
    assert ms[0].target_ids() == ids


NEAR_MISSES = [
    "aws ec2 describe-instances",
    "aws ec2 describe-instances --filters Name=instance-type,Values=t3.large",
    "aws ec2 run-instances --dry-run --image-id ami-1 --instance-type t3.large",
    "aws ec2 run-instances --generate-cli-skeleton",
    "aws ec2 describe-volumes",
    "aws ec2 create-tags --resources i-1 --tags Key=a,Value=b",
    "aws ec2 create-security-group --group-name g --description d",
    "aws rds describe-db-instances",
    "aws s3 ls",
    "aws s3 cp report.csv s3://bucket/report.csv",
    "aws elbv2 describe-load-balancers",
    "echo aws ec2 run-instances",
    "grep run-instances notes.txt",
    "awslocal-not ec2 run-instances",
    "aws help",
    "",
    "ls -la",
    "python3 -c 'import boto3'",
]


@pytest.mark.parametrize("cmd", NEAR_MISSES)
def test_near_misses(cmd):
    assert [m for m in match_command(cmd) if m.action == "create"] == []


def test_non_string_command():
    assert match_command(None) == [] and match_command(["aws"]) == []


def test_chain_and():
    assert keys("aws s3 mb s3://a && aws ec2 run-instances --image-id x --instance-type t3.micro") == [
        ("create", "s3:create"), ("create", "ec2:create")]


def test_chain_semicolon_and_pipe():
    assert keys("cd /tmp; aws ec2 allocate-address | jq .AllocationId") == [("create", "eip:create")]


def test_chain_or_and_newline():
    assert keys("aws ec2 create-volume --size 8 --availability-zone a || echo fail\naws s3 mb s3://b") == [
        ("create", "ebs:create"), ("create", "s3:create")]


def test_quoted_separator_is_not_split():
    # the ';' is inside quotes: one command, a describe
    assert keys("aws ec2 describe-instances --query 'Reservations[];x'") == []


def test_create_then_delete_in_one_line():
    assert keys("aws s3 mb s3://a && aws s3 rb s3://a") == [("create", "s3:create"), ("delete", "s3:delete")]


def test_unbalanced_quote_still_matched():
    assert ("create", "ec2:create") in keys("aws ec2 run-instances --image-id ami-1 --instance-type t3.large --tag 'oops")


def test_region_is_read():
    m = match_command("aws ec2 run-instances --region eu-west-1 --instance-type t3.large --image-id a")[0]
    assert m.region == "eu-west-1"


def test_split_simple_commands():
    assert split_simple_commands("a b && c 'd;e' | f") == [["a", "b"], ["c", "d;e"], ["f"]]


# --- prices ---------------------------------------------------------------------------------
def price(cmd):
    return price_for(match_command(cmd)[0])


def test_price_t3_large():
    p = price("aws ec2 run-instances --instance-type t3.large --image-id a")
    assert p.hourly == pytest.approx(0.0832) and round(p.monthly(), 2) == 60.74


def test_price_count():
    p = price("aws ec2 run-instances --instance-type t3.small --count 3 --image-id a")
    assert p.count == 3 and p.hourly == pytest.approx(0.0208)


def test_price_unknown_type_is_not_guessed():
    p = price("aws ec2 run-instances --instance-type nope.huge --image-id a")
    assert p.hourly is None and "unknown" in p.note


def test_price_other_region_unknown():
    p = price("aws ec2 run-instances --region eu-west-1 --instance-type t3.large --image-id a")
    assert p.hourly is None


def test_price_no_type():
    assert price("aws ec2 run-instances --image-id a").hourly is None


def test_price_ebs_gp3():
    p = price("aws ec2 create-volume --size 100 --volume-type gp3 --availability-zone a")
    assert round(p.monthly(), 2) == 8.00


def test_price_rds_with_storage():
    p = price("aws rds create-db-instance --db-instance-identifier d --db-instance-class db.t3.micro --engine postgres --allocated-storage 20")
    assert round(p.monthly(), 2) == round(0.018 * 730 + 20 * 0.115, 2)


def test_price_nat_eip_elb_s3():
    assert round(price("aws ec2 create-nat-gateway --subnet-id s").monthly(), 2) == 32.85
    assert round(price("aws ec2 allocate-address").monthly(), 2) == 3.65
    assert round(price("aws elbv2 create-load-balancer --name x --type network").monthly(), 2) == 16.43
    assert price("aws s3 mb s3://x").hourly == 0.0


def test_backslash_line_continuations():
    cmd = ("aws rds create-db-instance \\\n  --db-instance-identifier test-db \\\n  --db-instance-class db.t3.micro \\\n"
           "  --engine postgres \\\n  --allocated-storage 20")
    ms = match_command(cmd)
    assert [(m.action, m.key) for m in ms] == [("create", "rds:create")]
    assert price_for(ms[0]).hourly is not None and ms[0].flag("--db-instance-identifier") == "test-db"


def test_newline_still_separates_commands():
    assert keys("aws s3 mb s3://a\naws ec2 allocate-address") == [("create", "s3:create"), ("create", "eip:create")]
