"""The live parity matrix, as a test -- skipped unless it is explicitly asked for.

Never collected by an ordinary ``pytest tests -q`` run: it uploads real data to
a real Telegram account and registers real rows, so it has to be opted into by
naming a folder and a byte budget, the same two things the script demands.

    $env:TD_PARITY_FOLDER = "_parity-probe"
    $env:TD_PARITY_MAX_BYTES = "1200000000"
    .venv\Scripts\python.exe -m pytest tests/live -q
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

FOLDER = os.environ.get("TD_PARITY_FOLDER", "")
MAX_BYTES = os.environ.get("TD_PARITY_MAX_BYTES", "")

pytestmark = pytest.mark.skipif(
    not (FOLDER and MAX_BYTES),
    reason="set TD_PARITY_FOLDER and TD_PARITY_MAX_BYTES to run the live matrix",
)


def test_live_transfer_parity():
    from scripts.live_transfer_parity import main

    argv = ["--folder", FOLDER, "--max-bytes", MAX_BYTES]
    cases = os.environ.get("TD_PARITY_CASES", "")
    if cases:
        argv += ["--cases", cases]
    assert main(argv) == 0, "see the parity report for the mismatches"
