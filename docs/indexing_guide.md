# Indexing Perpetua: Soroban RPC and Horizon

Perpetua has two useful Stellar data surfaces, but they answer different
questions:

| Need | Recommended source | Reason |
|---|---|---|
| Discover or replay Perpetua contract events | Soroban RPC `getEvents` | Contract events are returned with their contract ID, topics, value, ledger, transaction hash, and event indexes. |
| Read current stream state | Soroban RPC `getLedgerEntries` or contract simulation | The contract storage is the source of truth for the current state. Use the generated contract client to decode values. |
| Find payments, account activity, trustlines, offers, and asset history | Horizon REST | Horizon's resource model is designed for account, operation, payment, and asset queries. |
| Link a contract event to a transaction or inspect transaction status | Soroban RPC, optionally Horizon | RPC supplies the event's transaction hash and ledger context; use Horizon only when its transaction/resource view is useful for the same network. |

Horizon is not a substitute for `getEvents` when the indexer needs every
Perpetua event. A payment operation may show token movement, but it does not
replace the contract event's stream ID, event-specific payload, or event order.

## Perpetua event model

The stream contract emits ten event types. `topic[0]` is always the event name
in snake case, and `topic[1]` is always `stream_id`; the remaining slots hold
the parties the event routes on.

| event | topic[1] | topic[2] | topic[3] |
|---|---|---|---|
| `stream_created` | `stream_id` | `sender` | `recipient` |
| `withdrawn` | `stream_id` | `recipient` | — |
| `cancelled` | `stream_id` | `sender` | `recipient` |
| `paused` | `stream_id` | `sender` | — |
| `resumed` | `stream_id` | `sender` | — |
| `topped_up` | `stream_id` | `sender` | — |
| `recipient_transferred` | `stream_id` | `old_recipient` | `new_recipient` |
| `delegate_granted` | `stream_id` | `grantor` | `delegate` |
| `delegate_revoked` | `stream_id` | `grantor` | `delegate` |
| `ttl_extended` | `stream_id` | — | — |

**Topic arity is not constant.** It ranges from 2 (`ttl_extended`) to 4
(`stream_created`, `cancelled`, `recipient_transferred`, and both delegate
events). A consumer must read the topic count per event rather than assuming
four entries.

`stream_created` carries the complete initial stream state in its payload and
is the bootstrap event for building a sender/recipient mapping. `withdrawn`
carries the amount and cumulative accounting fields. The full payload layout
and the ordering rationale are in [ABI.md](ABI.md#topic-positions), which is
the interface of record.

A `stream_created` event's topic vector is therefore:

```text
["stream_created", stream_id, sender, recipient]
```

The values sent to JSON-RPC are base64-encoded XDR `SCVal` values, not plain
text. Do not put the literal string `stream_created` or a raw integer in a
filter. Use the Stellar SDK's `ScVal`/XDR encoder for topic filters and decoder
for returned topics and values. The contract interface is the decoding source
of truth; see [`contracts/stream/abi/fluxora_stream.json`](../contracts/stream/abi/fluxora_stream.json).

## Event reconstruction rules

This section is normative. An indexer that follows these rules will produce a
byte-for-byte identical projection to any other conforming indexer, and to the
on-chain state, for the same set of events. The rules are machine-checkable:
[`script/event-byte-budget.py`](../script/event-byte-budget.py) encodes the
canonical topic layout and payload field order in its `EVENT_SCHEMA` table and
exposes `schema_fingerprint()` as a stable digest of that schema. Treat
`EVENT_SCHEMA` and `schema_fingerprint()` as the source of truth; this document
is a human-readable restatement of them.

### Topic layout

Every Perpetua event uses the same positional topic layout:

1. `topic[0]` — the event name as a snake_case `symbol` (for example
   `stream_created`). This is the discriminator an indexer switches on.
2. `topic[1]` — `stream_id`, encoded as `u64`. Every event carries it, so a
   stream-scoped projection can always be keyed by `(contract_id, stream_id)`.
3. `topic[2..]` — the routing parties for the event, in the order listed in
   the table above. These are `address` values. Events with no routing parties
   (`ttl_extended`) stop at `topic[1]`; events with one or two parties append
   them in the documented order.

The number of topics is therefore `2 + len(routing_parties)` and is **not**
constant across event types. Read the topic count from the event; never assume
a fixed arity. The routing-party order is part of the schema: `cancelled`
routes `(sender, recipient)` and `recipient_transferred` routes
`(old_recipient, new_recipient)`, and swapping them is a schema violation even
though the arity is identical.

### Payload field order

The event `data` payload is a struct whose fields are ordered exactly as
declared in `EVENT_SCHEMA`. Field order is significant because the payload is
serialized positionally; an indexer must decode fields by position, not by
name, and must not reorder them. Per-event payload layouts:

- `stream_created` — the complete initial stream state (sender, recipient,
  token, amounts, schedule, and status). This is the bootstrap event; a
  projection is built from it and then mutated by later events.
- `withdrawn` — the withdrawn amount plus the cumulative accounting fields.
- `cancelled` — the terminal accounting fields for the stream.
- `paused`, `resumed`, `topped_up` — the fields describing the transition.
- `recipient_transferred` — the old and new recipient.
- `delegate_granted`, `delegate_revoked` — the grantor and delegate.
- `ttl_extended` — the extension metadata.

Decode amounts as `i128` token base units and keep them as integers or decimal
strings; never pass them through an IEEE-754 `Number`.

### Forward compatibility: tolerate unknown trailing fields

Indexers **must tolerate unknown trailing fields** in the payload. A future
contract version may append fields to an existing event's payload without
changing its topic layout or its leading field order. A conforming decoder:

- reads the fields it knows by position from the front of the payload,
- ignores any additional trailing fields it does not recognize, and
- does not fail, truncate, or reorder the event when extra fields are present.

This keeps the topic layout and the leading payload prefix stable across
contract upgrades, so an indexer built against an older `EVENT_SCHEMA` keeps
working against a newer contract. Removing or reordering existing fields is a
breaking change and requires a new event name; appending trailing fields is
not.

### Parity corpus and CI checks

The golden JSON fixtures under
[`tests/fixtures/event_snapshots/`](../tests/fixtures/event_snapshots/) are the
parity corpus for these rules. Each fixture is a frozen, canonical
serialization of an event (topics plus payload) that every implementation —
Rust contract, TypeScript SDK, and GraphQL/SQL schema — must reproduce exactly.

CI enforces parity with:

- [`script/event-byte-budget.py`](../script/event-byte-budget.py) — the
  `EVENT_SCHEMA` table and `schema_fingerprint()` that define the canonical
  schema, plus the 16 KiB byte-footprint budget.
- [`script/verify_event_bytes.py`](../script/verify_event_bytes.py) — verifies
  each event's encoded footprint against the budget and the schema.
- [`script/check_snapshot_diff.py`](../script/check_snapshot_diff.py) — diffs
  freshly generated snapshots against the golden fixtures to detect schema
  drift.

A change to the topic layout or payload field order must update `EVENT_SCHEMA`,
regenerate the golden fixtures, and pass all three scripts; otherwise CI fails
with a schema-drift error.

## `getEvents` filter

A request for Perpetua events uses the Soroban RPC `getEvents` method. The
contract ID belongs in `filters[].contractIds`:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "getEvents",
  "params": {
    "startLedger": 123456,
    "endLedger": 124000,
    "filters": [
      {
        "type": "contract",
        "contractIds": ["C...PERPETUA_CONTRACT_ID..."],
        "topics": [["AAA...base64-SCVal(stream_created)..."]]
      }
    ],
    "pagination": {
      "limit": 1000
    }
  }
}
```

Important details:

- `startLedger` is the checkpoint to resume from. Set `endLedger` to bound a
  backfill; omit it when following new closed ledgers, subject to the RPC
  provider's limits.
- `contractIds` is the strongest filter and should always contain the exact
  Perpetua contract ID. Do not rely on event names alone when sharing an RPC
  endpoint with other contracts.
- `topics` is optional. A topic filter is positional: the first item matches
  `topic[0]`, the second matches `topic[1]`, and so on. Use the RPC wildcard
  for positions that should not be constrained. For example, a stream-specific
  filter can match `stream_created` at position zero and the encoded stream ID
  at position one.
- A filter with only `contractIds` is usually the safest way to backfill every
  Perpetua event. Narrow topic filters are useful for live projections, but a
  new event type can otherwise be missed by an indexer that has hard-coded an
  incomplete filter set.
- Consume every page before advancing the checkpoint. Persist the last fully
  processed ledger and event identity, and use a small overlap when restarting
  so a provider boundary or process failure cannot create a gap. Deduplicate by
  `(txHash, eventIndex)`; retain `ledger`, `txIndex`, and the raw event for
  auditing.
- Decode amounts as integers or decimal strings. Perpetua amounts are `i128`
  token base units and must never pass through an IEEE-754 JavaScript `Number`.
- `getEvents` is for closed-ledger history. It is not a mempool subscription and
  it does not replace confirmation handling for a submitted transaction.

For a stream-specific query, the conceptual filter is:

```json
{
  "type": "contract",
  "contractIds": ["C...PERPETUA_CONTRACT_ID..."],
  "topics": [
    ["AAA...SCVal(symbol('stream_created'))...", "AAA...SCVal(u64(42))..."]
  ]
}
```

The exact base64 strings should be generated by the SDK, not copied from this
example. Topic indexes are defined by the deployed contract ABI and must be
updated if a new contract deployment changes the event schema.

## Minimal JavaScript indexer

This example uses native `fetch` and treats the event topic/value fields as
opaque until they are decoded with the Stellar SDK generated from the contract
spec. It demonstrates a contract-wide backfill with a persisted ledger
checkpoint and event deduplication.

```js
const RPC_URL = process.env.SOROBAN_RPC_URL;
const CONTRACT_ID = process.env.PERPETUA_STREAM_CONTRACT_ID;
let nextLedger = Number(process.env.START_LEDGER);
const seen = new Set();

async function getEvents(startLedger, endLedger) {
  const response = await fetch(RPC_URL, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "getEvents",
      params: {
        startLedger,
        ...(endLedger === undefined ? {} : { endLedger }),
        filters: [{ type: "contract", contractIds: [CONTRACT_ID] }],
        pagination: { limit: 1000 },
      },
    }),
  });

  const body = await response.json();
  if (body.error) throw new Error(JSON.stringify(body.error));
  return body.result;
}

async function indexClosedLedgers(endLedger) {
  const page = await getEvents(nextLedger, endLedger);
  for (const event of page.events) {
    const identity = `${event.txHash}:${event.eventIndex}`;
    if (seen.has(identity)) continue;
    seen.add(identity);

    // Decode event.topic and event.value with the Stellar SDK and the
    // fluxora_stream ABI before writing the typed projection.
    await saveRawEvent({ identity, event });
  }

  // Persist this only after saveRawEvent has committed successfully. In a
  // production indexer, use the provider cursor as well when more pages exist.
  if (page.events.length > 0) {
    nextLedger = Math.max(...page.events.map((event) => event.ledger)) + 1;
    await saveCheckpoint(nextLedger);
  }
}
```

For a production indexer, replace the in-memory `seen` set with a database
unique constraint on `(contract_id, tx_hash, event_index)`, persist the RPC
pagination cursor, and retry failed pages before moving the ledger checkpoint.
Use a generated decoder rather than relying on the event names alone.

## Minimal Python request

The same contract-wide filter can be issued with Python's standard library:

```python
import json
import os
import urllib.request

payload = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "getEvents",
    "params": {
        "startLedger": int(os.environ["START_LEDGER"]),
        "endLedger": int(os.environ["END_LEDGER"]),
        "filters": [{
            "type": "contract",
            "contractIds": [os.environ["PERPETUA_STREAM_CONTRACT_ID"]],
        }],
        "pagination": {"limit": 1000},
    },
}

request = urllib.request.Request(
    os.environ["SOROBAN_RPC_URL"],
    data=json.dumps(payload).encode("utf-8"),
    headers={"content-type": "application/json"},
)
with urllib.request.urlopen(request) as response:
    result = json.load(response)

if "error" in result:
    raise RuntimeError(result["error"])
for event in result["result"]["events"]:
    print(event["ledger"], event["txHash"], event["eventIndex"])
```

This Python snippet intentionally prints raw event metadata only. Decode the
base64 XDR topics and value with a Stellar Python SDK or XDR implementation
before interpreting stream IDs, addresses, statuses, or amounts.

## Operational recommendations

1. Backfill from a known ledger before the contract deployment ledger, then
   advance in bounded ranges until the latest closed ledger.
2. Store the contract ID, ledger, transaction hash, event index, raw topics,
   raw value, and decoded event version. Raw data makes re-decoding possible
   after an ABI decoder fix.
3. Build the stream projection from `stream_created` and apply subsequent
   events in ledger/transaction/event order. Periodically reconcile important
   records with `getLedgerEntries` because events describe transitions while
   storage describes current state.
4. Keep Horizon ingestion separate. Join its payment/account records to RPC
   events by transaction hash when a user-facing view needs both contract
   semantics and token/account activity.
5. Treat an RPC provider's retention and rate limits as deployment concerns.
   For a durable historical index, archive raw pages or use a provider that
   guarantees access to the ledger range you need.