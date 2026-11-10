import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIX = Path(__file__).resolve().parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


def result_of(name: str) -> str:
    """The fixture as the terminal tool's JSON result string."""
    f = fixture(name)
    return json.dumps({"output": f["output"], "exit_code": f["exit_code"], "error": None})


NOW = datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)   # 14:30 IST


@pytest.fixture
def svc(tmp_path):
    from lights_out.service import LightsOut
    return LightsOut(tmp_path / "ledger.jsonl", tz_name="Asia/Kolkata", clock=lambda: NOW)
