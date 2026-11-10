"""Read YAML with whatever safe loader is installed: ruamel.yaml (ships with Hermes) or PyYAML."""
from __future__ import annotations

from typing import Any


def load_yaml(text: str) -> Any:
    try:
        from ruamel.yaml import YAML
    except ImportError:  # outside Hermes (unit tests): PyYAML
        import yaml

        return yaml.safe_load(text)
    return YAML(typ="safe", pure=True).load(text)
