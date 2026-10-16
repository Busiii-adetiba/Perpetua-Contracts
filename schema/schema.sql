-- Perpetua indexer schema — PostgreSQL.
--
-- Two concerns:
--   * `events` is the append-only raw ledger of stream events (source of
--     truth, replayable, feeds the GraphQL layer / SubQuery project).
--   * `streams` is the current-state mirror, rebuilt by folding `events`
--     forward; a keeper reads `ttl_extended_at` and `status` straight off it.
--
-- Mirrors contracts/stream/src/events.rs and docs/ABI.md. Amounts are i128,
-- times are u64 (unix seconds), both stored as NUMERIC(39). Ledger sequence
-- is u32. On chain, stream_id is u64 — keep it BIGINT here so ids never
-- collide.
--
-- Frozen topic0 values (snake_case, one per emitted event):
--   'created'        — StreamCreated
--   'withdrawn'      — Withdrawn
--   'cancelled'      — Cancelled
--   'paused'         — Paused
--   'unpaused'       — Unpaused
--   'transferred'    — StreamTransferred
--   'ttl_extended'   — TtlExtended
--
-- Frozen payload JSONB keys per topic0 (must match docs/ABI.md exactly):
--   created      : { sender, recipient, token, deposited, start_time,
--                    end_time, cliff_time, cancellable, pausable,
--                    transferable }
--   withdrawn    : { recipient, amount, withdrawn }
--   cancelled    : { recipient, refunded, withdrawn }
--   paused       : { recipient, paused_at }
--   unpaused     : { recipient, paused_total }
--   transferred  : { from, to }
--   ttl_extended : { extended_to }

CREATE TABLE IF NOT EXISTS events (
    id              BIGSERIAL PRIMARY KEY,
    ledger_seq      INTEGER   NOT NULL,
    tx_hash         TEXT      NOT NULL,
    topic0          TEXT      NOT NULL CHECK (topic0 IN (
                        'created', 'withdrawn', 'cancelled', 'paused',
                        'unpaused', 'transferred', 'ttl_extended')),
    stream_id       BIGINT    NOT NULL,
    payload         JSONB     NOT NULL,             -- full decoded event data
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_events_stream_id      ON events (stream_id, ledger_seq DESC);
CREATE INDEX IF NOT EXISTS idx_events_topic0_ledger  ON events (topic0, ledger_seq DESC);
CREATE INDEX IF NOT EXISTS idx_events_ledger         ON events (ledger_seq DESC);

-- Current-state mirror. Columns mirror the `Stream` struct fields in
-- contracts/stream/src/lib.rs (frozen ABI in docs/ABI.md).
CREATE TABLE IF NOT EXISTS streams (
    stream_id       BIGINT PRIMARY KEY,
    sender          TEXT      NOT NULL,             -- G…/C… address
    recipient       TEXT      NOT NULL,
    token           TEXT      NOT NULL,
    deposited       NUMERIC(39) NOT NULL,
    withdrawn       NUMERIC(39) NOT NULL,
    start_time      BIGINT    NOT NULL,
    end_time        BIGINT    NOT NULL,
    cliff_time      BIGINT    NOT NULL,
    cancellable     BOOLEAN   NOT NULL,
    pausable        BOOLEAN   NOT NULL,
    transferable    BOOLEAN   NOT NULL,
    paused_at       BIGINT,
    paused_total    BIGINT    NOT NULL DEFAULT 0,
    status          TEXT      NOT NULL CHECK (status IN (
                        'Active', 'Paused', 'Cancelled', 'Depleted')),
    created_ledger  INTEGER   NOT NULL,
    updated_ledger  INTEGER   NOT NULL,
    ttl_extended_at INTEGER   NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_streams_sender    ON streams (sender);
CREATE INDEX IF NOT EXISTS idx_streams_recipient ON streams (recipient);
CREATE INDEX IF NOT EXISTS idx_streams_status    ON streams (status);
CREATE INDEX IF NOT EXISTS idx_streams_token     ON streams (token);

-- Materialized activity for dashboards / queries without replay.
-- Extracts the frozen `withdrawn` payload keys: { recipient, amount, withdrawn }.
CREATE MATERIALIZED VIEW IF NOT EXISTS withdrawals AS
SELECT stream_id,
       payload->>'recipient'                                     AS recipient,
       (payload->>'amount')::NUMERIC                             AS amount,
       (payload->>'withdrawn')::NUMERIC                          AS withdrawn,
       ledger_seq, tx_hash
FROM events
WHERE topic0 = 'withdrawn'
ORDER BY ledger_seq DESC;

CREATE UNIQUE INDEX IF NOT EXISTS idx_withdrawals_ledger_tx
    ON withdrawals (ledger_seq, tx_hash);