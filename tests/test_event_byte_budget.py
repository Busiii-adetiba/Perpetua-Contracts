"""tests/test_event_byte_budget.py

Tests for script/event-byte-budget.py and script/verify_event_bytes.py.

The contract that matters is the Protocol 27 ledger bound: the SUM of all
contract events in a transaction must stay under
maxSorobanTransactionEventSizeBytes (16,384). A full `batch_withdraw` at
MAX_BATCH_SIZE is the worst realistic case, because it emits one `withdrawn`
event per stream drawn from.

These tests pin the per-event sizes, the batch ceiling, and the independent
hand-calculation cross-check. They also pin the FACT that `withdrawn` is
260 bytes, i.e. that it does not meet issue #57's "< 256" target - changing
that silently would be exactly the kind of drift these tests exist to catch.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

_SNAPSHOT_DIR = _ROOT / "tests" / "fixtures" / "event_snapshots"


def _load(name: str):
    path = _ROOT / "script" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ebb = _load("event-byte-budget")

SIZES = {r["name"]: r for r in ebb.sizes()}


class TestPerEventSizes:
    def test_every_event_is_measured(self):
        assert set(SIZES) == {
            "stream_created", "withdrawn", "cancelled", "paused", "resumed",
            "topped_up", "recipient_transferred", "delegate_granted",
            "delegate_revoked", "ttl_extended",
        }

    def test_withdrawn_size_is_pinned(self):
        assert SIZES["withdrawn"]["total"] == 260

    def test_withdrawn_exceeds_the_256_target(self):
        """Issue #57 asks for < 256. The real figure is 260. Do not paper
        over this: if this assertion starts failing, the encoding changed
        and the documentation must be revisited."""
        assert SIZES["withdrawn"]["total"] > 256

    def test_empty_payload_events_are_smallest(self):
        """recipient_transferred / delegate_revoked carry only topics."""
        for name in ("recipient_transferred", "delegate_revoked"):
            assert SIZES[name]["data"] == 12, f"{name} should have an empty map"

    def test_largest_event_is_stream_created(self):
        largest = max(SIZES.values(), key=lambda r: r["total"])
        assert largest["name"] == "stream_created"

    def test_all_sizes_are_positive_and_aligned(self):
        for r in SIZES.values():
            assert r["total"] > 0
            # XDR pads every body to a multiple of 4.
            assert r["total"] % 4 == 0, f"{r['name']} is not 4-byte aligned"


class TestBudgetCeiling:
    def test_full_batch_withdraw_fits_the_ledger_bound(self):
        """16 x withdrawn must leave substantial headroom under 16,384."""
        used = SIZES["withdrawn"]["total"] * ebb.MAX_BATCH_SIZE
        assert used < ebb.MAX_EVENT_BYTES
        assert ebb.MAX_EVENT_BYTES - used > 10_000

    def test_batch_is_a_fraction_of_the_budget(self):
        used = SIZES["withdrawn"]["total"] * ebb.MAX_BATCH_SIZE
        assert used / ebb.MAX_EVENT_BYTES < 0.5

    def test_no_single_event_comes_close_to_the_bound(self):
        assert max(r["total"] for r in SIZES.values()) < ebb.MAX_EVENT_BYTES

    def test_ledger_bound_matches_protocol_27(self):
        assert ebb.MAX_EVENT_BYTES == 16_384
        assert ebb.MAX_BATCH_SIZE == 16


class TestIndependentCrossCheck:
    def test_hand_calculation_agrees_with_the_model(self):
        """verify_event_bytes.py recomputes `withdrawn` from the XDR
        definitions without importing the model. Run it as a subprocess so
        the independence is real."""
        result = subprocess.run(
            [sys.executable, str(_ROOT / "script" / "verify_event_bytes.py")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "MATCHES evsize.py (260)" in result.stdout

    def test_cross_check_covers_every_event(self):
        """The independent verifier must exercise all code paths in
        event-byte-budget.py, not just `withdrawn`."""
        result = subprocess.run(
            [sys.executable, str(_ROOT / "script" / "verify_event_bytes.py")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        for name in SIZES:
            assert name in result.stdout, f"verify_event_bytes.py skipped {name}"


class TestGoldenSnapshots:
    def test_snapshot_fixtures_exist(self):
        assert _SNAPSHOT_DIR.is_dir(), "missing tests/fixtures/event_snapshots/"
        files = sorted(_SNAPSHOT_DIR.glob("*.json"))
        assert files, "no golden event snapshot fixtures found"

    def test_snapshots_match_event_definitions(self):
        """Every golden snapshot must describe an event that
        event-byte-budget.py actually models, with matching topic/payload
        fields. This is the schema-drift tripwire."""
        for path in sorted(_SNAPSHOT_DIR.glob("*.json")):
            snap = json.loads(path.read_text())
            name = snap["event"]
            assert name in SIZES, f"{path.name}: unknown event {name!r}"
            assert snap["total"] == SIZES[name]["total"], (
                f"{path.name}: total drifted from the model"
            )
            assert snap["data"] == SIZES[name]["data"], (
                f"{path.name}: data payload size drifted from the model"
            )
            assert snap["topics"] == SIZES[name]["topics"], (
                f"{path.name}: topic layout drifted from the model"
            )
            assert snap["topics"] > 0
            assert snap["data"] >= 0

    def test_every_event_has_a_snapshot(self):
        snapshotted = {
            json.loads(p.read_text())["event"]
            for p in _SNAPSHOT_DIR.glob("*.json")
        }
        assert snapshotted == set(SIZES), (
            "golden snapshots must cover every modelled event"
        )


class TestScriptRuns:
    def test_budget_script_exits_cleanly(self):
        result = subprocess.run(
            [sys.executable, str(_ROOT / "script" / "event-byte-budget.py")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "withdrawn" in result.stdout
