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
import subprocess
import sys
from pathlib import Path

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


class TestHotPathBudget:
    """Issue #285: the enforcer is invoked once per batch payroll run, so its
    hot path must stay within a strict CPU-instruction / memory-byte cap.

    `sizes()` is the only non-trivial work the enforcer does: it builds the
    XDR size table. It must be memoised, so N calls cost one computation and
    zero extra allocations. These tests pin that contract so a refactor that
    drops the cache (or invalidates it per call) fails loudly instead of
    silently multiplying the instruction count by the batch frequency.
    """

    #: Upper bound on how many times the size table may be recomputed across
    #: the whole benchmark loop. One is the memoised steady state; anything
    #: above this means per-call recomputation crept back in.
    MAX_SIZE_COMPUTATIONS = 1

    #: Iterations of the hot path exercised per benchmark. Chosen to be far
    #: larger than any realistic batch count so a per-call regression cannot
    #: hide behind a small sample.
    ITERATIONS = 1000

    def test_sizes_is_memoised_across_repeated_calls(self):
        """1000 hot-path calls at MAX_BATCH_SIZE must not recompute sizes."""
        calls = 0
        original = ebb._compute_sizes

        def counting_compute():
            nonlocal calls
            calls += 1
            return original()

        ebb._compute_sizes = counting_compute
        try:
            ebb.sizes.cache_clear()
            for _ in range(self.ITERATIONS):
                table = ebb.sizes()
                # Touch the hot fields the enforcer reads per event.
                for row in table:
                    row["total"] * ebb.MAX_BATCH_SIZE
        finally:
            ebb._compute_sizes = original
            ebb.sizes.cache_clear()

        assert calls <= self.MAX_SIZE_COMPUTATIONS, (
            f"sizes() recomputed {calls} times over {self.ITERATIONS} calls; "
            "the hot path must stay memoised"
        )

    def test_sizes_returns_the_identical_cached_object(self):
        """Identity, not equality: a fresh-but-equal table is still a
        regression, because it re-allocates the whole XDR size table."""
        first = ebb.sizes()
        second = ebb.sizes()
        assert first is second

    def test_batch_ceiling_is_pinned_exactly(self):
        """MAX_BATCH_SIZE * withdrawn is the worst-case event byte footprint
        of a full batch_withdraw. Pin the exact number so any encoding drift
        fails loudly rather than sliding under the 16,384 bound."""
        assert ebb.MAX_BATCH_SIZE * SIZES["withdrawn"]["total"] == 4_160

    def test_hot_path_stays_under_the_ledger_bound(self):
        """The benchmarked worst case must remain a strict fraction of the
        Protocol 27 ledger bound."""
        worst_case = ebb.MAX_BATCH_SIZE * SIZES["withdrawn"]["total"]
        assert worst_case < ebb.MAX_EVENT_BYTES
        assert worst_case / ebb.MAX_EVENT_BYTES < 0.5


class TestScriptRuns:
    def test_budget_script_exits_cleanly(self):
        result = subprocess.run(
            [sys.executable, str(_ROOT / "script" / "event-byte-budget.py")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "withdrawn" in result.stdout
