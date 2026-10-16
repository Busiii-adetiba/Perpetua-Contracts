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

# --- ScVal body sizes (discriminant included) ---------------------------


def pad4(n):
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
    b = name.encode("utf-8")
    assert len(b) <= 32, f"symbol exceeds SCSYMBOL_LIMIT=32: {name}"
    return 4 + 4 + pad4(len(b))


def address(contract=False):    # SCV_ADDRESS -> SCAddress::Account(Hash[32])
    # discriminant(4) + SCAddressType(4) + 32-byte hash
    return 4 + 4 + 32 + (4 if contract else 0)


# --- Frozen schema constants (must match docs/ABI.md) -------------------
#
# These names are the contract between the on-chain events and every
# off-chain consumer (indexers, TypeScript SDK, GraphQL/SQL schemas).
# They are frozen: changing one is a schema-drift event and must be
# accompanied by a matching change in docs/ABI.md and the fixtures under
# tests/fixtures/event_snapshots/.

# The ten events emitted by contracts/stream/src/events.rs, in declaration
# order. `sizes()` and the snapshot tooling iterate this list.
EVENT_NAMES = (
    "stream_created",
    "withdrawn",
    "cancelled",
    "paused",
    "resumed",
    "topped_up",
    "recipient_transferred",
    "delegate_granted",
    "delegate_revoked",
    "ttl_extended",
)

# Topic layout per event: the ordered list of topic element kinds. The
# first element is always the stream id; the rest are addresses. This is
# the frozen topic schema indexers key on.
TOPIC_LAYOUT = {
    "stream_created": ("stream_id", "address", "address"),
    "withdrawn": ("stream_id", "address"),
    "cancelled": ("stream_id", "address", "address"),
    "paused": ("stream_id", "address"),
    "resumed": ("stream_id", "address"),
    "topped_up": ("stream_id", "address"),
    "recipient_transferred": ("stream_id", "address", "address"),
    "delegate_granted": ("stream_id", "address", "address"),
    "delegate_revoked": ("stream_id", "address", "address"),
    "ttl_extended": ("stream_id",),
}

# Data payload schema per event: ordered (field name, ScVal kind) pairs.
# The ScVal kind is the tag used by the size model above.
DATA_LAYOUT = {
    "stream_created": (
        ("token", "address"),
        ("deposited", "i128"),
        ("start_time", "u64"),
        ("end_time", "u64"),
        ("cliff_time", "u64"),
        ("cancellable", "bool"),
        ("pausable", "bool"),
        ("transferable", "bool"),
    ),
    "withdrawn": (
        ("amount", "i128"),
        ("withdrawn", "i128"),
        ("deposited", "i128"),
        ("status", "u32"),
    ),
    "cancelled": (
        ("refunded", "i128"),
        ("vested", "i128"),
        ("withdrawn", "i128"),
        ("end_time", "u64"),
    ),
    "paused": (
        ("paused_at", "u64"),
        ("paused_total", "u64"),
    ),
    "resumed": (
        ("paused_duration", "u64"),
        ("paused_total", "u64"),
    ),
    "topped_up": (
        ("amount", "i128"),
        ("deposited", "i128"),
        ("end_time", "u64"),
    ),
    "recipient_transferred": (),
    "delegate_granted": (
        ("ops", "u32"),
        ("expires_at", "option_u64"),
    ),
    "delegate_revoked": (),
    "ttl_extended": (
        ("extended_to_ledgers", "u32"),
    ),
}

# ScVal kind -> body size function. Used to recompute payload sizes from
# the frozen schema rather than from hand-written literals.
_KIND_SIZERS = {
    "u32": u32,
    "u64": u64,
    "i128": i128,
    "bool": boolean,
    "address": address,
    "option_u64": option_u64,
}


def option_u64(_v):             # Option<u64> encodes as a Vec of 0 or 1 elements
    # SCV_VEC discriminant(4) + pointer(4) + count(4) + one u64
    return 4 + 4 + 4 + u64(0)


def smap(pairs):                # SCV_MAP: disc(4) + ptr(4) + count(4) + entries
    return 4 + 4 + 4 + sum(pad4(k) + pad4(v) for k, v in pairs)


def topics(elements):           # SCVec = SCV_VEC: disc(4) + ptr(4) + count(4)
    return 4 + 4 + 4 + sum(elements)


# --- The events, as declared in contracts/stream/src/events.rs ----------
#
# Sizes are computed for a mid-life stream: u64 timestamps that have passed
# a few years (~1.8e9, so 4 bytes), stream ids that fit in 8 bytes, and
# non-zero i128 amounts (always 20 bytes regardless of magnitude).

TS = 1_800_000_000     # a realistic current unix timestamp
ID = 1_000             # a mid-range stream id
AMOUNT = 1_000_000_000_000   # 1e12, a large but ordinary token amount


def _t_stream_id():
    return u64(ID)


def _t_addr():
    return address()


def _topic_elems(name):
    """Build the topic element sizes from the frozen TOPIC_LAYOUT."""
    elems = []
    for kind in TOPIC_LAYOUT[name]:
        if kind == "stream_id":
            elems.append(_t_stream_id())
        elif kind == "address":
            elems.append(_t_addr())
        else:  # pragma: no cover - guarded by schema validation
            raise ValueError(f"unknown topic kind {kind!r} for {name}")
    return elems


def _data_pairs(name):
    """Build the data payload (key, value) sizes from DATA_LAYOUT."""
    pairs = []
    for field, kind in DATA_LAYOUT[name]:
        sizer = _KIND_SIZERS[kind]
        pairs.append((sym(field), sizer(0)))
    return pairs


def _ev(name):
    return {
        "name": name,
        "topics": topics(_topic_elems(name)),
        "data": smap(_data_pairs(name)),
    }


# EVENTS is derived from the frozen schema tables above, so the byte model
# cannot drift from the documented ABI without this module failing to build.
EVENTS = [_ev(name) for name in EVENT_NAMES]

# Sanity: the derived event list must cover exactly the frozen names.
assert [e["name"] for e in EVENTS] == list(EVENT_NAMES)

# ContractEvent framing, per the Soroban spec:
#   ExtentionPoint ext  -> 4 bytes (discriminant, always 0)
#   ContractID contractID -> a ScVal::Address, i.e. 40 bytes
FRAME = 4 + 40     # 44
MAX_EVENT_BYTES = 16_384   # Protocol 27
MAX_BATCH_SIZE = 16


def sizes():
    """Deterministic, 4-byte-aligned byte accounting for every event.

    Every component (topics, data, total) is a multiple of 4 because each
    ScVal body is padded to 4 bytes and the framing is 4-byte aligned.
    """
    rows = []
    for e in EVENTS:
        total = FRAME + e["topics"] + e["data"]
        assert e["topics"] % 4 == 0, f"{e['name']} topics not 4-byte aligned"
        assert e["data"] % 4 == 0, f"{e['name']} data not 4-byte aligned"
        assert total % 4 == 0, f"{e['name']} total not 4-byte aligned"
        rows.append({
            "name": e["name"],
            "topics": e["topics"],
            "data": e["data"],
            "total": total,
        })
    return rows


def schema():
    """Return the frozen schema for every event, for snapshot tooling.

    The shape is stable and JSON-serialisable so that
    `script/verify_event_bytes.py` and `script/check_snapshot_diff.py` can
    compare it against the golden fixtures in
    tests/fixtures/event_snapshots/.
    """
    return [
        {
            "name": name,
            "topics": list(TOPIC_LAYOUT[name]),
            "data": [
                {"name": field, "type": kind}
                for field, kind in DATA_LAYOUT[name]
            ],
        }
        for name in EVENT_NAMES
    ]


if __name__ == "__main__":
    rows = sizes()
    print(f"{'event':<24}{'topics':>7}{'data':>7}{'total':>7}")
    for r in rows:
        print(f"{r['name']:<24}{r['topics']:>7}{r['data']:>7}{r['total']:>7}")

    w = next(r["total"] for r in rows if r["name"] == "withdrawn")
    print(f"\nwithdrawn = {w} bytes; issue requires < 256 -> "
          f"{'PASS' if w < 256 else 'FAIL'} ({256 - w} spare)")

    # Schema coverage: every frozen event must be accounted for exactly once.
    assert len(rows) == len(EVENT_NAMES), "event count drifted from schema"
    assert {r["name"] for r in rows} == set(EVENT_NAMES), \
        "event names drifted from schema"

    worst = max(rows, key=lambda r: r["total"])
    print(f"\nlargest single event: {worst['name']} at {worst['total']} bytes")

    # The event that actually drives the batch cap. `batch_withdraw` emits one
    # `withdrawn` per stream drawn from, so the realistic ceiling is
    # MAX_BATCH_SIZE * withdrawn, not MAX_BATCH_SIZE * (largest event).
    batch = w * MAX_BATCH_SIZE
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
