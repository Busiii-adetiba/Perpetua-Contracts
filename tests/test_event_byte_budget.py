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
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


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


class TestScriptRuns:
    def test_budget_script_exits_cleanly(self):
        result = subprocess.run(
            [sys.executable, str(_ROOT / "script" / "event-byte-budget.py")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "withdrawn" in result.stdout


# ---------------------------------------------------------------------------
# Issue #284 - invariant and boundary hardening.
# ---------------------------------------------------------------------------


class TestBoundaryInvariants:
    """Degenerate parameters must be rejected early, with a deterministic
    error variant, and never silently wrap or truncate."""

    def test_sym_rejects_over_32_byte_symbols(self):
        with pytest.raises(ebb.ByteBudgetError):
            ebb.sym("a" * 33)

    def test_sym_accepts_exactly_32_bytes(self):
        assert ebb.sym("a" * 32) == "a" * 32

    def test_sym_rejects_empty_symbol(self):
        with pytest.raises(ebb.ByteBudgetError):
            ebb.sym("")

    def test_pad4_rejects_negative_lengths(self):
        with pytest.raises(ebb.ByteBudgetError):
            ebb.pad4(-1)

    def test_pad4_is_idempotent_on_aligned_lengths(self):
        for n in (0, 4, 8, 12, 260):
            assert ebb.pad4(n) == n

    def test_pad4_rounds_up_to_the_next_multiple_of_four(self):
        assert ebb.pad4(1) == 4
        assert ebb.pad4(5) == 8
        assert ebb.pad4(259) == 260

    def test_empty_events_are_rejected(self):
        with pytest.raises(ebb.ByteBudgetError):
            ebb.total_bytes([])

    def test_zero_or_negative_sizes_are_rejected(self):
        with pytest.raises(ebb.ByteBudgetError):
            ebb.total_bytes([{"name": "x", "total": 0}])
        with pytest.raises(ebb.ByteBudgetError):
            ebb.total_bytes([{"name": "x", "total": -4}])

    def test_i128_max_scale_amounts_do_not_wrap(self):
        """An i128::MAX-scale amount must not silently wrap to a small or
        negative byte count. The model must either account for the full
        width or reject the value outright."""
        i128_max = (1 << 127) - 1
        try:
            n = ebb.amount_bytes(i128_max)
        except ebb.ByteBudgetError:
            return
        assert n > 0
        assert n >= 16, "i128 amounts need at least 16 bytes of XDR"

    def test_amount_bytes_rejects_negative_amounts(self):
        with pytest.raises(ebb.ByteBudgetError):
            ebb.amount_bytes(-1)

    def test_batch_size_boundary_is_enforced(self):
        with pytest.raises(ebb.ByteBudgetError):
            ebb.check_batch_size(ebb.MAX_BATCH_SIZE + 1)

    def test_batch_size_at_the_ceiling_is_accepted(self):
        ebb.check_batch_size(ebb.MAX_BATCH_SIZE)

    def test_batch_size_zero_is_rejected(self):
        with pytest.raises(ebb.ByteBudgetError):
            ebb.check_batch_size(0)

    def test_byte_budget_error_is_a_deterministic_variant(self):
        assert issubclass(ebb.ByteBudgetError, Exception)
        # Same input -> same error type and message, every time.
        msgs = set()
        for _ in range(3):
            with pytest.raises(ebb.ByteBudgetError) as exc:
                ebb.sym("a" * 33)
            msgs.add(str(exc.value))
        assert len(msgs) == 1


# ---------------------------------------------------------------------------
# Issue #285 - CPU and memory budget.
# ---------------------------------------------------------------------------


class TestCpuAndMemoryBudget:
    def test_sizes_table_is_cached(self):
        """sizes() must not rebuild the table on every call: the hot path
        is invoked once per batch and must stay within the CPU budget."""
        first = ebb.sizes()
        second = ebb.sizes()
        assert first is second, "sizes() must return the cached table object"

    def test_sizes_calls_are_bounded(self):
        """A full batch must not call sym() more than a fixed, small number
        of times per event."""
        calls = {"n": 0}
        original = ebb.sym

        def counting_sym(*args, **kwargs):
            calls["n"] += 1
            return original(*args, **kwargs)

        ebb.sym = counting_sym
        try:
            ebb.sizes()
            ebb.total_bytes([{"name": "withdrawn", "total": SIZES["withdrawn"]["total"]}])
        finally:
            ebb.sym = original
        assert calls["n"] <= 64, f"sym() called {calls['n']} times"

    def test_wasm_size_budget_script_stays_green(self, tmp_path):
        """script/check-stream-wasm-size.sh must pass with a stub artifact
        so the size budget cannot regress unnoticed."""
        script = _ROOT / "script" / "check-stream-wasm-size.sh"
        if not script.exists():
            pytest.skip("check-stream-wasm-size.sh not present")
        stub = tmp_path / "stub.wasm"
        stub.write_bytes(b"\x00asm\x01\x00\x00\x00")
        env = dict(os.environ)
        env["BUILD_COMMAND_OVERRIDE"] = "true"
        env["WASM_ARTIFACT"] = str(stub)
        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(_ROOT),
        )
        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Issue #286 - event schema and indexer parity.
# ---------------------------------------------------------------------------


class TestSchemaParity:
    def test_schema_fingerprint_is_stable(self):
        first = ebb.schema_fingerprint()
        second = ebb.schema_fingerprint()
        assert first == second
        assert isinstance(first, str) and first

    def test_event_schema_matches_abi_doc(self):
        abi = _ROOT / "docs" / "ABI.md"
        if not abi.exists():
            pytest.skip("docs/ABI.md not present")
        text = abi.read_text(encoding="utf-8")
        for name in SIZES:
            assert name in text, f"{name} missing from docs/ABI.md event table"

    def test_golden_snapshots_decode_against_frozen_schema(self):
        fixtures = _ROOT / "tests" / "fixtures" / "event_snapshots"
        if not fixtures.exists():
            pytest.skip("no event_snapshots fixtures")
        files = sorted(fixtures.glob("*.json"))
        assert files, "event_snapshots directory is empty"
        for path in files:
            payload = json.loads(path.read_text(encoding="utf-8"))
            assert "event" in payload, f"{path.name} missing 'event'"
            assert payload["event"] in SIZES, f"{path.name} unknown event"
            assert "topics" in payload and "data" in payload
