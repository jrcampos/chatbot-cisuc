-- The `current` schema is the only thing the chatbot reads: the published snapshot of every
-- source, with freshness (stale / partial / age) next to every row. The read-only role
-- `extsrc_reader` gets USAGE on `current` only, never on `extsrc`.

CREATE SCHEMA IF NOT EXISTS current;

CREATE VIEW current.source_freshness AS
SELECT s.name                                AS source,
       s.licence,
       s.attribution,
       st.published_snapshot_id              AS snapshot_id,
       sn.fetched_at                         AS data_as_of,
       sn.published_at,
       now() - sn.fetched_at                 AS data_age,
       (st.stale_since IS NOT NULL)          AS stale,
       st.stale_since,
       st.stale_reason,
       coalesce(sn.partial, false)           AS partial,
       coalesce(sn.partial_reasons, '[]')    AS partial_reasons,
       sn.record_count,
       st.last_attempt_at,
       st.last_success_at,
       st.last_status,
       st.consecutive_failures
FROM extsrc.source s
LEFT JOIN extsrc.source_state st ON st.source = s.name
LEFT JOIN extsrc.snapshot sn ON sn.snapshot_id = st.published_snapshot_id;

-- Every record of every published snapshot. `next_deadline` is the next cut-off still ahead;
-- `effective_status` turns an open/forthcoming record whose cut-offs have all passed into
-- 'closed' at read time, so a call never lingers as open between two loads.
CREATE VIEW current.records AS
WITH cutoffs AS (
    SELECT r.snapshot_id, r.natural_key,
           (SELECT min(d) FROM unnest(coalesce(r.deadline_dates, ARRAY[r.deadline_date])) AS d
             WHERE d >= now())                                            AS next_deadline,
           (SELECT max(d) FROM unnest(coalesce(r.deadline_dates, ARRAY[r.deadline_date])) AS d)
                                                                          AS last_deadline
    FROM extsrc.external_record r
    JOIN extsrc.source_state st ON st.published_snapshot_id = r.snapshot_id
)
SELECT r.snapshot_id, r.source, r.natural_key, r.record_type, r.record_kind, r.domain,
       r.programme, r.programme_key, r.call_identifier, r.call_title, r.country_scope,
       r.title, r.description,
       r.status                                   AS source_derived_status,
       CASE WHEN r.status IN ('open', 'forthcoming') AND c.last_deadline IS NOT NULL
                 AND c.next_deadline IS NULL THEN 'closed'
            ELSE r.status END                     AS status,
       r.funding_amount, r.currency, r.start_date, r.deadline_date, r.deadline_dates,
       c.next_deadline, r.source_url, r.source_updated_at, r.details, r.field_sources, r.data_gaps,
       r.raw_sha256, r.content_hash, r.fetched_at, r.change_state, r.carried_over, r.absent_runs,
       r.search_tsv, r.embed_text_sha256,
       f.stale, f.stale_since, f.data_age, f.partial AS source_partial, f.data_as_of
FROM extsrc.external_record r
JOIN cutoffs c ON c.snapshot_id = r.snapshot_id AND c.natural_key = r.natural_key
JOIN current.source_freshness f ON f.snapshot_id = r.snapshot_id;

CREATE VIEW current.calls AS
SELECT * FROM current.records WHERE status IN ('open', 'forthcoming');

CREATE VIEW current.resolved_values AS
SELECT v.* FROM extsrc.resolved_value v
JOIN extsrc.source_state st ON st.published_snapshot_id = v.snapshot_id;

CREATE VIEW current.field_values AS
SELECT v.* FROM extsrc.field_value v
JOIN extsrc.source_state st ON st.published_snapshot_id = v.snapshot_id;

CREATE VIEW current.record_documents AS
SELECT d.*, doc.resolved_url, doc.mime
FROM extsrc.record_document d
JOIN extsrc.source_state st ON st.published_snapshot_id = d.snapshot_id
LEFT JOIN extsrc.document doc ON doc.doc_id = d.doc_id;

-- Chunks of the latest version of every document linked from a published record.
CREATE VIEW current.document_chunks AS
SELECT ch.doc_id, ch.content_sha256, ch.chunk_no, ch.heading, ch.page_start, ch.page_end,
       ch.text, ch.tsv, doc.url, doc.resolved_url, doc.title AS document_title, doc.role
FROM extsrc.document_chunk ch
JOIN extsrc.document doc ON doc.doc_id = ch.doc_id AND doc.latest_sha256 = ch.content_sha256
WHERE EXISTS (SELECT 1 FROM current.record_documents rd WHERE rd.doc_id = ch.doc_id);

CREATE VIEW current.programmes AS
SELECT p.programme_key, p.label, p.sources,
       count(c.natural_key) FILTER (WHERE c.status IN ('open', 'forthcoming')) AS live_calls
FROM extsrc.programme p
LEFT JOIN current.records c ON c.programme_key = p.programme_key
GROUP BY p.programme_key, p.label, p.sources;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'extsrc_reader') THEN
        CREATE ROLE extsrc_reader NOLOGIN;
    END IF;
END
$$;
GRANT USAGE ON SCHEMA current TO extsrc_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA current TO extsrc_reader;
