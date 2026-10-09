# QA Ground Truth Generation

Generates and validates the ground-truth question/answer pairs used as the
reference dataset by the RAG evaluation pipeline in `tests/rag_ravluator.py`.

This directory covers question generation and ground-truth validation. The
evaluation pipeline that consumes the output — running the questions against
the RAG/orchestrator stack and scoring with Ragas (`rag_ravluator.py`
Phases 2 and 3) — is separate work and is not part of this directory.

## Pipeline

```
ChromaDB (local)  ─┐
                   ├─→ extract_corpus.py ──→ corpus/corpus.json
news_combined.md  ─┘                              │
                                                  ▼
                                     generate_ground_truth.py
                                                  │
                                                  ▼
                                    output/ragas_ground_truth.json
                                                  │
                                                  ▼
                                     validate_ground_truth.py
                                                  │
                          ┌───────────────────────┴──────────────────────┐
                          ▼                                              ▼
    output/ragas_ground_truth_validacao.json   output/ragas_ground_truth_sinalizados.json
```

### Layout

```
tests/qa_generation/
├── README.md
├── extract_corpus.py
├── generate_ground_truth.py
├── validate_ground_truth.py
├── corpus/     # extracted corpus        (generated, gitignored)
└── output/     # ground truth + verdicts (generated, gitignored)
```

Only the four source files are versioned. `corpus/` and `output/` hold
generated data and are gitignored; both are recreated by running the
pipeline. The ground-truth files are small enough (~470KB total) to share
directly with whoever needs them.

All three scripts are run through `./scripts/run-evaluation.sh` from the
repository root, which loads their configuration (see "Environment
variables" below). `extract_corpus.py` is free; the other two make billed
OpenAI API calls.

```bash
./scripts/run-evaluation.sh extract                 # one stage
./scripts/run-evaluation.sh extract generate        # several, in the given order
./scripts/run-evaluation.sh extract -- --output <path>   # arguments for a single stage
```

Stages: `extract` (`extract_corpus.py`), `generate`
(`generate_ground_truth.py`), `validate` (`validate_ground_truth.py`) and
`evaluate` (`tests/rag_ravluator.py`, which asks interactively which of its
phases to run).

## Prerequisites

- A local ChromaDB running and reachable at `CHROMA_ANALYSIS_URL`,
  containing the collection named by `CHROMA_COLLECTION`.
  See "Providing the ChromaDB" below.
- `.local/raw/news/markdown/news_combined.md`, produced by
  `./scripts/run-ingestion.sh` (the Articles category reads it directly).
- `secrets/evaluation.env` defining the variables marked as coming from it
  in "Environment variables" below.
- Python: `requests` is the only external dependency of these three scripts.

### Providing the ChromaDB

Either build it from scratch, or run an image that was already published by
the preprocessing CI workflow.

**Build it from scratch** — requires `CISUC_TOKEN` (scraping), an OpenAI key
(enrichment), and access to the lab Ollama instance, which the embedding step
runs through:

```bash
export HOST_UID=$(id -u) HOST_GID=$(id -g)
./scripts/build-preprocessing.sh    # build image, start containers
./scripts/run-ingestion.sh          # scrape  -> .local/raw
./scripts/run-enhancement.sh        # enrich  -> .local/enriched  (billed OpenAI)
./scripts/run-embeddings.sh         # embed   -> populates Chroma (needs Ollama)
./scripts/finish-preprocessing.sh   # commit  -> cisuc-chromadb:local
```

**Serve it on a host port.** The `chromadb` service in
`preprocessing/docker-compose.yaml` publishes no host port — it is only
reachable from inside the compose network — so `extract_corpus.py` cannot
reach it directly. Run the committed image instead:

```bash
docker run -d --name chroma-local -p 8000:8000 cisuc-chromadb:local
```

The same applies to a published image, which is useful for reproducing an
older corpus (the tag is the commit SHA):

```bash
docker run -d --name chroma-pinned -p 8001:8000 \
  ghcr.io/<owner>/cisuc-chromadb:<commit-sha>
CHROMA_ANALYSIS_URL=http://localhost:8001 \
  ./scripts/run-evaluation.sh extract -- --output corpus/corpus-pinned.json
```

Note that a corpus is only fully reproducible from a pinned image for
People, Projects, Groups and Institutional. Articles are read from
`news_combined.md`, which reflects whenever ingestion last ran and is not
pinned by the image.

### Environment variables

The scripts load no files themselves: like the other `scripts/run-*.sh`,
`run-evaluation.sh` exports the files below and the Python code only reads
`os.environ`. All variables are required and have no defaults in code: a
missing one stops the script with a `KeyError` (or, for `OPENAI_API_KEY`, an
explicit error) before any work is done.

| Variable | Defined in | Used by |
|---|---|---|
| `CHROMA_ANALYSIS_URL` | `config/evaluation.env` (versioned) | `extract_corpus.py` |
| `CHROMA_COLLECTION` | `config/chatbot-common.env` (versioned; do not redefine) | `extract_corpus.py` |
| `OPENAI_API_KEY` | `secrets/evaluation.env` | generation, validation |
| `OPENAI_MODEL_EVALUATOR` | `secrets/evaluation.env` | generation, validation |

`config/` files are versioned; `secrets/evaluation.env` is gitignored, so
each person keeps their own copy (in CI, its variables would come from
secrets instead). A `CHROMA_ANALYSIS_URL` exported in the shell takes
precedence over `config/evaluation.env`, which is how the pinned-image example
above points at another database.

The news markdown is always read from `<repo>/.local`, the host directory
the preprocessing containers mount as their workspace.

---

## 1. `extract_corpus.py`

Builds one document per entity and writes `corpus/corpus.json`.

### Why the corpus needs building

`enhancement.py` writes *consolidated* Markdown files — every researcher in a
single `enriched_api-users.md`, every project in a single
`enriched_projects_full.md`. The generator treats each document as one
indivisible unit and truncates it at 3000 characters, so without a
re-splitting step an entire category's questions would be drawn from one or
two entities. This script performs that split.

It reads from ChromaDB rather than the Markdown files because ChromaDB is
what the RAG queries at runtime: a corpus extracted from it is guaranteed to
describe content the system can actually retrieve.

### Sources

| Category | Source | Grouped by |
|---|---|---|
| People | chunks with `type=researcher_profile` | `Entity_Name` metadata |
| Projects | chunks with `type=research_project` | `Entity_Name` metadata |
| Groups | chunks with `type=research_group` | `source_file` metadata |
| Institutional | chunks with `type=general_research_data` | `source_file` metadata |
| Articles | `.local/raw/news/markdown/news_combined.md` | `title:` block |

Articles are the one category not taken from ChromaDB. News chunks there all
share the same generic `source_file`/`Document_Title` from the consolidated
file, and the character-based splitter does not respect article boundaries,
so per-article grouping from chunk metadata is not reliable. The ingestion
Markdown has not been chunked and still carries one
`title:`/`date:`/`url:` block per article, allowing exact reconstruction.

### How it works

1. `get_collection_id()` resolves the `cisuc_rag` collection, then
   `fetch_all_chunks()` pages through the whole collection (500 chunks per
   request) collecting each chunk's text and metadata.
2. `build_corpus()` buckets every chunk by its `type` metadata and groups it
   into entities using the key from the table above.
3. `join_chunks()` reassembles each entity's text, dropping exact duplicate
   chunks and ordering them via `section_order()`: description first, then
   `Projects`, then `Publications`. Publication lists are far larger than the
   rest of a profile, so without this ordering the Projects section would
   fall outside the 3000 characters the generator reads from each document.
4. Per-person project links are collected from chunks carrying both
   `Section=Projects` and `Item_Name`. These populate each person's
   `projects` list and are what `generate_ground_truth.py` uses to build
   genuine person↔project pairs.
5. `parse_articles()` splits the news Markdown on its `title:` blocks.
6. A `_meta` block records the Chroma URL, collection, chunk count,
   extraction timestamp and git commit, so a corpus can be traced back to the
   database build it came from. Consumers read only the category keys and
   ignore `_meta`.

### Usage

```bash
./scripts/run-evaluation.sh extract
```

`--output <path>` writes elsewhere than the default `corpus/corpus.json`.

### Output shape

```json
{
  "people":        [{"name": "...", "text": "...", "projects": ["..."]}],
  "projects":      [{"name": "...", "text": "..."}],
  "groups":        [{"id": "...", "text": "..."}],
  "institutional": [{"id": "...", "text": "..."}],
  "articles":      [{"title": "...", "text": "..."}],
  "_meta":         {"chroma_url": "...", "collection": "...", "chunks": 0,
                    "extracted_at": "...", "git_commit": "..."}
}
```

---

## 2. `generate_ground_truth.py`

Reads `corpus/corpus.json` and writes `output/ragas_ground_truth.json`.

Calls the OpenAI REST API directly (`response_format: json_object`,
temperature 0.2) once per batch, asking for a fixed number of questions per
batch until each category's target is met. `random.seed(42)` makes the whole
run reproducible.

### Sampling targets

`META_PERGUNTAS` sets how many questions each category contributes:

| Category | Target |
|---|---|
| Grupos | 18 |
| Pessoas | 140 |
| Projetos | 120 |
| Institucional | 40 |
| Artigos | 40 |
| Estrategico (Pessoa↔Projeto) | 100 |
| Estrategico (Grupo↔Projeto) | 50 |
| **Total** | **508** |

### Batching

`montar_lotes()` decides which documents go in each batch and how many
questions to request from it. Two strategies:

- **Default** — documents are shuffled and split into batches of
  `max_ficheiros_por_lote`; the target is divided evenly across batches by
  integer division, with the remainder absorbed by the final batch. Used for
  Grupos, Institucional, Artigos and Estrategico, where each document is
  expected to yield several questions.
- **`um_por_documento=True`** — exactly `META_PERGUNTAS[topic]` documents are
  sampled up front, and each batch is asked for exactly one question per
  document it contains. Used for Pessoas and Projetos, where the document
  count far exceeds the target: dividing 120 questions across 65 batches of
  projects would otherwise ask 64 batches for one question each and the last
  batch for the entire remainder.

`montar_lotes()` is a standalone function so that `validate_ground_truth.py`
can replay the exact same batching without making any API calls. Both
scripts rely on it returning the same sequence for the same arguments.

### Prompting

`gerar_qas_em_lotes()` builds one request per batch. Each document is
truncated to 3000 characters and introduced with a `--- Doc: <name> ---`
header. The system prompt casts the model as a test architect, appends a
topic-specific instruction, states the exact number of questions required,
and specifies the JSON response shape. The `topico` field returned by the
model is overwritten with the batch's real topic, because the model tends to
return narrower labels of its own (for example `Eventos` instead of
`Institucional`).

### Strategic questions

Strategic questions require cross-referencing two entities and are built in
two ways:

- **Pessoa↔Projeto** — `montar_pares_pessoa_projeto()` samples people who
  have at least one linked project in the corpus, pairs each with one of
  their real projects, and emits the two documents adjacently. The batching
  call passes `baralhar=False` so the pairing survives into the same prompt,
  meaning the model sees a person and a project that genuinely reference each
  other.
- **Grupo↔Projeto** — uses each group's own text, which already names its
  associated projects, so no artificial pairing is needed.

### Usage

```bash
./scripts/run-evaluation.sh generate
```

Writes `output/ragas_ground_truth.json`, creating the directory if needed.

### Output shape

```json
{
  "qa_pairs": [
    {
      "dificuldade": "facil" | "medio" | "dificil",
      "topico": "Grupos" | "Pessoas" | "Projetos" | "Institucional" | "Artigos" | "Estrategico",
      "question": "...",
      "ground_truth": "..."
    }
  ]
}
```

This is the shape `tests/rag_ravluator.py` expects as input to its Phase 2
and Phase 3. Note that `dificuldade` is only ever prompted as `facil` or
`dificil`; the model occasionally returns `medio` on its own for borderline
questions, which matters if downstream analysis filters strictly on the two
prompted values.

---

## 3. `validate_ground_truth.py`

Fact-checks every generated pair against the source text it came from, and
writes `output/ragas_ground_truth_validacao.json` and
`output/ragas_ground_truth_sinalizados.json`.

Generation produces plausible questions and answers, but nothing in that step
verifies that a `ground_truth` is actually correct according to the text it
was written from. This script re-sends each batch's source text to the same
model, this time as a fact-checker rather than a generator, and records a
verdict per question:

| Verdict | Meaning |
|---|---|
| `suportado` | Fully and correctly grounded in the source text. |
| `parcialmente_suportado` | Partially correct, incomplete, or imprecise. |
| `nao_suportado` | Not grounded in the text — invented, wrong, or unrelated. |
| `erro_validacao` | No parseable verdict returned; treated as needing review, never as passing. |

For anything other than `suportado`, the model proposes a fix in
`sugestao_correcao`. Corrections are **never applied automatically** — the
output is a review list, not a corrected dataset. The script only ever opens
`ragas_ground_truth.json` in read mode.

This validates the ground truth against its own source text. It does not
query the RAG API, the orchestrator, or ChromaDB at validation time, and it
is not a retrieval-quality measurement — that is what `rag_ravluator.py`
Phases 2 and 3 do, using this ground truth as their reference.

### Recovering each question's source text

Generation does not record, per question, which documents produced it. It
does not need to: batching is fully deterministic, and
`ragas_ground_truth.json` is written in batch order. `construir_plano()`
replays the identical sequence of calls — importing `generate_ground_truth`
as a module so it reuses the real `montar_lotes()`, `agrupar_ficheiros()`
and `montar_pares_pessoa_projeto()` — and recovers by position which
documents produced each question, at no API cost.

`construir_plano()` must mirror the call sequence in
`generate_ground_truth.fase_1_gerar_ground_truth()` exactly. Any change there
— a new category, a different order, a different target — has to be made
here too.

`atribuir_pares_aos_lotes()` checks the reconstructed plan against the file
before any API call is made, and raises rather than proceeding if the total
question count or the per-batch topics do not line up.

**A ground-truth file must only be validated against the corpus it was
generated from.** The check above compares totals and topic order, both of
which derive from `META_PERGUNTAS` and are therefore identical for any
corpus — so it will not detect a `corpus.json` that has been regenerated
since. Validating against a different corpus silently attributes questions to
the wrong source documents and produces meaningless verdicts at full API
cost. Keep each `ragas_ground_truth.json` paired with the `corpus.json` that
produced it.

### Resuming

Progress is written to `output/ragas_ground_truth_validacao.json` after every batch,
including a `concluido_ate_lote` marker. If the run is interrupted — Ctrl+C,
an API error, or a daily token limit — re-running the same command continues
from the next unvalidated batch instead of re-paying for completed work.

### Usage

```bash
./scripts/run-evaluation.sh validate
```

### Outputs

- `output/ragas_ground_truth_validacao.json` — every verdict, plus the
  `concluido_ate_lote` resume marker.
- `output/ragas_ground_truth_sinalizados.json` — only the
  `parcialmente_suportado` / `nao_suportado` / `erro_validacao` entries, for
  manual review.

---

## Output files

Generated files live in `corpus/` and `output/` and are gitignored, following
the same convention as `.local/raw` and `.local/enriched`. Only the four
source files in this directory are versioned.

Both `generate_ground_truth.py` and `validate_ground_truth.py` make real,
billed OpenAI calls (roughly 121 batched requests each for the full
508-question set) and run for several minutes. Run them directly in a
terminal you can interrupt.

`generate_ground_truth.py` overwrites `output/ragas_ground_truth.json` on
every run. Redirect the output path before running any test or debug
variant, and keep a backup of a generation run you are happy with before
running further experiments against it.
