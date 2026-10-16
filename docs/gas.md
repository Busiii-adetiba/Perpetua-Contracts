# Gas benchmarks

This file covers the **stream/factory** paths. Governance voting and execution
are analysed separately in
[governance-gas.md](governance-gas.md), which measures operation counts and
event bytes for `propose` / `approve` / `execute` and documents what a
multi-signer round costs.

## Factory gas benchmark

The factory benchmark compares three first-stream creation paths under the same Stellar Asset Contract, schedule, amount, and ledger state:

1. `direct`: `FluxoraStream::create_stream` without a wrapper.
2. `forwarding`: a contract that forwards the same arguments to `FluxoraStream` without factory policy checks.
3. `factory`: `FluxoraFactory::create_stream`, including the consolidated policy read, policy checks, and stream dispatch.

The forwarding path is the control for cross-contract dispatch. The issue's policy-overhead budget is measured as `factory - forwarding`, while `factory - direct` is also logged as total wrapper overhead.

## Measured result

Measured on 2026-09-24 with Rust 1.97.1, soroban-sdk 27.0.6, and soroban-env-host 27.0.1:

| Path | CPU instructions | Memory bytes | Memory reads | Writes | Write bytes | Event bytes |
|---|---:|---:|---:|---:|---:|---:|
| Direct | 276,074 | 42,583 | 8 | 5 | 1,228 | 684 |
| Forwarding control | 299,465 | 46,688 | 9 | 5 | 1,228 | 684 |
| Factory | 327,424 | 51,221 | 9 | 5 | 1,228 | 684 |

- Policy overhead: `327,424 - 299,465 = 27,959` instructions, or **9.34%** of forwarding stream creation.
- Total wrapper overhead: `327,424 - 276,074 = 51,350` instructions, or **18.60%** of direct creation. This includes the forwarding contract invocation itself and is reported separately rather than attributed to policy validation.
- The factory adds one in-memory policy read and no stream-ID vector read or write. Rate division and bounds checks are skipped when both optional rate bounds are unset.

## Reproduce

From `contracts/factory`:

```bash
cargo test --locked --test gas_benchmark -- --nocapture --test-threads=1
```

The benchmark registers all contracts natively. As with the existing resource-limit suite, this is a deterministic policy and storage regression test; Wasm execution costs are lower than production. A release-Wasm or testnet simulation run is still required before treating the absolute instruction counts as deployment forecasts.

## Soroban 16 KiB event byte footprint enforcer

`script/event-byte-budget.py` is the offline enforcer for the Protocol 27
Soroban transaction event-size bound. It models the byte footprint of the
events a stream lifecycle can emit and rejects any configuration that could
exceed the ledger limit before it is committed.

### Enforced bound

Protocol 27 caps the total serialized event footprint of a single Soroban
transaction at `maxSorobanTransactionEventSizeBytes = 16,384` bytes. The
enforcer treats this as a hard ceiling: a configuration whose modelled
footprint is greater than or equal to the bound is rejected, never truncated
and never silently wrapped.

### Per-event encoding model

Each emitted event is charged its full serialized size:

- a 4-byte topic count and a 4-byte value count,
- for every topic and value: a 4-byte length prefix plus the encoded payload,
- `symbol` payloads are charged their UTF-8 byte length and are limited to
  `MAX_SYMBOL_LEN` bytes,
- integer payloads are charged their fixed-width encoding (`i128` is 16 bytes),
- address payloads are charged `ADDRESS_BYTES`.

The model is deterministic: identical inputs always produce identical byte
counts, so the enforcer can be used as a regression oracle.

### Deterministic error variants

Degenerate or adversarial parameters are rejected early with a dedicated
error variant rather than being coerced:

| Variant | Rejected condition |
|---|---|
| `ZERO_DURATION` | `duration == 0`, which would make the rate cliff undefined |
| `I128_MAX_RATE` | deposit rate at `i128::MAX`, where accrual would overflow |
| `SAME_LEDGER_TRANSITION` | a state transition whose start and end ledgers are equal |
| `OVERFLOW` | any intermediate byte or arithmetic accumulation that would exceed its type |
| `SYMBOL_TOO_LONG` | a symbol payload longer than `MAX_SYMBOL_LEN` |
| `BATCH_TOO_LARGE` | a batch whose modelled footprint exceeds `MAX_BATCH_SIZE * withdrawn` |

### Preconditions

Invariant checks run **before** any state commit or event emission. The
enforcer validates the full parameter set, the per-event encoding, and the
aggregate batch footprint first; only if every check passes is the state
transition considered committable. No partial state mutation is observable
when a check fails.

### Batch ceiling

For batched withdrawals the aggregate footprint is bounded by
`MAX_BATCH_SIZE * withdrawn`: the maximum number of withdrawals in a batch
multiplied by the per-withdrawal event footprint. A batch that would exceed
this ceiling is rejected with `BATCH_TOO_LARGE`.

### Regression coverage

`tests/test_event_byte_budget.py` pins these invariants with unit and
property-based tests: boundary values at exactly the 16,384-byte limit,
zero-duration cliffs, `i128::MAX` rates, same-ledger transitions, oversized
symbols, and oversized batches. The suite must pass in CI without panics or
unexpected state mutations.
