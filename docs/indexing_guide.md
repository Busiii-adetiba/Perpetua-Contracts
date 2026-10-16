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

## Deterministic event reconstruction rules

This section is normative for any indexer that must produce byte-identical
projections from the same ledger history. The frozen interface of record is
[ABI.md](ABI.md); the golden fixtures under
[`tests/fixtures/event_snapshots/`](../tests/fixtures/event_snapshots/) are the
executable form of these rules. When this guide, the ABI, and the fixtures
disagree, the fixtures and ABI win and this document must be corrected.

### Topic layout

Every Perpetua event has a fixed topic layout. `topic[0]` is always the event
name encoded as a Soroban `symbol` in snake case. `topic[1]` is always the
`stream_id` encoded as `u64`. Any remaining slots are the routing parties for
that event, in the order listed below. Slots that are not listed are absent —
they are not present-but-empty.

| event | topic[0] | topic[1] | topic[2] | topic[3] | topic count |
|---|---|---|---|---|---|
| `stream_created` | `symbol("stream_created")` | `u64 stream_id` | `address sender` | `address recipient` | 4 |
| `withdrawn` | `symbol("withdrawn")` | `u64 stream_id` | `address recipient` | — | 3 |
| `cancelled` | `symbol("cancelled")` | `u64 stream_id` | `address sender` | `address recipient` | 4 |
| `paused` | `symbol("paused")` | `u64 stream_id` | `address sender` | — | 3 |
| `resumed` | `symbol("resumed")` | `u64 stream_id` | `address sender` | — | 3 |
| `topped_up` | `symbol("topped_up")` | `u64 stream_id` | `address sender` | — | 3 |
| `recipient_transferred` | `symbol("recipient_transferred")` | `u64 stream_id` | `address old_recipient` | `address new_recipient` | 4 |
| `delegate_granted` | `symbol("delegate_granted")` | `u64 stream_id` | `address grantor` | `address delegate` | 4 |
| `delegate_revoked` | `symbol("delegate_revoked")` | `u64 stream_id` | `address grantor` | `address delegate` | 4 |
| `ttl_extended` | `symbol("ttl_extended")` | `u64 stream_id` | — | — | 2 |

Rules that follow from the table:

- Never assume a constant topic arity. Read `topics.length` per event and
  dispatch on `topic[0]` first, then validate the arity for that event name.
- An event whose `topic[0]` is not in the table is unknown. Persist it raw and
  skip projection; do not guess a layout.
- An event whose arity does not match the table is malformed. Fail the page,
  do not advance the checkpoint, and surface the raw event for investigation.
- Addresses are `ScVal` addresses, not strings. Decode them with the generated
  SDK; never compare base64 topic bytes against hand-written literals.

### Payload schema

The data payload (`event.value`) is a single `ScVal` map. Field names are
snake case and match the ABI. All amounts are `i128` token base units.

| event | payload fields |
|---|---|
| `stream_created` | `stream_id`, `sender`, `recipient`, `deposit_amount`, `rate_per_ledger`, `start_ledger`, `cliff_ledger`, `end_ledger`, `withdrawable`, `status` |
| `withdrawn` | `stream_id`, `recipient`, `amount`, `total_withdrawn`, `withdrawable`, `status` |
| `cancelled` | `stream_id`, `sender`, `recipient`, `refund_amount`, `total_withdrawn`, `status` |
| `paused` | `stream_id`, `sender`, `paused_at_ledger`, `accrued_at_pause`, `status` |
| `resumed` | `stream_id`, `sender`, `resumed_at_ledger`, `accrued_at_resume`, `status` |
| `topped_up` | `stream_id`, `sender`, `amount`, `deposit_amount`, `end_ledger`, `status` |
| `recipient_transferred` | `stream_id`, `old_recipient`, `new_recipient`, `status` |
| `delegate_granted` | `stream_id`, `grantor`, `delegate`, `permissions`, `status` |
| `delegate_revoked` | `stream_id`, `grantor`, `delegate`, `status` |
| `ttl_extended` | `stream_id`, `extended_to_ledger` |

Payload rules:

- Decode the map by field name, not by position. A future additive field must
  not shift the meaning of an existing one.
- `status` is the stream status after the state change, encoded as the ABI
  enum. It is authoritative for the transition described by the event.
- `withdrawable` is the amount claimable at the event's ledger, computed by the
  contract. Do not recompute it from `rate_per_ledger` when the contract
  supplies it; recomputation is only for reconciliation checks.
- Amounts must be handled as integers or decimal strings. Never route an
  `i128` through an IEEE-754 `Number`.

### Ordering guarantees

The contract emits exactly one event per state change, and events are ordered
by the ledger that closed them:

1. Ledgers are totally ordered by `ledger` (ascending).
2. Within a ledger, transactions are ordered by `txIndex` (ascending).
3. Within a transaction, events are ordered by `eventIndex` (ascending).

The tuple `(ledger, txIndex, eventIndex)` is the canonical total order. Apply
events in exactly that order. `(txHash, eventIndex)` is the deduplication key;
`ledger` and `txIndex` are retained for audit and ordering. Because there is
one event per state change, folding the ordered event stream reproduces the
contract's state transitions without replaying storage writes.

### Folding events into the `Stream` mirror

Start from the `stream_created` event, which carries the complete initial
state, then apply each subsequent event for that `stream_id` in canonical
order. The mirror is a pure function of the ordered event stream.

- `stream_created`: insert a new `Stream` row from the payload. Set
  `deposit_amount`, `rate_per_ledger`, `start_ledger`, `cliff_ledger`,
  `end_ledger`, `withdrawable`, and `status`. Record `sender` and `recipient`
  from the payload and cross-check them against `topic[2]`/`topic[3]`.
- `withdrawn`: add `amount` to `total_withdrawn`, set `withdrawable` to the
  payload value, and set `status` from the payload. Do not recompute
  `total_withdrawn` by summing `amount` across events unless reconciling; the
  payload's cumulative field is authoritative.
- `topped_up`: add `amount` to `deposit_amount`, adopt the payload's
  `end_ledger`, and set `status`. A top-up can extend the stream, so
  `end_ledger` must be overwritten, not max-merged blindly.
- `paused`: set `status` to paused, store `paused_at_ledger`, and store
  `accrued_at_pause`. Accrual stops at `paused_at_ledger`; the mirror must not
  accrue withdrawable between `paused_at_ledger` and the resume ledger.
- `resumed`: set `status` to active, store `resumed_at_ledger` and
  `accrued_at_resume`, and shift the accrual window so that accrual restarts at
  `resumed_at_ledger`. The elapsed paused interval is excluded from accrual;
  `accrued_at_resume` is the contract's authoritative accrued amount at resume.
- `cancelled`: set `status` to cancelled, record `refund_amount` and
  `total_withdrawn` from the payload. A cancelled stream is terminal: no
  further `withdrawn`, `paused`, `resumed`, or `topped_up` events may be
  applied to it. If one arrives, treat it as a schema violation and halt.
- `recipient_transferred`: replace `recipient` with `new_recipient`. The
  `old_recipient` field and `topic[2]` must match the current mirror value;
  a mismatch is a reconstruction error.
- `delegate_granted` / `delegate_revoked`: upsert or delete the delegate
  permission row keyed by `(stream_id, grantor, delegate)`. These events do not
  change the `Stream` row itself.
- `ttl_extended`: record `extended_to_ledger` for operational visibility. It
  does not change stream accounting or status.

Status transitions are therefore: created → active; active → paused → active;
active → cancelled; active → completed when `withdrawable` reaches zero at or
after `end_ledger`. Terminal states (`cancelled`, `completed`) never transition
again.

### Decoding with the generated SDK

Decode topics and payloads with the SDK generated from
[`contracts/stream/abi/fluxora_stream.json`](../contracts/stream/abi/fluxora_stream.json),
not with hand-rolled XDR parsing:

```js
import { scValToNative, xdr } from "@stellar/stellar-sdk";

function decodeEvent(event) {
  const topics = event.topic.map((t) =>
    scValToNative(xdr.ScVal.fromXDR(t, "base64")),
  );
  const value = scValToNative(xdr.ScVal.fromXDR(event.value, "base64"));
  return { name: topics[0], streamId: topics[1], parties: topics.slice(2), value };
}
```

The generated client exposes the same decoders used by the contract tests, so
the SDK, the Rust contracts, and the GraphQL/SQL projections stay in lockstep.
Any change to a topic position or payload field is a breaking ABI change and
must be accompanied by updated fixtures.

### Validating parity with the snapshot fixtures

The fixtures under
[`tests/fixtures/event_snapshots/`](../tests/fixtures/event_snapshots/) are
golden JSON snapshots of decoded events and the resulting `Stream` mirror.
Indexers should run the same checks in CI:

- `script/verify_event_bytes.py` asserts that each fixture's encoded event
  stays within the 16 KiB Soroban event byte budget and that the topic/payload
  layout matches the ABI.
- `script/check_snapshot_diff.py` re-folds the fixture event stream and diffs
  the resulting `Stream` mirror against the golden snapshot, failing on any
  drift.

A projection is in parity only when both scripts pass. Treat a snapshot diff
as a schema change: update the ABI, the fixtures, and this guide together, and
never regenerate a fixture to hide an unexplained diff.
