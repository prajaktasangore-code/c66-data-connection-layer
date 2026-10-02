"""CI entry point: one pytest case per YAML test, using the same logic as run_connector_tests.py.

    $env:CONNECTOR_TESTS_ENV_FILE = ".env"
    py -m pytest connector_tests\\test_connectors_pytest.py -v

CONNECTOR_TESTS_CONFIG selects a different YAML file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_connector_tests as harness  # noqa: E402

SUITE = harness.load_suite(
    os.environ.get("CONNECTOR_TESTS_CONFIG", harness.DEFAULT_CONFIG),
    os.environ.get("CONNECTOR_TESTS_ENV_FILE"),
)
TESTS = [t for t in SUITE.tests if isinstance(t, dict)]


@pytest.mark.parametrize("test", TESTS, ids=[str(t.get("name")) for t in TESTS])
def test_connector(test: dict) -> None:
    result = harness.run_test(test, SUITE)
    if result.status == harness.SKIP:
        pytest.skip(result.reason)
    assert result.status == harness.PASS, f"[{result.category}] {result.reason}"
