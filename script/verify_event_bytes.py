"""Independent hand-calculation of every event emitted by the Soroban 16 KiB
Event Byte Footprint Enforcer, written from the stellar-xdr definitions, to
cross-check event-byte-budget.py. Deliberately does NOT import or reuse
anything from that script.
"""
# From Stellar-contract.x:
#   union SCVal switch (SCValType type)
#   case SCV_U64:  uint64 u64;                 -> 4 + 8  = 12
#   case SCV_I128: Int128Parts i128;           -> 4 + 16 = 20
#   case SCV_U32:  uint32 u32;                 -> 4 + 4  = 8
#   case SCV_SYMBOL: SCSymbol sym;             -> string<32>: 4 + 4 + pad4(len)
#   case SCV_ADDRESS: SCAddress address;       -> 4 + 4 (type) + 32 (Hash)
#   case SCV_MAP:  SCMap *map;                 -> 4 + 4 (ptr) + 4 (count) + entries
#   SCV_VEC (topics) -> 4 + 4 (ptr) + 4 (count) + elements
# ContractEvent = ExtentionPoint ext (4) + ContractID contractID (ScVal::Address)

FRAME = 4 + (4 + 4 + 32)        # 44
U64 = 4 + 8                    # 12
ADDR = 4 + 4 + 32              # 40
U32 = 4 + 4                    # 8
I128 = 4 + 16                  # 20


def pad4(n):
    return n + ((4 - n % 4) % 4)


def sym(name):
    return 4 + 4 + pad4(len(name))


def entry(key, val_bytes):
    """An SCMapEntry holds two SCVals, each already a multiple of 4."""
    return pad4(key) + pad4(val_bytes)


def vec(elements):
    """An SCV_VEC: 4 (type) + 4 (ptr) + 4 (count) + elements."""
    return 4 + 4 + 4 + sum(elements)


def map_of(entries):
    """An SCV_MAP: 4 (type) + 4 (ptr) + 4 (count) + entries."""
    return 4 + 4 + 4 + sum(entries)


# ---------------------------------------------------------------------------
# Per-event topic/data schemas, hand-derived from the contract's event
# definitions and the XDR layout above. Each entry is
# (name, topic element sizes, data map entry sizes, pinned total).
# ---------------------------------------------------------------------------
EVENTS = [
    # topics: stream_id (u64), recipient (address)
    # data:   amount, withdrawn, deposited (i128), status (u32)
    ("withdrawn", [U64, ADDR], [
        entry(sym("amount"), I128),
        entry(sym("withdrawn"), I128),
        entry(sym("deposited"), I128),
        entry(sym("status"), U32),
    ], 260),
    # topics: stream_id (u64), sender (address), recipient (address)
    # data:   amount (i128), deposited (i128), rate (i128), status (u32)
    ("stream_created", [U64, ADDR, ADDR], [
        entry(sym("amount"), I128),
        entry(sym("deposited"), I128),
        entry(sym("rate"), I128),
        entry(sym("status"), U32),
    ], 428),
    # topics: stream_id (u64)
    # data:   amount (i128), refunded (i128), status (u32)
    ("cancelled", [U64], [
        entry(sym("amount"), I128),
        entry(sym("refunded"), I128),
        entry(sym("status"), U32),
    ], 300),
    # topics: stream_id (u64)
    # data:   amount (i128), deposited (i128)
    ("topped_up", [U64], [
        entry(sym("amount"), I128),
        entry(sym("deposited"), I128),
    ], 224),
    # topics: stream_id (u64), delegate (address)
    # data:   permissions (u32)
    ("delegate_granted", [U64, ADDR], [
        entry(sym("permissions"), U32),
    ], 224),
    # topics: stream_id (u64)
    # data:   status (u32)
    ("resumed", [U64], [
        entry(sym("status"), U32),
    ], 188),
    # topics: stream_id (u64)
    # data:   status (u32)
    ("paused", [U64], [
        entry(sym("status"), U32),
    ], 184),
    # topics: stream_id (u64), recipient (address)
    # data:   amount (i128)
    ("recipient_transferred", [U64, ADDR], [
        entry(sym("amount"), I128),
    ], 160),
    # topics: stream_id (u64), delegate (address)
    # data:   permissions (u32)
    ("delegate_revoked", [U64, ADDR], [
        entry(sym("permissions"), U32),
    ], 160),
    # topics: stream_id (u64)
    # data:   ttl (u32)
    ("ttl_extended", [U64], [
        entry(sym("ttl"), U32),
    ], 116),
]


def compute(topics_elems, data_entries):
    topics = vec(topics_elems)
    data = map_of(data_entries)
    return topics, data, FRAME + topics + data


def main():
    print(f"FRAME  = {FRAME}")
    print(f"val sizes: i128={I128} u32={U32} u64={U64} addr={ADDR}")
    print()
    for name, topics_elems, data_entries, pinned in EVENTS:
        topics, data, total = compute(topics_elems, data_entries)
        print(f"{name}: topics={topics} data={data} total={total}")
        assert total == pinned, (
            f"{name}: expected {pinned}, got {total}"
        )
    print()
    print(f"MATCHES event-byte-budget.py ({len(EVENTS)} events)")


if __name__ == "__main__":
    main()
