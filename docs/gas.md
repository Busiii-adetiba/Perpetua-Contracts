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

`script/event-byte-budget.py` enforces the Protocol 27 per-transaction event
byte bound for batch payroll runs. The host caps a transaction's total event
footprint at `maxSorobanTransactionEventSizeBytes = 16,384` bytes (16 KiB);
exceeding it aborts the transaction, so the enforcer rejects any batch whose
worst-case event bytes would cross that ceiling before the batch is submitted.

### Per-event sizes

Each emitted event is measured as its XDR-encoded footprint (topics plus body):

| Event | Bytes |
|---|---:|
| `withdrawn` | 260 |
| `stream_created` | largest single event |

`withdrawn` is the hot path for batch payroll: one event is emitted per
withdrawal, so it dominates the batch footprint. `script/verify_event_bytes.py`
independently re-derives the `withdrawn` figure from the contract's event
definition and cross-checks it against the value used by the enforcer, so a
schema change that silently grows the event is caught by two separate paths.

### Batch ceiling and headroom

The enforcer caps a batch at `MAX_BATCH_SIZE = 16` withdrawals:

- Worst-case batch footprint: `16 x 260 = 4,160` bytes.
- Budget consumed: `4,160 / 16,384 = 25.4%`.
- Headroom: `16,384 - 4,160 = 12,224` bytes, i.e. **74.6%** of the budget
  remains available for the transaction's other events and host overhead.

The bound is a strict upper cap: the enforcer never admits a batch that could
exceed 16 KiB, regardless of how many events the surrounding transaction emits.

### Regression guard

`tests/test_event_byte_budget.py` asserts the enforcer's behaviour so that
instruction-count and byte-footprint regressions fail CI rather than surfacing
as aborted mainnet transactions:

- The per-event `withdrawn` size stays at 260 bytes.
- `MAX_BATCH_SIZE` stays at 16 and the worst-case batch footprint stays at
  4,160 bytes (25.4% of the 16,384-byte budget).
- A batch at the maximum size is accepted; a batch one event over the ceiling
  is rejected.
- The enforcer's `withdrawn` figure matches the value independently derived by
  `script/verify_event_bytes.py`.

The WASM binary size budget check (`script/check-stream-wasm-size.sh`) is
unaffected by these changes and must remain green.
