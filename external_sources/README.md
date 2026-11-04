# External Sources — EU funding calls

Scheduled extraction of **open and forthcoming EU funding calls** into a Postgres + pgvector store,
read by the chatbot through the `RAG_CALLS` retrieval service (`application/RAG_CALLS/`), the same
way CISUC's own data is read through `RAG_CISUC` from ChromaDB. Nothing here runs on the request
path: the loader runs on a schedule (`calls_ingest` service, daily by default). Decisions and their
reasons: [`DECISIONS.md`](DECISIONS.md).

```
GUI ─► Orchestrator ─┬─► RAG_CISUC (/query) ─► chromadb               CISUC knowledge base
     (SLM source tag └─► RAG_CALLS (/query) ─► calls_db (Postgres + pgvector, read-only login)
      [CISUC] [CALLS] [AMBOS])                     ▲
                        calls_ingest: store run --all (daily) + store embed --records-only
```

## Three layers per call, one plugin per source

| Layer | What | Where |
|---|---|---|
| 1. Search API | one plugin per source / search type; a declarative field spec maps every raw key | `plugins/`, `config/*_fields.yaml` |
| 2. Detail endpoint | richer per-item JSON; URL template in config, source-agnostic fetcher | `enrichment/detail.py`, `config/eu_programmes.yaml` (`detail:`) |
| 3. Linked documents | call fiches, work programmes (PDF/DOCX/HTML -> Markdown); fields read by declarative extraction families chosen per programme | `documents/`, `extraction/`, `config/programme_profiles.yaml`, `config/document_fields.yaml` |

Values from all layers are cross-checked (`verification.py`): agreeing values are corroborated,
disagreeing numbers and dates are kept as ranges with every value and its source, never a silently
picked one. Every record carries numbered citable sources (`sources.py`).

Sources (all on the EU Funding & Tenders Portal, CC BY 4.0, see `store/registry.py`):

- `ft_portal` — call topics (search type 1) of every programme: the 23 in `config/eu_programmes.yaml`
  plus a catch-all query for programmes not configured (labelled from the portal's facet endpoint).
  Uses the detail endpoint and the documents.
- `ft_cascade` — cascade-funding calls launched by EU-funded projects (search type 8). The texts are
  written by the projects: shown with attribution and the portal link.
- `ft_europeaid` — EU external-action calls for proposals (search type 2); the texts are the
  published documents.

Every plugin isolates failures per page and per record (`collect()` returns records, failures with
their raw payloads, and partial reasons) and records unknown raw keys as `schema_drift` gaps.

**Per-programme formats are configuration, not code.** A programme profile
(`config/programme_profiles.yaml`) says which documents of its calls matter (classify rules by URL,
host and link text), which hosts are never fetched, and which extraction families read each
document role. A family (`config/document_fields.yaml`) is a set of section headings / anchors and
field rules (`text`, `eur`, dates, tables). A programme using the usual F&T call-fiche template
needs one profile line; a new document format needs a new family.

## Layer 1: field specs (search API -> NormalizedRecord)

Each plugin's raw payload is normalized by a declarative **field spec**: one
YAML file per plugin under `config/` (e.g. `config/ft_portal_fields.yaml`),
loaded with `normalization.load_spec` and set on the plugin as `field_spec`.

Spec format:

- `source` — the plugin name.
- `fields` — every raw key the source sends must be **mapped or ignored with a
  reason** (`{ignore: "why"}`). Nothing is dropped silently, and the untouched
  payload always stays in `record.raw`.
- A mapping is a list of rules: `to` (target field), `parse` (a list of
  transform steps, e.g. `first`, `strip`, `html_to_text`, `iso_datetime`) and
  optionally `applies_to` (record types the rule applies to) and
  `may_be_empty: true` (the source can legitimately say "nothing here", e.g.
  HTML that is only `&nbsp;` or a budget of 0: an emptied value is then
  `n/a:source_empty` instead of `gap:empty`). One raw key may feed several
  targets.
- `not_applicable` — `{field: [record_types]}` for fields that no raw key can
  feed for a record type (e.g. a field only awarded projects carry).
- `finalizers` — named post-processing steps that run once all rules have been
  applied (e.g. `derive_status`).
- `transform_modules` — dotted module paths imported by `load_spec` before it
  validates the steps, so a spec brings its own source-specific transforms
  (the F&T spec lists `external_sources.plugins.ft_portal_transforms`). A
  missing module or a non-list is a `SpecError`.

Transforms are registered with `@transform("name")`; pass
`produces_date=True` for one whose output is ISO-8601 UTC date(s) (like
`iso_datetime` and `ft_all_dates`). Date targets (`start_date`,
`deadline_date`) must include such a step (`load_spec` rejects the spec
otherwise).

Targets are the normalized top-level fields plus `details.<name>`. `details`
is a free-form dict for prepared extras (call identifier, keywords, budget
breakdown, links, ...) that do not deserve a top-level column. Output
conventions: dates are ISO-8601 UTC, `description` is cleaned text (no markup
or entities), and `status` is derived (an "open" call whose deadline has
passed becomes `closed`; the source's own label is kept in
`details.source_status`).

F&T Portal specifics:

- `source_url` is canonical: the API path `.../opportunities/data/topicDetails/<ID>`
  (565 records) is rewritten by `ft_topic_url` to the browsable
  `.../opportunities/portal/screen/opportunities/topic-details/<ID>` page.
- `details.description_links` keeps the links of the description HTML as
  `[{text, url}]` next to the cleaned `description` text.
- `details.actions` and `details.links` are structured, not raw JSON strings:
  `actions` is a list of `{types: [{action, grant_type: [{code, description}]}],
  status, submission_procedure, planned_opening_date, deadline_dates}`;
  `links` is a list of `{call_identifier, criterion_code, criterion_description,
  url, mga_code, mga_description, start_date, end_date}` (epoch-millisecond
  dates become ISO UTC). Opaque portal ids are dropped.
- `deadline_date` is the next upcoming cut-off (else the latest); all cut-offs
  are in `details.deadline_dates`.

## Layer 2: detail endpoint

The search payload of a topic can be empty or truncated. `enrichment/detail.py` fetches the
per-topic detail JSON (URL template and payload root in `config/eu_programmes.yaml`; the F&T portal
wants the lowercase identifier) through the same HTTP cache, and only fills what layer 1 left empty.

## Layer 3: documents (conversion details)

- PDFs: `pdftotext -layout`, then per-page clean-up — running headers/footers (a line repeated at
  the top/bottom of >= 40% of pages, and of >= 3), bare page numbers, and page-tail footnotes,
  which become `[^n]: text` lines (kept in the stored document, dropped from extracted prose).
  `<!-- page N -->` markers stay, so every extracted value carries the PDF page it was read on
  (`evidence[field].page`).
- Word (.docx): read with the standard library (zip + XML, entities not resolved, 50 MB cap):
  headings, paragraphs, table rows. Other zip archives are `unsupported_type`.
- europa.eu document landing pages ("Download (996 KB - PDF)"): for call-specific and
  programme-level roles the download link is followed once, same host only
  (`document_policy.yaml: follow_download_text_patterns`); stored metadata keeps `resolved_url`.
- Families can be anchored on the topic id (`anchored`), on a heading (`anchor.by_heading`, e.g.
  the EIC work programme: one rule per EIC call type) or on the CALL id (`call_anchored`, `{CALL}`
  in the pattern: Horizon WP call rows `HORIZON-MSCA-2026-DN-01 628.03` and call blocks
  `Opening: ... Deadline(s): ...`). `pattern` may be a list (first match wins); `eur_breakdown`
  keeps labelled call-TYPE budgets (EIC Accelerator Open / Challenges) apart from topic budgets.

## Verification, value ranges and sources

Every enriched record carries (see `config/verification_policy.yaml`, `verification.py`,
`sources.py`):

- `details.verification[field]` for title, description, funding_amount, topic/call budgets,
  deadline_date, start_date, programme and eligibility: `status` (confirmed | single_source |
  conflicting | resolved | absent), the value per layer, how many independent layers agree (API
  and detail endpoint count as one), the source URLs/pages and `checked_at`. Agreeing document
  values are kept in `details.corroboration` (they used to be dropped). A topic budget equal to
  the call budget stated by the call document is labelled `level: call_total`.
- **Ranges, never a picked value**: when sources disagree on a number or a date, the verdict and
  `details.value_range[field]` hold `{min, max, primary, settled, status, values: [{value, layer,
  source, page?}]}`. The status stays `conflicting` unless an official source settles it: an
  official topic-page update notice newer than the document (`update_notices`), or a checked entry
  in `config/verification_verdicts.yaml` (each with source URLs, quotes and `checked_at`). Settled
  ranges keep the settled value as `primary` and still list the others.
- `details.sources`: numbered citable sources — the F&T topic page (or the portal page of a
  cascade call) first, then every stored document that supplied, corroborated or contradicted a
  value (`kind` call_document | work_programme | programme_document, `fields`, `pages`,
  `retrieved_at`, `last_modified`, `content_sha256`, `doc_id`, `download_url`, `disagrees_on`).
- `context.py` packs a `[Sources]` block right after the card in every answering intent, shows
  value ranges with each value's source in the budget/deadline blocks, labels call totals as such,
  and adds every cited URL to `pack.citations`.

## The store (Postgres + pgvector)

```
 per source (ft_portal, ft_cascade, ft_europeaid) — one failing source never stops the rest
 collect ──► fetched.json ──► enrich (detail, documents, verification) ──► enriched.jsonl
                     └──► batch.json (spool) ──► loader ──► Postgres
 Postgres  extsrc.*  (writer)                                current.*  (read-only chatbot role)
   snapshot ── staging ─validate─► published (pointer swap)   records / calls (effective status)
   external_record + payload (raw, by hash)                   resolved_values (value, status, min..max)
   field_value  (provenance: every value of every layer)      field_values, record_documents
   resolved_value (status + range + contributing + sources)   document_chunks (+ embeddings)
   document / document_version / document_chunk (FTS, vector) source_freshness (stale, age, partial)
   record_tracking / record_change (new/changed/.../expired)  programmes, record_embeddings
   run / source_run / dead_letter / source_state
```

A load writes a new snapshot; after the validation gates (record-count drop, required-field
coverage collapse, empty batch) it is published by moving one pointer, so a failed or rejected load
never empties anything and the previous snapshot is one pointer move away. A call missing from a
complete run is carried over for `tracking.grace_runs` runs; a partial run never counts as an
absence; calls whose deadlines have all passed are served as `closed` at once.

### Running it

In the application stack (`./scripts/run-application.sh up-build`) three services do everything:

- `calls_db` — pgvector image; `store/init/01-reader-role.sh` creates the read-only login on the
  first start of an empty volume.
- `calls_ingest` — `application/RAG_CALLS/ingest.sh`: `store migrate`, then every
  `CALLS_INGEST_INTERVAL_SECONDS` `store run --all`, `store embed --records-only`, `store status`.
- `rag_calls` — the retrieval API (same image).

Configuration is in `config/chatbot.env`; the two passwords (`EXTSRC_DB_PASSWORD`,
`EXTSRC_DB_READER_PASSWORD`) in `secrets/chatbot.env` locally and in GitHub secrets in CI. The first
load of a new stack downloads every document (about 15 minutes); until it is published `rag_calls`
returns no calls. Manual commands run inside the loader container:

```bash
docker compose -p <project> exec calls_ingest python -m external_sources.store status
docker compose -p <project> exec calls_ingest python -m external_sources.store coverage
docker compose -p <project> exec calls_ingest python -m external_sources.store dead-letters list
docker compose -p <project> exec calls_ingest python -m external_sources.store run --source ft_cascade --fresh
```

Exit codes of `store run`: **0** all sources published complete · **1** degraded (a source partial,
rejected or failed — the last good data stays served, flagged) · **2** configuration error ·
**3** database unreachable (batches spooled, loaded by the next run) · **4** another run in progress
· **75** interrupted (resumed by the next run). Alerts go to `<work>/alerts.log` and, when
`ALERT_WEBHOOK_URL` is set, to that webhook. PDF conversion needs `pdftotext` (poppler-utils, in the
image). Document fetches honour robots.txt; challenge pages are dead letters, never bypassed.

### What the chatbot reads

`RAG_CALLS` answers `POST /query {query, top_k}` with `{results: [{text, metadata}]}`, like
`RAG_CISUC`: an exact lookup of any call/topic identifier in the query goes first, then Postgres
full-text search (any-term match, like BM25) and pgvector search over the call embeddings, fused
with the same weighted RRF as `RAG_CISUC` (0.6 / 0.4). Each call is one cited text block from
`context.py` (card, numbered sources, description, budget with ranges, deadlines): the first
`CALLS_DETAILED` calls in full, the rest compact. `GET /health` shows the freshness of every source.

The underlying read API (`store/queries.py`: read-only login, 5 s statement timeout, parameterised
SQL) also has deadline-window, budget-range and programme filters for later use:

```python
from external_sources.store import queries
conn = queries.connect_reader()
calls = queries.search_calls(conn, text="quantum computing", text_any=True, programme="Horizon",
                             deadline_from=date(2027, 1, 1), budget_min=1_000_000, order="relevance")
pack = queries.render_context(calls[0], "deadlines")   # capped blocks + [Data note] when stale
```

### Recovery runbook

| Failure | What happens automatically | What to do |
|---|---|---|
| Source API down / 5xx / timeouts | retries with backoff, circuit breaker; source marked `failed`, last snapshot kept and flagged `stale` (`current.source_freshness`), exit 1 | nothing; check `status`; next run refreshes |
| Some pages/programmes fail | snapshot published as `partial` with reasons; absent calls carried, absence not counted | nothing; `dead-letters list` shows the pages |
| Malformed JSON / empty response | retried; then the page is a dead letter (with query) | `dead-letters retry` once the API is back |
| API schema drift | unknown keys → `schema_drift` gaps (raw kept), records without id → dead letters with payload | update the field spec; `dead-letters retry` re-normalizes stored payloads |
| Document 404/403/corrupt/too large/unsupported | record loaded without it; document dead letter; robots-blocked is a policy outcome | `dead-letters retry` after fixing; nothing for robots |
| Validation rejects a snapshot (count drop, coverage collapse) | last good snapshot stays published, source `stale`, critical alert | inspect `extsrc.snapshot.validation`; if the drop is real: `run --source X --force-publish` |
| Database down at load | batch spooled (`runs/<id>/<source>/batch.json`), exit 3 | start the DB; the next `run` loads spooled batches first |
| Connection lost mid-load | transaction rolled back (nothing half-loaded), batch spooled, exit 3 | same as above |
| Process killed (kill -9, OOM, reboot) | lock released by the OS; checkpoints on disk | run again: it resumes the same run (fetch and enriched records are not redone) |
| SIGTERM (systemd stop, deploy) | current record finished, checkpoints flushed, exit 75 | run again to resume |
| Disk full | checkpointing disabled for the run (reported), data still loaded from memory, exit 1 | free space |
| Two runs at once | second exits 4 | nothing |
| Bad deploy / wrong data published | previous snapshot kept (`source_state.previous_snapshot_id`) | `UPDATE extsrc.source_state SET published_snapshot_id = previous_snapshot_id WHERE source = '...'` |

Retention runs at the end of each run (`store_policy.yaml: retention`; also `python -m
external_sources.store prune`).

## How to add a source

1. Check the licence/terms first and record them in `DECISIONS.md` (no scraping behind challenges,
   robots.txt honoured).
2. Write the plugin (`plugins/<source>.py`, `SourcePlugin` in `plugins/base.py`; fill
   `self.failures` / `self.partial_reasons` instead of raising, check `self.stop_requested()`
   between pages) and its field spec `config/<source>_fields.yaml`, with any source-specific
   transforms under `transform_modules`. A per-item detail endpoint is declared with
   `detail_config` (`enrichment/detail.py`).
3. Register it in `store/registry.py` (`SOURCES`: builder, licence, attribution, `with_detail`) and
   add it to `config/store_policy.yaml` (`runner.sources`) and `config/quality_policy.yaml`.
4. Tests: `tests/unit/test_<source>_plugin.py` with a recorded fixture, then
   `store run --source <source> --limit 5` in a dev stack.

## Tests

```bash
python3 -m pytest tests/unit -q          # no database, no network
python3 -m pytest tests/integration      # throwaway pgvector container (docker); skipped without docker, psycopg or the image
```
