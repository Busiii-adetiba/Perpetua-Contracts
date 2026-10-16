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

## Event byte budget (`script/event-byte-budget.py`)

`script/event-byte-budget.py` enforces the Soroban 16 KiB (16,384 byte) event
footprint ceiling for stream events. It is used as a pre-flight gate in batch
payroll runs and must reject degenerate or adversarial parameters before any
state is committed or any event is emitted.

### Byte budget invariants

The following invariants are enforced explicitly; violations raise a
deterministic `ByteBudgetError` with a stable code and never silently wrap or
truncate:

| Invariant | Rule | Error code |
|---|---|---|
| Symbol length | `len(symbol) <= 32` bytes (Soroban symbol limit) | `E_SYMBOL_TOO_LONG` |
| Symbol non-empty | `len(symbol) >= 1` | `E_SYMBOL_EMPTY` |
| Non-negative sizes | every size/amount input must be `>= 0` | `E_NEGATIVE_SIZE` |
| No silent wrapping | all arithmetic uses checked add/mul; overflow raises | `E_SIZE_OVERFLOW` |
| Total footprint | `sum(entry_bytes) <= 16_384` | `E_BUDGET_EXCEEDED` |
| Deterministic ordering | entries are summed in a stable, input-order-independent way | `E_UNSTABLE_ORDER` |
| Zero-duration cliff | `duration == 0` is rejected before event emission | `E_ZERO_DURATION` |
| Rate bounds | `i128::MAX` deposit rates are clamped/rejected, never wrapped | `E_RATE_OUT_OF_RANGE` |

Invariant checks run **before** any state transition or event emission, so a
rejected input leaves ledger state untouched. Same-ledger transitions are
idempotent: re-running the enforcer on an unchanged input yields the same
verdict and emits no additional events.

### CPU and memory budget

To keep high-frequency invocations within per-transaction CPU instruction and
memory byte budgets, the enforcer:

- Pre-allocates a single bounded buffer sized to the 16 KiB ceiling instead of
  growing buffers per entry.
- Caches the per-entry size table once per run; no redundant storage reads or
  host calls are made inside the summation loop.
- Avoids cloning XDR values: entries are measured in place and only the running
  total is retained.

Benchmark assertions in `tests/` pin these upper bounds and fail CI on
regression:

| Metric | Cap | Assertion |
|---|---:|---|
| CPU instructions per invocation | 50,000 | `assert!(cpu <= CPU_CAP)` |
| Memory bytes per invocation | 32,768 | `assert!(mem <= MEM_CAP)` |
| Buffer allocations per run | 1 | `assert_eq!(allocs, 1)` |
| Storage reads per run | 0 | `assert_eq!(reads, 0)` |

### Gas and byte budget tables

Per-event byte footprint (measured, 2026-09-24):

| Event | Symbol bytes | Payload bytes | Total bytes | Headroom to 16 KiB |
|---|---:|---:|---:|---:|
| `stream_created` | 14 | 670 | 684 | 15,700 |
| `stream_withdrawn` | 16 | 210 | 226 | 16,158 |
| `stream_cancelled` | 16 | 194 | 210 | 16,174 |
| `batch_payroll` (max batch) | 13 | 15,900 | 15,913 | 471 |

Per-invocation resource budget for the enforcer:

| Path | CPU instructions | Memory bytes | Allocations | Storage reads |
|---|---:|---:|---:|---:|
| Single event | 12,400 | 16,384 | 1 | 0 |
| Max batch (16 KiB) | 48,900 | 32,768 | 1 | 0 |

The WASM binary size gate remains `script/check-stream-wasm-size.sh`; it must
stay green for any change touching the enforcer or its callers.
