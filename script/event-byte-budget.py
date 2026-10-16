"""Byte accounting for the stream contract's Soroban events.

Protocol 27 caps the SUM of all contract events in a transaction at
`maxSorobanTransactionEventSizeBytes` = 16,384. This computes each event's
on-wire size so that cap can be checked per event and per batch.

Encoding model, taken from `Stellar-contract.x` (see the `SCVal` union):

  ContractEvent = ExtentionPoint ext    4 bytes (discriminant, always 0)
                + ContractID contractID  an SCVal::Address
                + SCVec  topics          SCV_VEC: disc(4) + ptr(4) + count(4)
                + SCVal  data            normally an SCV_MAP

  SCV_U64     -> uint64                       4 + 8
  SCV_U32     -> uint32                       4 + 4
  SCV_I128    -> Int128Parts{int64,uint64}    4 + 16
  SCV_BOOL    -> bool                         4 + 4
  SCV_SYMBOL  -> SCSymbol, `string<32>`       4 + 4 + pad4(len)
  SCV_ADDRESS -> SCAddress::Account(Hash[32]) 4 + 4 + 32
  SCV_MAP     -> SCMap*                       4 + 4 + 4 + entries

Every body is XDR-padded to a multiple of 4 bytes.

SCV_VEC and SCV_MAP are declared as pointers, so they carry a 4-byte pointer
in addition to their discriminant. That is accounted for above.

## Scope and honesty about the numbers

This is an ANALYTICAL model of the published encoding. It is not a measurement
from a running host, and it does not include any per-event accounting the host
adds outside the `ContractEvent` struct itself. Two consequences:

  * treat the figures as the size of the encoded event, and confirm them
    against a host run before using them for a release decision;
  * `script/verify_event_bytes.py` recomputes the `withdrawn` figure by hand,
    from the XDR definitions, without importing anything from this file. It
    exists so the number is not resting on one implementation.

`withdrawn` comes out at 260 bytes, which is 4 bytes OVER the "< 256 bytes"
target in issue #57. See docs/ABI.md for why that target is not a real
constraint and why no optimisation is warranted.
"""

# --- Deterministic error variants ---------------------------------------
#
# Every rejection path raises one of these instead of a bare `assert`, so
# callers (and CI) can branch on a stable code rather than a message string.
# All checks run *before* any state commit or event emission.


class EventByteBudgetError(Exception):
    """Deterministic, code-tagged rejection of a degenerate event/parameter."""

    ZERO_DURATION = "ZERO_DURATION"
    I128_MAX_RATE = "I128_MAX_RATE"
    SAME_LEDGER_TRANSITION = "SAME_LEDGER_TRANSITION"
    OVERFLOW = "OVERFLOW"
    SYMBOL_TOO_LONG = "SYMBOL_TOO_LONG"
    BATCH_TOO_LARGE = "BATCH_TOO_LARGE"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"

    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


# Largest value an i128 can hold; a deposit rate equal to this is degenerate.
I128_MAX = 2 ** 127 - 1


def _require_non_negative(value, what):
    """Guard every integer that feeds a size computation against wrapping."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise EventByteBudgetError(
            EventByteBudgetError.INVALID_ARGUMENT,
            f"{what} must be an int, got {type(value).__name__}",
        )
    if value < 0:
        raise EventByteBudgetError(
            EventByteBudgetError.OVERFLOW,
            f"{what} must be non-negative, got {value}",
        )
    return value


def _checked_add(a, b, what):
    """Add two non-negative ints, rejecting anything that would wrap."""
    _require_non_negative(a, what)
    _require_non_negative(b, what)
    total = a + b
    if total < a or total < b:  # cannot happen for Python ints, kept explicit
        raise EventByteBudgetError(
            EventByteBudgetError.OVERFLOW, f"{what} overflowed: {a} + {b}"
        )
    return total


# --- ScVal body sizes (discriminant included) ---------------------------


def pad4(n):
    _require_non_negative(n, "pad4 input")
    return n + ((4 - n % 4) % 4)


def u32(_v):                    # SCV_U32 -> uint32
    return 4 + 4


def u64(_v):                    # SCV_U64 -> uint64
    return 4 + 8


def i128(_v):                   # SCV_I128 -> Int128Parts { int64 hi; uint64 lo; }
    return 4 + 16


def boolean(_v):                # SCV_BOOL -> bool, padded to 4
    return 4 + 4


def sym(name):                  # SCV_SYMBOL -> SCSymbol, a string<32>
    if not isinstance(name, str):
        raise EventByteBudgetError(
            EventByteBudgetError.INVALID_ARGUMENT,
            f"symbol name must be a str, got {type(name).__name__}",
        )
    b = name.encode("utf-8")
    if len(b) > 32:
        raise EventByteBudgetError(
            EventByteBudgetError.SYMBOL_TOO_LONG,
            f"symbol exceeds SCSYMBOL_LIMIT=32: {name!r} ({len(b)} bytes)",
        )
    return 4 + 4 + pad4(len(b))


def address(contract=False):    # SCV_ADDRESS -> SCAddress::Account(Hash[32])
    # discriminant(4) + SCAddressType(4) + 32-byte hash
    return 4 + 4 + 32 + (4 if contract else 0)


def option_u64(_v):             # Option<u64> encodes as a Vec of 0 or 1 elements
    # SCV_VEC discriminant(4) + pointer(4) + count(4) + one u64
    return 4 + 4 + 4 + u64(0)


def smap(pairs):                # SCV_MAP: disc(4) + ptr(4) + count(4) + entries
    total = 4 + 4 + 4
    for k, v in pairs:
        total = _checked_add(total, pad4(k), "smap entry key")
        total = _checked_add(total, pad4(v), "smap entry value")
    return total


def topics(elements):           # SCVec = SCV_VEC: disc(4) + ptr(4) + count(4)
    total = 4 + 4 + 4
    for e in elements:
        total = _checked_add(total, e, "topics element")
    return total


# --- The events, as declared in contracts/stream/src/events.rs ----------
#
# Sizes are computed for a mid-life stream: u64 timestamps that have passed
# a few years (~1.8e9, so 4 bytes), stream ids that fit in 8 bytes, and
# non-zero i128 amounts (always 20 bytes regardless of magnitude).

TS = 1_800_000_000     # a realistic current unix timestamp
ID = 1_000             # a mid-range stream id
AMOUNT = 1_000_000_000_000   # 1e12, a large but ordinary token amount


def _ev(name, topic_elems, pairs):
    return {
        "name": name,
        "topics": topics(topic_elems),
        "data": smap(pairs),
    }


def _t_stream_id():
    return u64(ID)


def _t_addr():
    return address()


EVENTS = [
    _ev("stream_created",
        [_t_stream_id(), _t_addr(), _t_addr()],
        [(sym("token"), address()),
         (sym("deposited"), i128(AMOUNT)),
         (sym("start_time"), u64(TS)),
         (sym("end_time"), u64(TS)),
         (sym("cliff_time"), u64(TS)),
         (sym("cancellable"), boolean(True)),
         (sym("pausable"), boolean(True)),
         (sym("transferable"), boolean(True))]),

    _ev("withdrawn",
        [_t_stream_id(), address()],
        [(sym("amount"), i128(AMOUNT)),
         (sym("withdrawn"), i128(AMOUNT)),
         (sym("deposited"), i128(AMOUNT)),
         (sym("status"), u32(0))]),

    _ev("cancelled",
        [_t_stream_id(), _t_addr(), _t_addr()],
        [(sym("refunded"), i128(AMOUNT)),
         (sym("vested"), i128(AMOUNT)),
         (sym("withdrawn"), i128(AMOUNT)),
         (sym("end_time"), u64(TS))]),

    _ev("paused",
        [_t_stream_id(), _t_addr()],
        [(sym("paused_at"), u64(TS)), (sym("paused_total"), u64(TS))]),

    _ev("resumed",
        [_t_stream_id(), _t_addr()],
        [(sym("paused_duration"), u64(TS)), (sym("paused_total"), u64(TS))]),

    _ev("topped_up",
        [_t_stream_id(), _t_addr()],
        [(sym("amount"), i128(AMOUNT)),
         (sym("deposited"), i128(AMOUNT)),
         (sym("end_time"), u64(TS))]),

    _ev("recipient_transferred",
        [_t_stream_id(), _t_addr(), _t_addr()], []),

    _ev("delegate_granted",
        [_t_stream_id(), _t_addr(), _t_addr()],
        [(sym("ops"), u32(0)),
         (sym("expires_at"), option_u64(TS))]),

    _ev("delegate_revoked",
        [_t_stream_id(), _t_addr(), _t_addr()], []),

    _ev("ttl_extended",
        [_t_stream_id()],
        [(sym("extended_to_ledgers"), u32(0))]),
]

# ContractEvent framing, per the Soroban spec:
#   ExtentionPoint ext  -> 4 bytes (discriminant, always 0)
#   ContractID contractID -> a ScVal::Address, i.e. 40 bytes
FRAME = 4 + 40     # 44
MAX_EVENT_BYTES = 16_384   # Protocol 27
MAX_BATCH_SIZE = 16


def sizes():
    rows = []
    for e in EVENTS:
        total = _checked_add(FRAME, e["topics"], "event topics")
        total = _checked_add(total, e["data"], "event data")
        if total > MAX_EVENT_BYTES:
            raise EventByteBudgetError(
                EventByteBudgetError.OVERFLOW,
                f"event {e['name']!r} is {total} bytes, over the "
                f"{MAX_EVENT_BYTES}-byte cap",
            )
        rows.append({"name": e["name"],
                     "topics": e["topics"],
                     "data": e["data"],
                     "total": total})
    return rows


# --- Invariant enforcement before state commit / event emission ---------


def validate_event(name, start_time=None, end_time=None, cliff_time=None,
                   amount=None):
    """Reject degenerate parameters before an event is emitted.

    Guards the boundary conditions called out in issue #284:
      * zero-duration cliffs (start_time == end_time or cliff_time == end_time)
      * i128::MAX deposit rates (amount == 2**127 - 1)
      * same-ledger state transitions (identical start/end/cliff timestamps)
    """
    if amount is not None:
        _require_non_negative(amount, "amount")
        if amount == I128_MAX:
            raise EventByteBudgetError(
                EventByteBudgetError.I128_MAX_RATE,
                f"deposit rate equals i128::MAX ({I128_MAX})",
            )

    times = [t for t in (start_time, end_time, cliff_time) if t is not None]
    for t in times:
        _require_non_negative(t, "timestamp")

    if start_time is not None and end_time is not None:
        if start_time == end_time:
            raise EventByteBudgetError(
                EventByteBudgetError.ZERO_DURATION,
                f"zero-duration stream: start_time == end_time == {start_time}",
            )
        if start_time > end_time:
            raise EventByteBudgetError(
                EventByteBudgetError.ZERO_DURATION,
                f"start_time {start_time} is after end_time {end_time}",
            )
    if cliff_time is not None and end_time is not None:
        if cliff_time == end_time:
            raise EventByteBudgetError(
                EventByteBudgetError.ZERO_DURATION,
                f"zero-duration cliff: cliff_time == end_time == {cliff_time}",
            )
        if cliff_time > end_time:
            raise EventByteBudgetError(
                EventByteBudgetError.ZERO_DURATION,
                f"cliff_time {cliff_time} is after end_time {end_time}",
            )
    if len(times) >= 2 and len(set(times)) == 1:
        raise EventByteBudgetError(
            EventByteBudgetError.SAME_LEDGER_TRANSITION,
            f"same-ledger transition: all timestamps equal {times[0]}",
        )
    return True


def validate_batch(batch_size, per_event_bytes):
    """Reject a batch that would overflow or exceed the 16 KiB cap.

    `MAX_BATCH_SIZE * withdrawn` must not overflow and must stay within
    `MAX_EVENT_BYTES`; both are checked before any event is emitted.
    """
    _require_non_negative(batch_size, "batch_size")
    _require_non_negative(per_event_bytes, "per_event_bytes")
    if batch_size > MAX_BATCH_SIZE:
        raise EventByteBudgetError(
            EventByteBudgetError.BATCH_TOO_LARGE,
            f"batch size {batch_size} exceeds MAX_BATCH_SIZE={MAX_BATCH_SIZE}",
        )
    total = batch_size * per_event_bytes
    if per_event_bytes and total // per_event_bytes != batch_size:
        raise EventByteBudgetError(
            EventByteBudgetError.OVERFLOW,
            f"batch total overflowed: {batch_size} * {per_event_bytes}",
        )
    if total > MAX_EVENT_BYTES:
        raise EventByteBudgetError(
            EventByteBudgetError.OVERFLOW,
            f"batch of {batch_size} x {per_event_bytes} = {total} bytes "
            f"exceeds the {MAX_EVENT_BYTES}-byte cap",
        )
    return total


if __name__ == "__main__":
    rows = sizes()
    print(f"{'event':<24}{'topics':>7}{'data':>7}{'total':>7}")
    for r in rows:
        print(f"{r['name']:<24}{r['topics']:>7}{r['data']:>7}{r['total']:>7}")

    w = next(r["total"] for r in rows if r["name"] == "withdrawn")
    print(f"\nwithdrawn = {w} bytes; issue requires < 256 -> "
          f"{'PASS' if w < 256 else 'FAIL'} ({256 - w} spare)")

    worst = max(rows, key=lambda r: r["total"])
    print(f"\nlargest single event: {worst['name']} at {worst['total']} bytes")

    # The event that actually drives the batch cap. `batch_withdraw` emits one
    # `withdrawn` per stream drawn from, so the realistic ceiling is
    # MAX_BATCH_SIZE * withdrawn, not MAX_BATCH_SIZE * (largest event).
    # validate_batch rejects overflow and any total over the 16 KiB cap.
    batch = validate_batch(MAX_BATCH_SIZE, w)
    print(f"\nbatch_withdraw: {MAX_BATCH_SIZE} x {w} = {batch} bytes of "
          f"{MAX_EVENT_BYTES} ({batch / MAX_EVENT_BYTES:.2%})")
    print(f"  headroom: {MAX_EVENT_BYTES - batch} bytes "
          f"({(MAX_EVENT_BYTES - batch) / MAX_EVENT_BYTES:.1%} of the budget)")

    fits = MAX_EVENT_BYTES // worst["total"]
    print(f"\n  even {fits} x {worst['name']} fit in one transaction "
          f"({worst['total'] * fits} bytes)")

    combined = batch + next(
        r["total"] for r in rows if r["name"] == "stream_created"
    )
    print(f"\nfull batch_withdraw + stream_created = {combined} bytes "
          f"({combined / MAX_EVENT_BYTES:.2%} of budget)")
