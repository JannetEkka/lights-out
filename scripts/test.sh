#!/usr/bin/env bash
# Unit tests (no network, no Hermes needed): bash scripts/test.sh [-k pattern]
cd "$(dirname "$0")/.." && exec "${PYTHON:-python3}" -m pytest -q -c tests/pytest.ini --rootdir tests tests "$@"
