# Security, Audit Scope and Bug Bounty (§101)

Preparing Perpetua for mainnet means being explicit about what has been
reviewed, who may review the rest, and what a reviewer is rewarded for finding.

This document is the scope **of record**. It answers:

1. what counts as in-scope for a security review,
2. how findings are reported,
3. how in-scope findings are rewarded.

## 1. Audit readiness

The repository holds the artifacts a third-party auditor needs, so a review
starts from buildable, pinned, reproducible inputs:

* **Pinned toolchain and target** — `rust-toolchain.toml` pins Rust `1.97.1`
  and the `wasm32v1-none` WASM target; the contract refuses to build for
  `wasm32-unknown-unknown` or with debug assertions (`lib.rs` guards).
* **Reproducible build** — `script/release.sh` and `script/provenance.sh`
  produce and verify a SLSA-style wasm manifest (`docs/provenance.md`).
* **Interface of record** — `docs/ABI.md` and `contracts/stream/abi/fluxora_stream.json`
  pin the public ABI; `docs/audit.md` is the drift-checked entrypoint table.
* **Invariant suite** — the 36 test modules (≈570 tests) assert the I1–I5
  invariants after every operation, over random schedules (`test::props`) and
  across every entry-point ordering (`test::monotonicity`). The event byte
  footprint enforcer invariants (boundary guards, deterministic rejection of
  degenerate parameters, zero silent wrapping) are pinned by
  `tests/test_event_byte_budget.py`.
* **Known-limits register** — `docs/KNOWN-LIMITATIONS.md` records what a green
  suite does *not* prove, including the residual limits of the event byte
  footprint enforcer's boundary checks. Reviewers are asked to read it before
  anything else.

**Status:** no third-party audit has been completed. Do not claim otherwise.
This document lowers the cost of the first one.

## 2. Audit scope

### In scope

* `contracts/stream/` — the entire contract: `src/accrual.rs` (arithmetic),
  `src/lib.rs` (all entry points), `src/storage.rs` (TTL and storage access),
  `src/types.rs`, `src/error.rs`, `src/events.rs`.
* Fund-safety properties: the I1–I5 invariants in `contracts/stream/src/accrual.rs`
  and the pool invariant `liability <= contract balance`.
* The off-chain math mirror `sdk/stream-math.js` (must never disagree with
  `accrual.rs`).
* The Soroban 16 KiB event byte footprint enforcer `script/event-byte-budget.py`
  and its boundary/adversarial guarantees: degenerate parameters (zero-duration
  cliffs, `i128::MAX` deposit rates, same-ledger state transitions) are rejected
  deterministically with explicit error variants before any state is committed
  or any event is emitted, and no integer arithmetic silently wraps. These
  invariants are asserted by `tests/test_event_byte_budget.py` (unit and
  property-based regression tests) and are part of the invariant suite below.
* Build/provenance tooling in `script/` and `tools/provenance/` (only insofar
  as a defect could ship a wrong wasm).

### Out of scope

* `contracts/factory/`, `contracts/governance/` — separate products for their
  own review.
* `contracts/archival-probe/` — a throwaway probe, never deployed as product.
* Deploy/infra configuration, key custody, and any frontend.
* Known limits already disclosed in `docs/KNOWN-LIMITATIONS.md`.

### Readiness gate — what the out-of-scope reviewers must accept

A reviewer accepts this scope by confirming:

1. the release build reproduces byte-for-byte from a fresh checkout
   (`script/release-dry-run.sh`), and
2. the invariant suite passes at the pinned toolchain, and
3. ambition of the ABI snapshot (`docs/audit.md`) matches `src/lib.rs`.

## 3. Reporting

* **Private:** email the maintainers (addresses in the GitHub org). Include
  reproduction, expected vs actual, and impact.
* **On-chain:** an exploit attempt that provably drains or locks funds is
  disclosed privately first; there is no public-fine process that worries about
  front-running.
* **Public:** anything that is not a fund-safety issue (UI, docs, gas) can be
  filed straight as a GitHub issue.

Every report is acknowledged within 7 days and answered with a fix plan or a
reasoned non-issue within 30.

## 4. Bug bounty

* **Severity scale** — Critical (guards broken, funds at risk), High
  (funds stuck or commutative accounting corruption), Medium (invariant
  violation without fund loss), Low (gas, UX, informational) — aligned with the
  settlement model below.
* **Rewards** — reserved for a campaign the maintainers open before mainnet
  launch; the amounts and token denom are set in that announcement. Until then
  this section is a contract, not a funded pool:
  * Critical — top reward tier, awarded at the discretion of the maintainers
    after a verified impact.
  * High/Medium — tiered, only in an open campaign.
  * Low/informational — acknowledgements in `docs/accredited.md` once created.
* **Eligibility** — any original finding; excluded are findings already
  disclosed in `docs/KNOWN-LIMITATIONS.md`, `docs/audit.md`, this file, or
  developer-found during a scheduled audit. Self-research against the deployed
  testnet contract is fine; a live mainnet exploit attempt to *demonstrate* a
  bounty finding is not eligible and is a crime on most chains.
* **Disclosure** — bounty-worthy findings are disclosed after a fix ships or
  the maintainers grant a 90-day embargo.

## 5. References

* `docs/audit.md` — entrypoint inventory + arithmetic audit (§98).
* `docs/dust-theft-proof.md` — why micro-top-ups cannot drain the pool (§102).
* `docs/KNOWN-LIMITATIONS.md` — what a green suite does not prove, including the
  event byte footprint enforcer's boundary guarantees.
* `docs/ABI.md` — the interface of record.
* `script/event-byte-budget.py` — the Soroban 16 KiB event byte footprint
  enforcer; invariants asserted by `tests/test_event_byte_budget.py`.