# Governance Gas Analysis

Scope: `contracts/governance/src/lib.rs` (`FluxoraGovernance`) — the cost of
`propose`, `approve` and `execute`, and the event volume of a multi-signer
voting round.

Every number in this document is produced by
[`script/governance-gas-model.py`](../script/governance-gas-model.py). Run it
to regenerate the tables:

```bash
python3 script/governance-gas-model.py
```

`script/validate-doc-alignment.py` verifies this document against the model:
it checks that the operation-count and event-byte tables above stay in lockstep
with the values the model computes, and that the model's public docstrings
match the terminology used here. It must pass with zero warnings.

## Usage

Run the profiler with no arguments to print the human-readable report
(operation counts, the 10-signer round, and the event-byte tables):

```bash
python3 script/governance-gas-model.py
```

Pass `--json` to emit the same data as machine-readable JSON, suitable for
piping into other tooling or for diffing against a checked-in baseline:

```bash
python3 script/governance-gas-model.py --json
```

Both modes exit `0` on success. The JSON output is the stable interface; the
text report is for humans and may be reflowed.

## Troubleshooting

The profiler uses a small, fixed set of exit codes so it can be wired into CI:

| exit code | meaning | what to do |
|---:|---|---|
| `0` | Success. The model parsed the contract source and all structural checks passed. | Nothing. |
| `1` | A structural check failed — a per-vote cost guarantee from §3 regressed (e.g. `is_registered_signer` no longer reaches `SignerIndex`, or the quorum branch became unconditional). | Read the failing check's message; it names the helper and the expected call. Fix the contract, not the model. |
| `2` | A `ParseError` was raised while reading the contract source. | See the `ParseError` cases below. |

`ParseError` is raised when the model cannot locate a construct it depends on
in `contracts/governance/src/lib.rs`. The cases are:

* **Missing entrypoint.** One of `propose`, `approve` or `execute` was not
  found, or its signature no longer matches the expected shape.
* **Missing helper.** A free helper function the call graph resolves through
  (for example `is_registered_signer` or `get_signer_index`) was renamed or
  removed.
* **Unbalanced braces.** The brace-matching used to locate the enclosing block
  of the `QuorumReachedAt` write or the `proposal_queued` emit failed, which
  usually means the source was edited into a shape the matcher does not
  understand.

A `ParseError` is not a bug in the contract — it means the model and the
source have drifted apart. Update the model's expectations in the same change
that moves the source.

## What this is, and what it is not
=======

Issue #54 asks for **measured CPU instruction counts**. This document does
**not** contain them, and does not estimate them.

Instruction counts come from `soroban-env-host`'s metering, which only exists
when the contract is actually executed on a host. No such run was possible
here: the machine this was authored on has no usable Rust toolchain (no MSVC
linker, so even `cargo check` cannot build a build-script), and
`contracts/governance` does not produce a wasm target without one. Quoting a
number that was not measured would be worse than quoting none.

What *is* delivered is the part of the question that can be answered from
source, and answered exactly:

1. **Operation counts** — storage reads, storage writes, TTL bumps, ledger
   reads and event emissions on each path. These are what actually determine
   whether a call fits inside a ledger's limits, they are deterministic, and
   they can be regression-tested.
2. **Event byte sizes** for the events a voting round emits, computed from the
   `SCVal` definitions in `Stellar-contract.x`.
3. **Structural guarantees** that keep per-vote cost independent of
   deployment configuration, asserted by a check that fails if they regress.

A `cargo test` benchmark reporting raw instruction counts is still required
before any per-vote figure can be quoted. §6 says exactly what it must do.

## 1. Operation counts per entrypoint

Derived by resolving each entrypoint's call graph over the contract's free
helper functions and counting the leaf storage operations.

| entrypoint | reads | writes | total ops |
|---|---:|---:|---:|
| `propose` | 1 | 5 | 6 |
| `approve` | 5 | 10 | 15 |
| `execute` | 6 | 8 | 14 |

Breakdown:

| entrypoint | operations |
|---|---|
| `propose` | `instance_write` 1, `instance_bump` 1, `persistent_write` 1, `persistent_bump` 1, `ledger_read` 1, `event_emit` 1 |
| `approve` | `persistent_read` 1, `persistent_write` 3, `persistent_bump` 4, `persistent_exists` 2, `instance_read` 1, `instance_bump` 1, `ledger_read` 2, `event_emit` 2 |
| `execute` | `persistent_read` 1, `persistent_write` 1, `persistent_bump` 3, `persistent_exists` 2, `instance_read` 1, `instance_write` 2, `instance_bump` 1, `ledger_read` 3, `event_emit` 1 |

Two notes on reading this table:

* **`approve` reports `event_emit` 2, but a steady-state vote emits one.** The
  model cannot evaluate `if approval_count == threshold`, so it counts both
  `vote_cast` and `proposal_queued`. Only the vote that reaches threshold pays
  for the second. This is checked structurally — see §3.
* **`execute` is a floor, not the whole transaction.** The dispatched target
  contract's own work is metered separately and is not modelled here.

## 2. A 10-signer round

The scenario issue #54 names: 10 signers, threshold 10.

```text
propose                 6 ops
10 x approve          150 ops   (15 each)
execute                14 ops
                     -------
total                170 ops
```

Every vote costs the same 15 operations. There is no per-signer
multiplication inside a vote, which is the property that matters — see §3.

## 3. Why per-vote cost does not grow with the signer count

This is the load-bearing claim, and it is enforced rather than asserted.
`script/governance-gas-model.py` checks it against the source and fails if it
regresses.

**Membership is a Map lookup, not a scan.** `approve` calls
`is_registered_signer`, which reads the `SignerIndex` Map through
`get_signer_index` and does a `contains_key`. It never iterates the `Signers`
vec. A linear scan would make each vote O(signer count), so the 1M-per-vote
budget would become a function of how the contract happened to be deployed
rather than a fixed property of it. The check fails if `is_registered_signer`
stops reaching `SignerIndex` or stops using `contains_key`.

**Duplicate detection is a Map lookup, not a scan.** `approve` consults the
per-proposal `ProposalApprovalIdx` map, again via `contains_key`. Scanning
`proposal.approvals` instead would make the *n*-th vote O(n): cheap for the
first signer, progressively more expensive for the tenth, and worst exactly
where a multi-sig round is largest. The check fails if the index is dropped or
if the approvals vec is iterated.

**The quorum branch stays conditional.** Only the vote that reaches threshold
writes `QuorumReachedAt` and emits `proposal_queued`. If that work became
unconditional, all 10 votes would pay for one. The check locates the
`QuorumReachedAt` write and the `proposal_queued` emit by brace-matching
backwards to the enclosing block, and fails if either is no longer inside an
`if`.

**Consequence:** a vote is 15 operations whether the contract has 3 signers or
20. `MAX_SIGNERS = 20` is enforced separately in `init` and `add_signer`.

## 4. Event bytes for a voting round

Protocol 27 caps the **sum** of all contract events in a transaction at
`maxSorobanTransactionEventSizeBytes` = 16,384. Governance emits one
`vote_cast` per signer, so this is the limit a multi-signer round actually
pushes against.

| event | encoded bytes |
|---|---:|
| `vote_cast` | 284 |
| `proposal_queued` | 232 |

| round | votes | queue | total | % of 16,384 |
|---|---:|---:|---:|---:|
| 10 signers | 2,840 | 232 | **3,072** | 18.8% |
| 20 signers (`MAX_SIGNERS`) | 5,680 | 232 | **5,912** | 36.1% |

**A 10-signer round uses under a fifth of the event budget**, and even the
maximum permitted signer set stays at about a third. Event volume is not a
constraint on this contract.

Sizes come from the `SCVal` union in `Stellar-contract.x`: `SCSymbol` is a
`string<32>`; `SCV_U32`/`SCV_U64`/`SCV_I128` are fixed-width; `SCV_ADDRESS` is
a 4-byte discriminant plus a 4-byte `SCAddressType` plus a 32-byte hash;
`SCV_VEC` and `SCV_MAP` are declared as **pointers**, so each carries a 4-byte
pointer in addition to its discriminant; every body is XDR padded to a
multiple of 4.

`vote_cast` publishes topics `(vote_cast, proposal_id, approver)` — a symbol,
a `u32` and an `Address`. Topic types are not uniform, and sizing them all as
symbols is a real error mode: an early version of this model did exactly that
and understated `vote_cast` by 4 bytes while overstating `proposal_queued` by
20.

**These are analytical, not measured.** They model the published encoding and
exclude any per-event accounting the host adds outside the `ContractEvent`
struct. `script/verify_governance_event_bytes.py` recomputes both figures by
hand from the XDR definitions without importing the model, so the numbers are
not resting on one implementation; the two agree on 284 and 232.


## 5. What is not covered

* **CPU instruction counts.** See the header and §6.
* **The dispatched target's cost.** `execute` calls into another contract via
  `dispatch_call`. That contract's work is metered to the same transaction
  budget and is not modelled here.
* **Wasm vs native.** Even once a host run is possible, a `cargo test`
  benchmark registers contracts natively, and native instruction counts differ
  from release-wasm ones. The existing factory benchmark
  ([gas.md](gas.md)) makes the same caveat.
* **Rent.** Proposal and approval-index entries are TTL-bumped on every
  access. Rent depends on entry size and lifetime, neither of which is
  modelled.

## 6. What the missing benchmark needs to do

To close issue #54's acceptance criteria, a `cargo test` benchmark in
`contracts/governance/tests/` should:

1. `init` with 10 signers and threshold 10.
2. `propose` once, then `approve` 10 times, measuring each call's instruction
   count via the host's budget tracker.
3. Report **per-vote** instructions, not just the round total, and assert the
   issue's 1,000,000 budget.
4. Record the soroban-sdk and soroban-env-host versions and the measurement
   date, as [gas.md](gas.md) does for the factory.
5. Report event bytes alongside instructions and check them against §4's
   figures. A disagreement means the encoding model above is wrong and should
   be fixed here first.

The static model in this repository is the independent check on that benchmark.
If the benchmark ever reports a per-vote cost implying O(n) behaviour in the
signer count, §3's guarantees have been broken — and the model will say so
without the benchmark having to be interpreted.

## Acceptance criteria status

| Criterion | Status |
|---|---|
| Governance gas benchmarks published | **Partially.** Operation counts and event bytes published; CPU instruction counts not, as no host run was possible. |
| Multi-sig voting CPU instructions under 1M per vote | **Not verified.** The structural guarantee that a vote's cost is independent of signer count is verified (§3); the absolute instruction figure is not. |
| Resource usage validated against Protocol 27 limits | **Partially.** Event bytes checked against the 16,384-byte limit (§4). Instruction and rent budgets are not. |

