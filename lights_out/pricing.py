"""Hourly price for a matched create, from the dated price table. Unknown = None, never a guess."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .matcher import Match
from .yamlish import load_yaml

PRICES_DIR = Path(__file__).resolve().parent.parent / "prices"


@dataclass
class Price:
    hourly: Optional[float]   # per resource; None = price unknown
    size: str                 # what the receipt shows next to the kind: "t3.large", "100 GiB gp3", ...
    count: int = 1
    note: str = ""            # "+ storage", "+ LCU usage", "price unknown: ...", ...

    @property
    def known(self) -> bool:
        return self.hourly is not None

    def monthly(self, hours_per_month: float = 730.0) -> Optional[float]:
        return None if self.hourly is None else self.hourly * hours_per_month


@lru_cache(maxsize=4)
def load_prices(path: str | None = None) -> dict:
    p = Path(path) if path else PRICES_DIR / "aws-us-east-1.yaml"
    data = load_yaml(p.read_text(encoding="utf-8"))
    for key in ("read", "region", "hours_per_month", "ec2_hourly", "rds_hourly"):
        if key not in data:
            raise ValueError(f"price table {p} is missing {key}")
    return data


def _count(m: Match, flags: list[str]) -> int:
    for f in flags:
        v = m.flag(f)
        if v:
            mt = re.match(r"^(\d+)", v)   # "--count 2" or "--count 1:3" (min)
            if mt:
                return max(1, int(mt.group(1)))
    return 1


def price_for(m: Match, table: dict | None = None) -> Price:
    t = table or load_prices()
    spec = m.rule.get("price", {}) or {}
    model = spec.get("model")
    region = m.region or ""
    wrong_region = bool(region) and region != t["region"]

    def unknown(size: str, why: str, count: int = 1) -> Price:
        return Price(None, size, count, f"price unknown: {why}")

    if model == "ec2_instance":
        size = m.flag(spec["size_flag"]) or ""
        count = _count(m, spec.get("count_flags", []))
        if not size:
            return unknown("type not given", "no --instance-type (launch template or AWS default)", count)
        if wrong_region:
            return unknown(size, f"table covers {t['region']} only, command uses {region}", count)
        hourly = (t.get("ec2_hourly") or {}).get(size)
        return Price(float(hourly), size, count) if hourly is not None else unknown(size, f"{size} not in table", count)

    if model == "ebs_volume":
        raw = m.flag(spec["size_flag"]) or ""
        vtype = m.flag(spec.get("type_flag", "")) or spec.get("default_type", "gp2")
        if not raw.isdigit():
            return unknown(f"? GiB {vtype}", "no --size (snapshot size)")
        size = f"{raw} GiB {vtype}"
        if wrong_region:
            return unknown(size, f"table covers {t['region']} only")
        per_gb = (t.get("ebs_gb_month") or {}).get(vtype)
        if per_gb is None:
            return unknown(size, f"{vtype} not in table")
        return Price(int(raw) * float(per_gb) / float(t["hours_per_month"]), size, 1,
                     "+ IOPS/throughput above baseline" if vtype in ("gp3", "io1", "io2") else "")

    if model == "public_ipv4":
        if wrong_region:
            return unknown("public IPv4", f"table covers {t['region']} only")
        return Price(float(t["public_ipv4_hourly"]), "public IPv4")

    if model == "nat_gateway":
        if wrong_region:
            return unknown("NAT gateway", f"table covers {t['region']} only")
        return Price(float(t["nat_gateway_hourly"]), "NAT gateway", 1, "+ $0.045/GB processed")

    if model == "elbv2":
        lbtype = m.flag(spec.get("type_flag", "")) or spec.get("default_type", "application")
        if wrong_region:
            return unknown(lbtype, f"table covers {t['region']} only")
        hourly = (t.get("elbv2_hourly") or {}).get(lbtype)
        if hourly is None:
            return unknown(lbtype, f"{lbtype} load balancer not in table")
        return Price(float(hourly), lbtype, 1, "+ LCU usage")

    if model == "rds_instance":
        cls = m.flag(spec["size_flag"]) or ""
        engine = (m.flag(spec["engine_flag"]) or "").lower()
        engine = {"postgresql": "postgres", "aurora-postgresql": "", "aurora-mysql": ""}.get(engine, engine)
        size = f"{cls or '?'} {engine or '?'}".strip()
        if wrong_region:
            return unknown(size, f"table covers {t['region']} only")
        hourly = ((t.get("rds_hourly") or {}).get(engine) or {}).get(cls)
        if hourly is None:
            return unknown(size, f"{engine or 'engine'} {cls or 'class'} not in table")
        storage = m.flag(spec.get("storage_flag", ""))
        stype = (m.flag(spec.get("storage_type_flag", "")) or "gp2").lower()
        note = ""
        total = float(hourly)
        if storage and storage.isdigit() and stype in ("gp2", "gp3"):
            total += int(storage) * float(t["rds_storage_gb_month"]) / float(t["hours_per_month"])
            size += f" {storage} GiB"
        elif storage:
            note = f"+ {stype} storage"
        return Price(total, size, 1, note)

    if model == "s3_bucket":
        return Price(0.0, "bucket", 1, "+ storage")

    return unknown(m.kind, f"no price model {model!r}")
