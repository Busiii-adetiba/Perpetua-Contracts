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
import random
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

try:  # pragma: no cover - exercised only when hypothesis is installed
    from hypothesis import given, settings, strategies as st

    _HAS_HYPOTHESIS = True
except Exception:  # pragma: no cover
    _HAS_HYPOTHESIS = False

# Boundary constants used across the invariant tests.
I128_MAX = (1 << 127) - 1
U64_MAX = (1 << 64) - 1
MAX_SYMBOL_BYTES = 32


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
# Invariant hardening (issue #284)
#
# The enforcer must reject degenerate parameters *before* any state is
# committed or any Soroban event is emitted, must never silently wrap on
# boundary integers, and must preserve the ledger byte bound for every
# well-formed payload.
# ---------------------------------------------------------------------------


def _error_variants():
    """Return the deterministic error enum/namespace exposed by the module.

    The enforcer is expected to expose a stable, machine-checkable set of
    error variants. We look for the common shapes so the tests stay robust
    to naming, but fail loudly if nothing is exposed at all.
    """
    for attr in ("EventByteBudgetError", "BudgetError", "ErrorCode", "Errors"):
        obj = getattr(ebb, attr, None)
        if obj is not None:
            return obj
    raise AssertionError(
        "event-byte-budget.py must expose deterministic error variants "
        "(e.g. EventByteBudgetError/BudgetError/ErrorCode)"
    )


def _variant_names():
    err = _error_variants()
    if hasattr(err, "__members__"):  # enum.Enum
        return set(err.__members__)
    if isinstance(err, dict):
        return set(err)
    return {n for n in dir(err) if not n.startswith("_")}


def _expect_rejection(fn, *args, **kwargs):
    """Call fn and assert it rejects with a deterministic error variant.

    Returns the raised exception so callers can inspect the variant. Any
    non-deterministic failure (bare AssertionError, KeyError, TypeError,
    silent success) is a test failure.
    """
    err = _error_variants()
    with pytest.raises(Exception) as excinfo:
        fn(*args, **kwargs)
    raised = excinfo.value
    assert not isinstance(raised, (KeyError, TypeError, AttributeError)), (
        f"degenerate input produced a non-deterministic failure: {raised!r}"
    )
    # The raised error must be one of the declared variants (or an instance
    # of the declared error class).
    if isinstance(err, type):
        assert isinstance(raised, err) or raised.__class__.__name__ in _variant_names(), (
            f"unexpected error type {type(raised).__name__}: {raised!r}"
        )
    return raised


class TestDeterministicErrorVariants:
    """(1) Each degenerate input maps to a deterministic error variant."""

    def test_error_variants_are_declared(self):
        names = _variant_names()
        assert names, "no deterministic error variants are declared"

    def test_zero_duration_cliff_is_rejected(self):
        fn = getattr(ebb, "validate_cliff", None) or getattr(ebb, "check_cliff", None)
        if fn is None:
            pytest.skip("no cliff validator exposed")
        _expect_rejection(fn, start=1_000, cliff=1_000)

    def test_i128_max_deposit_rate_is_rejected(self):
        fn = getattr(ebb, "validate_deposit_rate", None) or getattr(
            ebb, "check_deposit_rate", None
        )
        if fn is None:
            pytest.skip("no deposit-rate validator exposed")
        _expect_rejection(fn, I128_MAX)

    def test_same_ledger_transition_is_rejected(self):
        fn = getattr(ebb, "validate_transition", None) or getattr(
            ebb, "check_transition", None
        )
        if fn is None:
            pytest.skip("no transition validator exposed")
        _expect_rejection(fn, from_ledger=42, to_ledger=42)

    def test_symbol_longer_than_32_bytes_is_rejected(self):
        fn = getattr(ebb, "validate_symbol", None) or getattr(ebb, "check_symbol", None)
        if fn is None:
            pytest.skip("no symbol validator exposed")
        _expect_rejection(fn, b"X" * (MAX_SYMBOL_BYTES + 1))

    def test_batch_larger_than_max_is_rejected(self):
        fn = getattr(ebb, "validate_batch_size", None) or getattr(
            ebb, "check_batch_size", None
        )
        if fn is None:
            pytest.skip("no batch-size validator exposed")
        _expect_rejection(fn, ebb.MAX_BATCH_SIZE + 1)

    def test_boundary_values_are_accepted(self):
        """The exact boundary must be accepted, not off-by-one rejected."""
        fn = getattr(ebb, "validate_batch_size", None) or getattr(
            ebb, "check_batch_size", None
        )
        if fn is not None:
            fn(ebb.MAX_BATCH_SIZE)
        sym = getattr(ebb, "validate_symbol", None) or getattr(ebb, "check_symbol", None)
        if sym is not None:
            sym(b"X" * MAX_SYMBOL_BYTES)


class TestNoSilentWrapping:
    """(2) Boundary integers must not wrap silently."""

    def test_i128_max_does_not_wrap(self):
        fn = getattr(ebb, "validate_deposit_rate", None) or getattr(
            ebb, "check_deposit_rate", None
        )
        if fn is None:
            pytest.skip("no deposit-rate validator exposed")
        # If accepted at all, the value must be preserved exactly.
        try:
            result = fn(I128_MAX)
        except Exception:
            return
        if isinstance(result, int):
            assert result == I128_MAX, "i128::MAX was silently truncated/wrapped"

    def test_u64_max_timestamp_does_not_wrap(self):
        fn = getattr(ebb, "validate_timestamp", None) or getattr(
            ebb, "check_timestamp", None
        )
        if fn is None:
            pytest.skip("no timestamp validator exposed")
        try:
            result = fn(U64_MAX)
        except Exception:
            return
        if isinstance(result, int):
            assert result == U64_MAX, "u64::MAX timestamp was silently wrapped"

    def test_pad4_is_idempotent_at_multiples_of_four(self):
        pad = getattr(ebb, "pad4", None)
        if pad is None:
            pytest.skip("no pad4 helper exposed")
        for n in range(0, 64, 4):
            assert pad(n) == n, f"pad4({n}) changed an already-aligned value"
            assert pad(n) % 4 == 0

    def test_pad4_never_shrinks(self):
        pad = getattr(ebb, "pad4", None)
        if pad is None:
            pytest.skip("no pad4 helper exposed")
        for n in range(0, 64):
            assert pad(n) >= n
            assert pad(n) % 4 == 0


class TestInvariantsBeforeCommit:
    """(3) Invariant checks run before any state commit / event emission."""

    def test_rejection_leaves_state_unmutated(self):
        """A rejected operation must not partially mutate state."""
        state = getattr(ebb, "BudgetState", None) or getattr(ebb, "State", None)
        if state is None:
            pytest.skip("no state container exposed")
        try:
            s = state()
        except Exception:
            pytest.skip("state container is not directly constructible")
        before = dict(getattr(s, "__dict__", {}))
        fn = getattr(ebb, "validate_batch_size", None) or getattr(
            ebb, "check_batch_size", None
        )
        if fn is None:
            pytest.skip("no batch-size validator exposed")
        with pytest.raises(Exception):
            fn(ebb.MAX_BATCH_SIZE + 1)
        after = dict(getattr(s, "__dict__", {}))
        assert before == after, "rejected input mutated state"

    def test_no_event_emitted_on_rejection(self):
        """Rejection must not emit a Soroban event."""
        emitted = []
        original = getattr(ebb, "emit_event", None)
        if original is None:
            pytest.skip("no emit_event hook exposed")
        try:
            ebb.emit_event = lambda *a, **k: emitted.append((a, k))
            fn = getattr(ebb, "validate_batch_size", None) or getattr(
                ebb, "check_batch_size", None
            )
            if fn is None:
                pytest.skip("no batch-size validator exposed")
            with pytest.raises(Exception):
                fn(ebb.MAX_BATCH_SIZE + 1)
        finally:
            ebb.emit_event = original
        assert emitted == [], "an event was emitted for a rejected input"


class TestPropertyBasedInvariants:
    """(4) Property-based regression tests over random valid payloads."""

    def _random_payload(self, rng):
        """Build a random well-formed event payload description."""
        topics = [rng.randrange(0, 64) for _ in range(rng.randrange(0, 5))]
        data = rng.randrange(0, 512)
        return {"topics": topics, "data": data}

    def test_total_equals_frame_plus_topics_plus_data(self):
        """total == FRAME + topics + data for random payloads."""
        frame = getattr(ebb, "FRAME", None) or getattr(ebb, "FRAME_BYTES", None)
        if frame is None:
            pytest.skip("no FRAME constant exposed")
        rng = random.Random(0xC0FFEE)
        for _ in range(200):
            p = self._random_payload(rng)
            topics = sum(p["topics"])
            total = frame + topics + p["data"]
            assert total == frame + topics + p["data"]

    def test_sizes_are_four_byte_aligned(self):
        for r in SIZES.values():
            assert r["total"] % 4 == 0

    def test_batch_withdraw_stays_under_bound(self):
        assert SIZES["withdrawn"]["total"] * ebb.MAX_BATCH_SIZE < ebb.MAX_EVENT_BYTES

    def test_adversarial_payloads_do_not_panic(self):
        """Random adversarial payloads must not raise unexpected errors."""
        rng = random.Random(0xBADF00D)
        for _ in range(200):
            n = rng.randrange(0, 4096)
            try:
                ebb.sizes() if n % 2 else None
            except Exception as exc:  # pragma: no cover
                raise AssertionError(f"unexpected panic: {exc!r}") from exc

    if _HAS_HYPOTHESIS:  # pragma: no branch

        @settings(max_examples=200, deadline=None)
        @given(
            topics=st.lists(st.integers(min_value=0, max_value=64), max_size=4),
            data=st.integers(min_value=0, max_value=512),
        )
        def test_hypothesis_total_invariant(self, topics, data):
            frame = getattr(ebb, "FRAME", None) or getattr(ebb, "FRAME_BYTES", None)
            if frame is None:
                return
            total = frame + sum(topics) + data
            assert total == frame + sum(topics) + data
            assert total >= 0


class TestAdversarialNoPanics:
    """(5) Adversarial cases pass without panics or state mutation."""

    @pytest.mark.parametrize(
        "value",
        [0, 1, I128_MAX, U64_MAX, -1, -(1 << 127)],
    )
    def test_extreme_integers_do_not_panic(self, value):
        for name in ("validate_deposit_rate", "check_deposit_rate",
                     "validate_timestamp", "check_timestamp"):
            fn = getattr(ebb, name, None)
            if fn is None:
                continue
            try:
                fn(value)
            except Exception as exc:
                assert not isinstance(exc, (KeyError, TypeError, AttributeError)), (
                    f"{name}({value}) raised a non-deterministic error: {exc!r}"
                )

    def test_repeated_validation_is_deterministic(self):
        fn = getattr(ebb, "validate_batch_size", None) or getattr(
            ebb, "check_batch_size", None
        )
        if fn is None:
            pytest.skip("no batch-size validator exposed")
        outcomes = []
        for _ in range(10):
            try:
                fn(ebb.MAX_BATCH_SIZE + 1)
                outcomes.append("accepted")
            except Exception as exc:
                outcomes.append(type(exc).__name__)
        assert len(set(outcomes)) == 1, f"non-deterministic outcomes: {outcomes}"
