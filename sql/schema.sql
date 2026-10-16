-- Trade reconciliation schema.
-- Re-runnable: drops and recreates everything. All data in it is synthetic.

DROP TABLE IF EXISTS breaks, match_results, recon_runs, answer_key, broker_trades, internal_trades CASCADE;

-- Our firm's blotter. One row per trade; trade_id is trustworthy here.
CREATE TABLE internal_trades (
    trade_id      text PRIMARY KEY,
    trade_date    date          NOT NULL,
    instrument    text          NOT NULL,
    side          text          NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity      integer       NOT NULL CHECK (quantity > 0),
    price         numeric(18,4) NOT NULL CHECK (price > 0),
    currency      char(3)       NOT NULL,
    counterparty  text          NOT NULL,
    settle_date   date          NOT NULL
);
CREATE INDEX ix_internal_trade_date ON internal_trades (trade_date);

-- The broker's statements. A broker can re-send a record (AMEND) or withdraw it
-- (CANCEL) on a later statement, so each broker_ref can have several versions.
-- The latest version as of a statement date is the broker's current view.
-- trade_id is deliberately unreliable: it can be NULL or mistyped.
CREATE TABLE broker_trades (
    broker_ref      text          NOT NULL,
    version         integer       NOT NULL,
    statement_date  date          NOT NULL,
    record_status   text          NOT NULL CHECK (record_status IN ('NEW', 'AMEND', 'CANCEL')),
    trade_id        text,
    trade_date      date          NOT NULL,
    instrument      text          NOT NULL,
    side            text          NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity        integer       NOT NULL CHECK (quantity > 0),
    price           numeric(18,4) NOT NULL CHECK (price > 0),
    currency        char(3)       NOT NULL,
    settle_date     date          NOT NULL,
    PRIMARY KEY (broker_ref, version)
);
CREATE INDEX ix_broker_statement_date ON broker_trades (statement_date);
CREATE INDEX ix_broker_trade_id ON broker_trades (trade_id);

-- Every error the generator planted, so the detector can be scored.
-- The matcher never reads this table.
CREATE TABLE answer_key (
    plant_id                 serial PRIMARY KEY,
    plant_date               date NOT NULL,
    break_type               text NOT NULL,
    trade_id                 text,   -- true internal trade_id (NULL for missing_internally)
    broker_ref               text,   -- broker record that carries the error (NULL for missing_at_broker)
    field                    text,
    original_value           text,
    changed_value            text,
    planned_resolution_date  date    -- NULL = never fixed inside the 20-day window
);

-- One row per reconciliation run (one run per business day).
CREATE TABLE recon_runs (
    run_date            date PRIMARY KEY,
    tolerance           numeric NOT NULL,
    internal_count      integer NOT NULL,
    broker_count        integer NOT NULL,
    exact_matches       integer NOT NULL,
    fuzzy_matches       integer NOT NULL,
    partial_matches     integer NOT NULL,
    clean_matches       integer NOT NULL,
    unmatched_internal  integer NOT NULL,
    unmatched_broker    integer NOT NULL,
    duplicates_flagged  integer NOT NULL,
    new_breaks          integer NOT NULL,
    resolved_breaks     integer NOT NULL,
    open_breaks         integer NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now()
);

-- One row per matched internal/broker pair per run, and how it was matched.
CREATE TABLE match_results (
    run_date          date NOT NULL REFERENCES recon_runs ON DELETE CASCADE,
    trade_id          text NOT NULL,
    broker_ref        text NOT NULL,
    match_method      text NOT NULL CHECK (match_method IN ('exact', 'fuzzy', 'partial')),
    fields_differing  text[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_date, trade_id)
);

-- Daily snapshot of open breaks. A break keeps its break_id and first_seen_date
-- for as long as it stays open, so age = business days since first_seen_date.
CREATE TABLE breaks (
    run_date         date NOT NULL REFERENCES recon_runs ON DELETE CASCADE,
    break_id         text NOT NULL,
    break_key        text NOT NULL,
    break_type       text NOT NULL CHECK (break_type IN (
                         'missing_at_broker', 'missing_internally', 'quantity_mismatch',
                         'price_mismatch', 'side_mismatch', 'duplicate',
                         'settlement_date_mismatch')),
    severity         smallint NOT NULL CHECK (severity BETWEEN 1 AND 3),
    trade_id         text,
    broker_ref       text,
    related_ref      text,   -- for a duplicate: the record it duplicates
    field            text,
    internal_value   text,
    broker_value     text,
    detail           text NOT NULL,
    notional         numeric(20,2),
    first_seen_date  date NOT NULL,
    status           text NOT NULL CHECK (status IN ('NEW', 'OPEN')),
    suggested_owner  text NOT NULL,
    PRIMARY KEY (run_date, break_id)
);
CREATE INDEX ix_breaks_key ON breaks (break_key);
