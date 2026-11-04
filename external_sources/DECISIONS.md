# External-sources DB — decisions

Every decision taken while building the store (`external_sources/store/`), with the alternatives
considered and the reason. Date of all entries: 2026-10-28 unless stated. Decisions were delegated
by Gabriel for this build; any of them can be revisited — each names what it would cost to change.

## D1. Storage engine: Postgres 16 + pgvector (as designed)
- **Decision**: follow the agreed design (Postgres + pgvector, one store for structured filters and
  vectors). Image `pgvector/pgvector:pg16` (pgvector 0.8.2 measured).
- **Alternatives**: second Chroma collection; SQLite for dev.
- **Why**: the agreed design; range filters (deadlines, budgets) and FTS + vectors in one query.
- **Reviewed 2026-11-05 (kept, as a design choice)**: Chroma could hold the calls too (metadata
  filters with `$gt/$lt/$in/$and`, `where_document` text filters), with dates stored as epoch
  numbers. What it does not give is the snapshot/pointer-swap publication (no transactions, no
  collection alias), the provenance tables and joins, or ranked full-text search; a Chroma version
  would still need a relational store for those. The retrieval *design* is the same as CISUC's
  (D19); only the engine differs.

## D2. Full replace = per-source snapshots with an atomic pointer swap (not a staging *schema*)
- **Decision**: every load writes a new `snapshot` (status `staging`) into the same tables; after
  validation, publishing is one transaction that moves `source_state.published_snapshot_id`.
  The chatbot reads only the `current` schema (views that follow the pointers).
- **Alternatives**: (a) staging schema + `ALTER SCHEMA ... RENAME` swap; (b) truncate-and-insert in
  one transaction; (c) upsert per record (the original sketch).
- **Why**: per-source granularity is required for stale-while-error (a failed `ft_cascade` must not
  touch `ft_portal`); the previous snapshot stays intact, so rollback is a pointer move and a
  rejected load never empties anything; history tables (provenance, changes, dead letters) are
  shared across snapshots and do not need to be swapped; no DDL at run time (no lock storms, no
  privileges needed). Cost: storage per snapshot (≈25 s and ≈16k provenance rows per 333-record
  load), bounded by retention (D10).

## D3. Snapshot identity includes the run id
- **Decision**: `batch_sha256` = run id + source + record hashes + document outcomes + partial flags.
- **Alternatives**: content-only hash (first version).
- **Why**: found by the integration tests — with a content-only hash an unchanged source on the
  next day was "already loaded", so data age and absence counting stopped advancing. With the run
  id, re-loading the same spooled batch is a no-op (idempotent resume) and a new run is a new snapshot.

## D4. Provenance: one row per field per layer, plus every alternative value
- **Decision**: `field_value` rows with `role` = value | alternative | corroboration | superseded |
  check, `layer` = api | detail | document | other, URL, doc id, page, section, verbatim quote,
  retrieved/published times, content hash, confidence. Built from `field_sources`, `evidence`,
  `verification.layers`, `value_range.values`, `corroboration`, `value_history`.
- **Long values** (> 2,000 serialized chars, e.g. descriptions, conditions) are stored by hash plus
  a 300-char preview; the full text is in `external_record` of the same snapshot (nothing lost,
  ~5× less duplication).
- **Confidence** is a fixed per-layer prior (api/detail 0.9, document 0.8, other 0.5): the verdict
  (`resolved_value.status`, `agree_count`) is the real certainty signal.

## D5. Resolved values and ranges: keep every stated value, even within tolerance
- **Decision**: `resolved_value` keeps the verifier's status (confirmed | single_source |
  conflicting | resolved | absent) and the primary value, and `value_min/max` (numeric or
  timestamp) span **every contributing value**. Example measured on real data:
  HORIZON-MSCA-2027-SE-01-01 budget is `confirmed` (within the verifier's tolerance) but the range is
  95,035,106..95,150,000 because the API and the work programme state different numbers.
- **Alternatives**: collapse the range when the verifier says "confirmed".
- **Why**: Gabriel's rule "store everything, keep inconsistencies with ranges"; filters stay
  range-aware (D12) and the status still says how certain the value is.

## D6. Absent records: grace runs, partial runs never count, deadlines expire immediately
- **Decision**: reuse `ChangeTracker` semantics (grace_runs = 2) in the database
  (`record_tracking`, `record_change`). A record missing from a COMPLETE run is carried over
  (`carried_over = true`, `absent_runs`); after 2 consecutive complete absences it expires. A
  PARTIAL run (a page or a programme failed) never increments `absent_runs`. A carried record whose
  every deadline has passed is not carried: it expires with reason `every deadline has passed`.
  `current.records` also turns open/forthcoming calls whose cut-offs are all past into `closed` at
  read time, so nothing lingers as open between loads.
- **Why**: a partial API response must never wipe calls, and a closed call must never be served as open.

## D7. Validation gates (reject = keep last good data + alert)
- **Hard (reject)**: record count < `min_records_ratio` × previous (quality_policy per source,
  default 0.7; only when the previous snapshot had ≥ 10 records); a required field's coverage falls by
  > 25 points; an empty batch when the previous one had data (or an empty partial first run);
  referential integrity (orphan document links, records without resolved values).
- **Soft (alert only)**: coverage floors and drops from `quality_policy.yaml`, missing citable URL,
  partial run, dead letters.
- **Override**: `run --force-publish` (e.g. a real mass closing of calls), recorded in `validation.forced`.
- **Why**: quality_policy says thresholds never drop records; only a structural collapse should block.

## D8. Resilience mechanics
- Retries: 4 attempts, exponential backoff with full jitter (base 2 s, cap 60 s), `Retry-After`
  honoured (seconds, fractional seconds, HTTP date; capped at 300 s), plus one whole
  "request + parse JSON" retry so truncated/malformed bodies are retried.
- Timeouts everywhere: HTTP (10 s connect / 90 s read), DB connect 10 s, loader statement 10 min,
  reader statement 5 s, idle-in-transaction 10 min.
- Circuit breaker per host: 8 consecutive failures open it for 5 minutes (plugins and documents
  share the registry). A host-wide outage then fails fast instead of costing every programme the full
  retry budget.
- Isolation: per page/programme (partial reason), per record (dead letter with raw payload), per
  document (dead letter), per source (failed, last snapshot kept and flagged stale).
- Schema drift: unknown raw keys → `data_gaps` kind `schema_drift` on the record; missing
  `results` → page failure; missing identifier → dead letter; missing title → natural key as a
  placeholder + gap.
- Checkpoints: `fetched.json`, `enriched.jsonl` (fsynced appends), `batch.json` (atomic). The HTTP
  cache + document store act as the phase-1 checkpoint (`fresh_for` = 12 h: a resumed run does not
  re-request what it fetched recently). Unfinished runs older than 36 h are abandoned (their spooled
  batches still load; an older batch never replaces newer data: `obsolete`).
- SIGTERM/SIGINT: finish the current item, flush, exit 75; next invocation resumes the same run.
  `StopRequested` derives from BaseException so per-record `except Exception` cannot swallow it.
- DB down or lost mid-load: the batch stays spooled (`pending_load`), exit 3; the next run loads
  pending batches before collecting.
- Disk full: checkpointing is disabled for the rest of the run (reported, exit 1); data still loads
  from memory.
- One run at a time: `fcntl` lock on `<work>/run.lock` (released by the OS on kill -9).

## D9. Exit codes for schedulers
0 all published complete · 1 degraded (partial/rejected/failed source, data still served and
flagged) · 2 usage/config · 3 DB unreachable (spooled) · 4 another run holds the lock · 75 interrupted.

## D10. Retention
Keep the 14 newest published/superseded snapshots per source (published + previous are never
deleted); rejected/abandoned staging snapshots 14 days; run log and change history 400 days; resolved
dead letters 90 days; document versions/documents not linked by any retained snapshot 30 days; local
run directories 14 days (never ones with a pending batch). All in `config/store_policy.yaml`.

## D11. Collection scope
- **Types**: 1 (topics, with detail endpoint + documents), 8 (cascade funding) and 2 (EU external
  action calls for proposals, new `ft_europeaid` plugin). Type 0 (procurement tenders) out of scope.
- **Statuses**: open + forthcoming (the portal exposes only these and closed); closed calls leave
  through change tracking (D6), since the chatbot serves future calls.
- **All programmes**: besides the 23 configured programmes, one catch-all query (`must_not` the
  configured ids) collects every other programme, labelled from the portal's facet endpoint.
  Measured 2026-10-28: 34 type-1 hits in 12 unconfigured programmes (Euratom, JTM, ESF, ...).
- **Raw payloads** are stored once per content hash (`payload`), linked from every record.

## D12. Read API semantics
- Deadline window: a call matches if ANY cut-off falls inside, or the resolved deadline range
  (API vs documents) overlaps the window. Budget: the resolved budget range overlaps
  [min, max]; calls without a stated budget never match a budget filter (they are not "0").
- Budget filters use the topic budget (`funding_amount`); call totals stay in details (meeting
  decision: budgets reported at separate levels).
- Read-only login + read-only transactions + 5 s statement timeout; parameterised SQL only; the
  WHERE clause is assembled from fixed fragments.
- Every result carries attribution/licence and a freshness block; `render_context` prepends a
  `[Data note]` when the source is stale, partial, or the call was carried over.

## D13. Embeddings: optional, deferred, resumable
`python -m external_sources.store embed` embeds only rows without an embedding, with
MODEL_EMBEDDINGS on OLLAMA_URL (read from the environment, never printed). When the server is
unreachable (no VPN today) it defers and exits 0. 768 dimensions fixed by migration 0003 (a model of
another size needs a new migration). FTS never depends on it. No billed API.

## D14. Additional sources: none added in this pass (licences recorded)
| Source | Licence evidence (2026-10-28) | Decision |
|---|---|---|
| CORDIS (projects) | legal notice page not retrievable (`/about/legal-notice` 404; open-data page renders by JS); EU open data is normally CC BY 4.0 — unverified here | not now: awarded projects, not calls; being designed in the projects work |
| OpenAIRE Graph (projects) | "available for download and re-use as CC-BY" (graph.openaire.eu/docs/license, v11.4.0) | not now: awarded projects; clean REST plugin later (record_type `project`) |
| ccfddl/ccf-deadlines, huggingface/ai-deadlines | MIT (GitHub license API) | not now: conference CFP deadlines are not funding calls; fits a separate `events` domain |
| WikiCFP | no API; scraping with 1 page/5 s guideline | no |
| DevelopmentAid | Cloudflare challenge; reading API "internal systems" only | no (licence) |
| Portugal 2030 | terms forbid copying without authorisation | no (until written authorisation) |
- **Why**: the brief limits new sources to open, licence-compatible feeds that fit cleanly; the
  only funding-call feeds that qualify are the three F&T types, all implemented. Adding project
  databases or conference deadlines would mix domains in the calls store without a consumer yet.

## D15. Configuration and secrets
(Updated 2026-11-05.) The store is part of the application stack: its settings live in
`config/chatbot.env` (db name, users, timeouts, image, `CALLS_*`); the two passwords
(`EXTSRC_DB_PASSWORD`, `EXTSRC_DB_READER_PASSWORD`) only in git-ignored `secrets/chatbot.env`
locally and in GitHub secrets in CI. Host and port are fixed inside the compose network
(`calls_db:5432`, like `chromadb:8000`); the database publishes no host port. Compose uses
`${VAR:?...}` for every variable. The reader login is created by `store/init/01-reader-role.sh`
with psql variables (no shell interpolation into SQL).

## D16. Python dependency
`psycopg[binary]` 3.x (added to requirements.txt), imported lazily: the rest of the package and the
unit suite run without it; integration tests skip cleanly without it or without docker.
Alternatives: psycopg2 (older API, no native pipeline), SQLAlchemy (heavy, not needed).

## D17. Document chunks
Section-aware chunks (heading tree, then paragraphs, then sentences; ≤ 2,000 chars) with the PDF
page range from `<!-- page N -->` markers; stored once per document version; boilerplate documents
are excluded from document search and embedding.

## D18. Live-run findings acted on (2026-10-28/29)
- **Boilerplate documents that answer with a challenge are dead letters, not bypassed**: eur-lex
  answers scripted requests with HTTP 202 challenge pages (86 URLs) and 25 ec.europa.eu legal pages
  failed; all are `boilerplate` (legal texts excluded from retrieval). No call-specific or
  work-programme document failed. Alternatives: headless browser / challenge solving — rejected
  (security floor, terms).
- **Open circuit = network error** (`CircuitOpenError` subclasses `requests.ConnectionError`): found
  by the document chaos test; an open circuit now serves the stored copy instead of failing it.
- **Credential-like query parameters are masked** in failure messages and logs (`redact()`): urllib3
  echoes full URLs; the F&T key `SEDIA` is public, but no key-shaped value should reach a log.
- **NUL characters are stripped** before insert: Postgres text/jsonb reject them, and one stray
  `\u0000` in a payload must not fail a whole load.
- **Legacy binary attachments** (`application/octet-stream`, not PDF/DOCX/HTML by content sniffing)
  stay `unsupported_type` dead letters; adding .doc/.xls converters would need new dependencies.

## D19. Retrieval for the chatbot: a RAG_CALLS service shaped like RAG_CISUC (2026-11-05)
- **Decision**: `application/RAG_CALLS/` exposes `POST /query {query, top_k} -> {results: [{text,
  metadata}]}`, the RAG_CISUC contract. Hybrid retrieval as in RAG_CISUC: lexical (Postgres FTS,
  any-term match like BM25) + semantic (pgvector over call embeddings), weighted RRF 0.6 / 0.4. An
  identifier in the query (`HORIZON-CL4-...`, `EuropeAid/...`) is looked up exactly and goes first
  (FTS splits identifiers into words: measured, the exact topic ranked 5th without it). Each result
  is one call rendered by `context.py` (first `CALLS_DETAILED` in full, the rest compact).
- **Alternatives**: the query-understanding planner calling the read API directly (branch
  `feature/calls-integration`): left out by Gabriel's decision, to keep one retrieval design.
- **Why**: Gabriel's rule — follow the same design as the CISUC/Chroma retrieval. One image
  (`cisuc-calls`) serves the API and the scheduled loader.

## D20. Source routing: the existing SLM call adds a tag (2026-11-05)
- **Decision**: the keyword-extraction prompt also starts its answer with `[CISUC]`, `[CALLS]` or
  `[AMBOS]` (`application/Orchestrator/routing.py`); a missing/unknown tag means CISUC (the old
  behaviour); `CALLS_SOURCE=off` disables the calls source. Same single SLM call, no added latency.
- **Measured** (llama3.1:8b, 16 PT/EN questions): 14/16 routed correctly; all single-source
  questions right; the 2 misses are CISUC×calls cross questions ("calls for group X"), which need a
  two-step retrieval (group topics, then calls) that the single-step design does not do.
- **Alternatives**: always query both (calls text in every CISUC answer); keyword rules (miss
  paraphrases).

## D21. Removed in the consolidation (2026-11-05)
The JSON-file tooling that the store run replaced: `run_extraction.py`, the `enrich` CLI,
`audit.py`, `validate.py` + `validation/`, the file-manifest `ChangeTracker` (change tracking is in
the DB, D6; `record_hash` moved to `plugins/base.py`), the report half of `quality.py`, and the
`keep_eu` stub (never validated: no API key). The dev-only `store/docker-compose.yaml`,
`scripts/run-external-sources-db.sh` and `config/external-sources.env` are replaced by the
application stack. All recoverable from `feature/external-sources-db`.

