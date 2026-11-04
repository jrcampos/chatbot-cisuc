-- External-sources store: core schema (Postgres 14+).
--
-- Model: every load of a source is a SNAPSHOT. Rows of a snapshot are written while it is in
-- `staging` (invisible to readers), validated, then published by moving one pointer
-- (`source_state.published_snapshot_id`) inside a transaction: an atomic full replace per
-- source. The previous snapshot stays intact, so a rejected or failed load never leaves the
-- chatbot with empty or half-loaded data, and rollback is a pointer move.
-- Provenance (`field_value`) is append-only per snapshot: every value from every layer is kept,
-- including values that disagree; `resolved_value` holds the chosen value, its status and the
-- min..max range over all contributing values.

CREATE TABLE extsrc.source (
    name         text PRIMARY KEY,
    kind         text NOT NULL DEFAULT 'api',
    description  text,
    base_url     text,
    licence      text,
    attribution  text,
    enabled      boolean NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE extsrc.run (
    run_id       uuid PRIMARY KEY,
    started_at   timestamptz NOT NULL,
    finished_at  timestamptz,
    status       text NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running', 'succeeded', 'partial', 'failed', 'interrupted')),
    trigger      text,
    host         text,
    pid          integer,
    options      jsonb NOT NULL DEFAULT '{}',
    summary      jsonb NOT NULL DEFAULT '{}',
    error        text
);

CREATE TABLE extsrc.snapshot (
    snapshot_id      bigserial PRIMARY KEY,
    source           text NOT NULL REFERENCES extsrc.source (name),
    run_id           uuid NOT NULL,
    batch_sha256     text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    fetched_at       timestamptz NOT NULL,
    status           text NOT NULL DEFAULT 'staging'
                     CHECK (status IN ('staging', 'published', 'superseded', 'rejected')),
    partial          boolean NOT NULL DEFAULT false,
    partial_reasons  jsonb NOT NULL DEFAULT '[]',
    record_count     integer NOT NULL DEFAULT 0,
    fetched_count    integer NOT NULL DEFAULT 0,
    carried_count    integer NOT NULL DEFAULT 0,
    validation       jsonb,
    published_at     timestamptz,
    UNIQUE (source, batch_sha256)
);
CREATE INDEX snapshot_source_status_idx ON extsrc.snapshot (source, status, snapshot_id DESC);

CREATE TABLE extsrc.source_run (
    source_run_id    bigserial PRIMARY KEY,
    run_id           uuid NOT NULL REFERENCES extsrc.run (run_id) ON DELETE CASCADE,
    source           text NOT NULL REFERENCES extsrc.source (name),
    started_at       timestamptz NOT NULL,
    finished_at      timestamptz,
    status           text NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running', 'published', 'partial', 'rejected', 'failed',
                                       'skipped', 'interrupted', 'already_loaded', 'obsolete')),
    records_fetched  integer,
    records_loaded   integer,
    records_carried  integer,
    records_failed   integer,
    documents_total  integer,
    documents_failed integer,
    partial_reasons  jsonb NOT NULL DEFAULT '[]',
    errors           jsonb NOT NULL DEFAULT '[]',
    validation       jsonb,
    snapshot_id      bigint REFERENCES extsrc.snapshot (snapshot_id) ON DELETE SET NULL,
    stats            jsonb NOT NULL DEFAULT '{}',
    UNIQUE (run_id, source)
);

CREATE TABLE extsrc.source_state (
    source                 text PRIMARY KEY REFERENCES extsrc.source (name),
    published_snapshot_id  bigint REFERENCES extsrc.snapshot (snapshot_id),
    previous_snapshot_id   bigint REFERENCES extsrc.snapshot (snapshot_id),
    last_attempt_at        timestamptz,
    last_success_at        timestamptz,
    last_status            text,
    consecutive_failures   integer NOT NULL DEFAULT 0,
    stale_since            timestamptz,
    stale_reason           text,
    updated_at             timestamptz NOT NULL DEFAULT now()
);

-- Reference data (stable identities across snapshots).
CREATE TABLE extsrc.programme (
    programme_key  text PRIMARY KEY,          -- F&T frameworkProgramme id, or a label slug
    label          text NOT NULL,
    sources        text[] NOT NULL DEFAULT '{}',
    first_seen     timestamptz NOT NULL DEFAULT now(),
    last_seen      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE extsrc.call (
    source           text NOT NULL REFERENCES extsrc.source (name),
    call_identifier  text NOT NULL,
    call_title       text,
    programme_key    text REFERENCES extsrc.programme (programme_key),
    first_seen       timestamptz NOT NULL DEFAULT now(),
    last_seen        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, call_identifier)
);

-- Untouched source payloads, stored once per content hash.
CREATE TABLE extsrc.payload (
    sha256      text PRIMARY KEY,
    body        jsonb NOT NULL,
    first_seen  timestamptz NOT NULL DEFAULT now()
);

-- One row per topic / call per snapshot (the NormalizedRecord).
CREATE TABLE extsrc.external_record (
    snapshot_id        bigint NOT NULL REFERENCES extsrc.snapshot (snapshot_id) ON DELETE CASCADE,
    source             text NOT NULL,
    natural_key        text NOT NULL,
    record_type        text NOT NULL,
    record_kind        text NOT NULL,          -- topic | cascade_call | external_action_call | ...
    domain             text NOT NULL,
    programme          text,
    programme_key      text,
    call_identifier    text,
    call_title         text,
    country_scope      text,
    title              text NOT NULL,
    description        text,
    status             text,
    funding_amount     numeric,
    currency           text NOT NULL DEFAULT 'EUR',
    start_date         timestamptz,
    deadline_date      timestamptz,
    deadline_dates     timestamptz[],
    source_url         text NOT NULL,
    source_updated_at  timestamptz,
    details            jsonb NOT NULL DEFAULT '{}',
    field_sources      jsonb NOT NULL DEFAULT '{}',
    data_gaps          jsonb NOT NULL DEFAULT '[]',
    raw_sha256         text REFERENCES extsrc.payload (sha256),
    content_hash       text NOT NULL,
    fetched_at         timestamptz NOT NULL,
    change_state       text,
    carried_over       boolean NOT NULL DEFAULT false,
    absent_runs        integer NOT NULL DEFAULT 0,
    search_extra       text,
    embed_text_sha256  text,
    search_tsv         tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(natural_key, '') || ' ' || coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(call_title, '') || ' ' || coalesce(programme, '') || ' '
                                          || coalesce(search_extra, '')), 'B') ||
        setweight(to_tsvector('english', left(coalesce(description, ''), 200000)), 'C')
    ) STORED,
    PRIMARY KEY (snapshot_id, natural_key)
);
CREATE INDEX external_record_tsv_idx ON extsrc.external_record USING gin (search_tsv);
CREATE INDEX external_record_key_idx ON extsrc.external_record (source, natural_key);
CREATE INDEX external_record_deadline_idx ON extsrc.external_record (snapshot_id, deadline_date);
CREATE INDEX external_record_programme_idx ON extsrc.external_record (snapshot_id, programme_key);
CREATE INDEX external_record_call_idx ON extsrc.external_record (call_identifier);

-- Provenance: one row per field per source/layer (and per alternative value). Append-only.
CREATE TABLE extsrc.field_value (
    id              bigserial PRIMARY KEY,
    snapshot_id     bigint NOT NULL,
    natural_key     text NOT NULL,
    field           text NOT NULL,
    layer           text NOT NULL CHECK (layer IN ('api', 'detail', 'document', 'other')),
    role            text NOT NULL DEFAULT 'value'
                    CHECK (role IN ('value', 'alternative', 'corroboration', 'superseded', 'check')),
    value           jsonb,                 -- NULL for long texts: the full text is in external_record
    value_text      text,
    value_num       numeric,
    value_ts        timestamptz,
    source_url      text,
    doc_id          text,
    page            integer,
    section         text,
    evidence_quote  text,
    retrieved_at    timestamptz,
    published_at    timestamptz,
    content_hash    text,
    confidence      real,
    note            text,
    FOREIGN KEY (snapshot_id, natural_key)
        REFERENCES extsrc.external_record (snapshot_id, natural_key) ON DELETE CASCADE
);
CREATE INDEX field_value_lookup_idx ON extsrc.field_value (snapshot_id, natural_key, field);

-- The value each field resolves to, with its status and the range over every contributing value.
CREATE TABLE extsrc.resolved_value (
    snapshot_id    bigint NOT NULL,
    natural_key    text NOT NULL,
    field          text NOT NULL,
    value          jsonb,
    value_num      numeric,
    value_ts       timestamptz,
    status         text NOT NULL
                   CHECK (status IN ('confirmed', 'single_source', 'conflicting', 'resolved', 'absent')),
    value_min_num  numeric,
    value_max_num  numeric,
    value_min_ts   timestamptz,
    value_max_ts   timestamptz,
    settled        boolean,
    agree_count    integer,
    basis          text,
    contributing   jsonb NOT NULL DEFAULT '[]',
    sources        jsonb NOT NULL DEFAULT '[]',
    checked_at     timestamptz,
    PRIMARY KEY (snapshot_id, natural_key, field),
    FOREIGN KEY (snapshot_id, natural_key)
        REFERENCES extsrc.external_record (snapshot_id, natural_key) ON DELETE CASCADE
);
CREATE INDEX resolved_value_num_idx ON extsrc.resolved_value (field, value_min_num, value_max_num);
CREATE INDEX resolved_value_ts_idx ON extsrc.resolved_value (field, value_min_ts, value_max_ts);

-- Documents: stored once per URL, one version per content hash.
CREATE TABLE extsrc.document (
    doc_id         text PRIMARY KEY,
    url            text NOT NULL,
    resolved_url   text,
    title          text,
    role           text,
    mime           text,
    latest_sha256  text,
    last_status    text,
    last_error     text,
    first_seen     timestamptz NOT NULL DEFAULT now(),
    last_seen      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE extsrc.document_version (
    doc_id          text NOT NULL REFERENCES extsrc.document (doc_id) ON DELETE CASCADE,
    content_sha256  text NOT NULL,
    text            text NOT NULL,
    chars           integer NOT NULL,
    mime            text,
    etag            text,
    last_modified   text,
    retrieved_at    timestamptz,
    stored_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (doc_id, content_sha256)
);

CREATE TABLE extsrc.document_chunk (
    doc_id          text NOT NULL,
    content_sha256  text NOT NULL,
    chunk_no        integer NOT NULL,
    heading         text,
    page_start      integer,
    page_end        integer,
    text            text NOT NULL,
    text_sha256     text NOT NULL,
    tsv             tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    PRIMARY KEY (doc_id, content_sha256, chunk_no),
    FOREIGN KEY (doc_id, content_sha256)
        REFERENCES extsrc.document_version (doc_id, content_sha256) ON DELETE CASCADE
);
CREATE INDEX document_chunk_tsv_idx ON extsrc.document_chunk USING gin (tsv);

-- Which documents a record links to in a snapshot, and how the fetch went.
CREATE TABLE extsrc.record_document (
    snapshot_id     bigint NOT NULL,
    natural_key     text NOT NULL,
    url             text NOT NULL,
    doc_id          text REFERENCES extsrc.document (doc_id),
    role            text,
    title           text,
    status          text NOT NULL,
    error           text,
    content_sha256  text,
    chars           integer,
    PRIMARY KEY (snapshot_id, natural_key, url),
    FOREIGN KEY (snapshot_id, natural_key)
        REFERENCES extsrc.external_record (snapshot_id, natural_key) ON DELETE CASCADE
);
CREATE INDEX record_document_doc_idx ON extsrc.record_document (doc_id);

-- Change tracking (the DB form of change_tracking.ChangeTracker's manifest).
CREATE TABLE extsrc.record_tracking (
    source            text NOT NULL,
    natural_key       text NOT NULL,
    first_seen        timestamptz NOT NULL,
    last_seen         timestamptz NOT NULL,
    last_changed      timestamptz NOT NULL,
    last_hash         text NOT NULL,
    absent_runs       integer NOT NULL DEFAULT 0,
    expired_at        timestamptz,
    expired_reason    text,
    last_snapshot_id  bigint,
    PRIMARY KEY (source, natural_key)
);

CREATE TABLE extsrc.record_change (
    id             bigserial PRIMARY KEY,
    run_id         uuid,
    source         text NOT NULL,
    snapshot_id    bigint,
    natural_key    text NOT NULL,
    state          text NOT NULL CHECK (state IN ('new', 'changed', 'unchanged', 'missing', 'expired')),
    previous_hash  text,
    new_hash       text,
    absent_runs    integer,
    reason         text,
    at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX record_change_key_idx ON extsrc.record_change (source, natural_key, at DESC);
CREATE INDEX record_change_at_idx ON extsrc.record_change (at);

-- Everything that failed, with its raw payload, so nothing is lost and it can be retried.
CREATE TABLE extsrc.dead_letter (
    id               bigserial PRIMARY KEY,
    source           text NOT NULL,
    stage            text NOT NULL,
    item_key         text NOT NULL,
    error            text NOT NULL,
    payload          jsonb,
    retryable        boolean NOT NULL DEFAULT true,
    attempts         integer NOT NULL DEFAULT 1,
    first_run_id     uuid,
    last_run_id      uuid,
    first_failed_at  timestamptz NOT NULL DEFAULT now(),
    last_failed_at   timestamptz NOT NULL DEFAULT now(),
    resolved_at      timestamptz,
    resolution       text
);
CREATE UNIQUE INDEX dead_letter_open_idx ON extsrc.dead_letter (source, stage, item_key)
    WHERE resolved_at IS NULL;
