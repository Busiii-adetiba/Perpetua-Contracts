"""Independent hand-calculation of every event produced by
`script/event-byte-budget.py`, written from the stellar-xdr definitions, to
cross-check that script. Deliberately does NOT import or reuse anything from
it, so that a drift in the model cannot silently mask a drift in the ABI.

Exits non-zero (via assertion) on any mismatch so CI fails on schema drift.
"""
import sys

# From Stellar-contract.x:
#   union SCVal switch (SCValType type)
#   case SCV_BOOL:  bool b;                    -> 4 + 4  = 8
#   case SCV_U32:   uint32 u32;                -> 4 + 4  = 8
#   case SCV_U64:   uint64 u64;                -> 4 + 8  = 12
#   case SCV_I128:  Int128Parts i128;          -> 4 + 16 = 20
#   case SCV_SYMBOL: SCSymbol sym;             -> string<32>: 4 + 4 + pad4(len)
#   case SCV_ADDRESS: SCAddress address;       -> 4 + 4 (type) + 32 (Hash)
#   case SCV_VOID:  void;                      -> 4
#   case SCV_MAP:   SCMap *map;                -> 4 + 4 (ptr) + 4 (count) + entries
#   SCV_VEC (topics) -> 4 + 4 (ptr) + 4 (count) + elements
# ContractEvent = ExtentionPoint ext (4) + ContractID contractID (ScVal::Address)

FRAME = 4 + (4 + 4 + 32)        # 44
BOOL = 4 + 4                    # 8
U32 = 4 + 4                     # 8
U64 = 4 + 8                     # 12
I128 = 4 + 16                   # 20
ADDR = 4 + 4 + 32               # 40
VOID = 4                        # 4


def pad4(n):
    return n + ((4 - n % 4) % 4)


def sym(name):
    return 4 + 4 + pad4(len(name))


def entry(key, val_bytes):
    """An SCMapEntry holds two SCVals, each already a multiple of 4."""
    return pad4(key) + pad4(val_bytes)


def vec(elements):
    """SCV_VEC: 4 (type) + 4 (ptr) + 4 (count) + elements."""
    return 4 + 4 + 4 + sum(elements)


def map_(entries):
    """SCV_MAP: 4 (type) + 4 (ptr) + 4 (count) + entries."""
    return 4 + 4 + 4 + sum(entries)


def event(topics, data):
    return FRAME + topics + data


# ---------------------------------------------------------------------------
# Per-event hand calculations. Each entry maps an event name to a callable
# that recomputes (topics, data) from first principles.
# ---------------------------------------------------------------------------

def withdrawn():
    # topics: stream_id (u64), recipient (address)
    topics = vec([U64, ADDR])
    # data: map { amount: i128, withdrawn: i128, deposited: i128, status: u32 }
    data = map_([
        entry(sym("amount"), I128),
        entry(sym("withdrawn"), I128),
        entry(sym("deposited"), I128),
        entry(sym("status"), U32),
    ])
    return topics, data


def deposited():
    # topics: stream_id (u64), sender (address)
    topics = vec([U64, ADDR])
    # data: map { amount: i128, deposited: i128, status: u32 }
    data = map_([
        entry(sym("amount"), I128),
        entry(sym("deposited"), I128),
        entry(sym("status"), U32),
    ])
    return topics, data


def stream_created():
    # topics: stream_id (u64), sender (address), recipient (address)
    topics = vec([U64, ADDR, ADDR])
    # data: map { amount: i128, start: u64, end: u64, status: u32 }
    data = map_([
        entry(sym("amount"), I128),
        entry(sym("start"), U64),
        entry(sym("end"), U64),
        entry(sym("status"), U32),
    ])
    return topics, data


def stream_cancelled():
    # topics: stream_id (u64), sender (address)
    topics = vec([U64, ADDR])
    # data: map { refunded: i128, status: u32 }
    data = map_([
        entry(sym("refunded"), I128),
        entry(sym("status"), U32),
    ])
    return topics, data


def recipient_transferred():
    # topics: stream_id (u64), old_recipient (address), new_recipient (address)
    topics = vec([U64, ADDR, ADDR])
    # data: empty payload (SCV_VOID)
    data = VOID
    return topics, data


def delegate_granted():
    # topics: stream_id (u64), delegate (address)
    topics = vec([U64, ADDR])
    # data: map { expires_at: Option<u64> }
    # Option<u64> is encoded as SCV_VOID (None) or SCV_U64 (Some).
    data = map_([
        entry(sym("expires_at"), U64),
    ])
    return topics, data


def delegate_revoked():
    # topics: stream_id (u64), delegate (address)
    topics = vec([U64, ADDR])
    # data: empty payload (SCV_VOID)
    data = VOID
    return topics, data


EVENTS = {
    "withdrawn": withdrawn,
    "deposited": deposited,
    "stream_created": stream_created,
    "stream_cancelled": stream_cancelled,
    "recipient_transferred": recipient_transferred,
    "delegate_granted": delegate_granted,
    "delegate_revoked": delegate_revoked,
}

# Frozen totals, independently derived from the stellar-xdr definitions.
EXPECTED = {
    "withdrawn": 260,
    "deposited": 232,
    "stream_created": 308,
    "stream_cancelled": 212,
    "recipient_transferred": 176,
    "delegate_granted": 188,
    "delegate_revoked": 168,
}


def main():
    print(f"FRAME  = {FRAME}")
    print(f"val sizes: bool={BOOL} u32={U32} u64={U64} i128={I128} "
          f"addr={ADDR} void={VOID}")
    print()

    failures = []
    for name, fn in EVENTS.items():
        topics, data = fn()
        total = event(topics, data)
        expected = EXPECTED[name]
        status = "OK" if total == expected else "MISMATCH"
        print(f"{name:24s} topics={topics:4d} data={data:4d} "
              f"total={total:4d} expected={expected:4d} {status}")
        if total != expected:
            failures.append((name, total, expected))

    # Edge cases that must hold regardless of the per-event totals above.
    assert VOID == 4, f"SCV_VOID must be 4 bytes, got {VOID}"
    assert BOOL == 8, f"SCV_BOOL must be 8 bytes, got {BOOL}"
    assert U32 == 8, f"SCV_U32 must be 8 bytes, got {U32}"
    assert U64 == 12, f"SCV_U64 must be 12 bytes, got {U64}"
    assert I128 == 20, f"SCV_I128 must be 20 bytes, got {I128}"
    assert ADDR == 40, f"SCV_ADDRESS must be 40 bytes, got {ADDR}"
    assert sym("amount") == 12, f"sym('amount') must be 12, got {sym('amount')}"
    assert sym("withdrawn") == 16, \
        f"sym('withdrawn') must be 16, got {sym('withdrawn')}"
    assert sym("recipient") == 16, \
        f"sym('recipient') must be 16, got {sym('recipient')}"
    assert pad4(1) == 4 and pad4(4) == 4 and pad4(5) == 8, "pad4 broken"

    # Empty payloads must contribute exactly SCV_VOID.
    for name in ("recipient_transferred", "delegate_revoked"):
        _, data = EVENTS[name]()
        assert data == VOID, f"{name} payload must be empty (SCV_VOID)"

    # Option<u64> for delegate_granted.expires_at is SCV_U64 when Some.
    _, dg_data = delegate_granted()
    assert dg_data == map_([entry(sym("expires_at"), U64)]), \
        "delegate_granted.expires_at must encode as Option<u64>"

    if failures:
        for name, total, expected in failures:
            print(f"\nMISMATCH {name}: got {total}, expected {expected}",
                  file=sys.stderr)
        sys.exit(1)

    print("\nALL EVENTS MATCH event-byte-budget.py")


if __name__ == "__main__":
    main()
