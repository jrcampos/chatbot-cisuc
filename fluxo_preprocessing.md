# Fluxo de Código — Pipeline de Preprocessing (offline)

Detalha o pipeline offline completo (`preprocessing/`), que roda numa rede Docker isolada (`preprocessing`, sem ligação ao runtime — ver [`arquitetura_geral.md`](./arquitetura_geral.md)) e produz os artefactos que o [RAG Hybrid Engine](./fluxo_rag_retrieval.md) e o [Evaluator](./fluxo_evaluator_geracao_qa.md) consomem: `.local/raw`, `.local/enriched` e a imagem ChromaDB populada.

Orquestrado ponta-a-ponta por `scripts/run-full-processing.sh` (ou, sem a construção da imagem Chroma, por `scripts/run-preprocessing-data.sh`, o comando do stage `preprocessing` em `dvc.yaml`).

```mermaid
flowchart TD
    Trigger(["run-full-processing.sh<br/>(ou dvc repro, stage 'preprocessing')"]) --> Build["build-preprocessing.sh<br/>docker compose build + up<br/>(containers 'preprocessing' + 'chromadb', rede isolada)"]

    Build --> S1

    subgraph S1["1. Ingestão — scraper_orchestrator.py (run-ingestion.sh)"]
        direction TB
        Cfg["OrchestratorConfig<br/>lê config/scraper.yaml"] --> Sel{"--source static|api|news<br/>(omitido = todos os enabled)"}
        Sel --> Static["StaticContentSource<br/>crawler do site CISUC"]
        Sel --> ApiSrc["APIDataSource<br/>API CISUC (header CISUC_TOKEN)<br/>users / publications / projects"]
        Sel --> News["NewsDataSource<br/>crawler Selenium"]
        Static --> RawOut
        ApiSrc --> RawOut
        News --> RawOut
        RawOut[("WORKSPACE/raw<br/>static/research/*.md<br/>api/api-{users,publications,projects}.json<br/>news/markdown/news_combined.md")]
    end

    S1 --> S2

    subgraph S2["2. Enriquecimento LLM — enhancement.py (run-enhancement.sh, billed)"]
        direction TB
        Groups["process_groups()<br/>1 chamada LLM por grupo → resumo"] --> Rel
        Projs["extract_all_unique_projects()<br/>dedup por project id"] --> Rel
        Users["process_users_and_create_relations()<br/>ThreadPoolExecutor(ENHANCEMENT_MAX_WORKERS)<br/>por utilizador: aliases + short_description<br/>+ research_focus (3 chamadas LLM)"] --> Rel
        Users -.-> Retry{"erro 429 / rate limit /<br/>disconnected / timeout?"}
        Retry -->|"sim, até 3x (backoff 2^n)"| Users
        Retry -->|"não"| Fail(["raise — aborta o stage"])
        Rel["derive_group_project_links()<br/>cruza grupo↔projeto<br/>(sem campo direto nos dados da API)"] --> MD
        MD["generate_enriched_markdowns()<br/>generate_projects_markdown()"] --> EnrOut
        EnrOut[("WORKSPACE/enriched<br/>enriched_api-users.md, enriched_projects_full.md,<br/>enriched_&lt;grupo&gt;.md, relacoes_grupo_investigador.csv")]
    end

    S2 --> S3

    subgraph S3["3. Chunking + Embeddings — populate.py (run-embeddings.sh)"]
        direction TB
        Discover["descobre ficheiros:<br/>TODOS em enriched/ + brutos em raw/<br/>ainda não enriquecidos (exclui raw/api)"] --> Split
        Split["MarkdownHeaderTextSplitter<br/>(# Document_Title · ## Entity_Name ·<br/>### Section · #### Item_Name)<br/>→ RecursiveCharacterTextSplitter<br/>chunk_size=1500, overlap=250"] --> Filter
        Filter["descarta chunks vazios/ruído<br/>('no projects found' / &lt;50 chars)"] --> Classify
        Classify["classifica doc_type pelo nome do ficheiro<br/>(researcher_profile / research_project /<br/>news_article / research_group / general_research_data)<br/>+ injeta prefixo 'Context: ... About: ... Section: ...'"] --> Dedup
        Dedup["ID = MD5(texto + metadata)<br/>remove duplicados exatos"] --> Batch
        Batch["embeddings (OpenAI/Ollama, MODEL_EMBEDDINGS)<br/>em lotes de 200 → vector_store.add_documents()"] --> ChromaPrep[("ChromaDB (preprocessing)<br/>coleção CHROMA_COLLECTION, hnsw:space=cosine")]
    end

    S3 --> S4

    subgraph S4["4. Finalização — finish-preprocessing.sh"]
        direction TB
        Stop["docker compose stop chromadb<br/>(força o flush da base para disco)"] --> Commit["docker commit &lt;container&gt; CHROMADB_FINAL_IMAGE<br/>(tag = SHA do commit, em CI)"]
    end

    S4 --> Runtime

    Runtime[["imagem CHROMADB_FINAL_IMAGE<br/>→ serviço 'chromadb' do runtime<br/>(ver arquitetura_geral.md), consumida só em leitura"]]

    LLM[["LLM Service<br/>OpenAI/Ollama<br/>(ENHANCEMENT_MODEL, MODEL_EMBEDDINGS)"]]
    CISUCWeb[["Site + API CISUC<br/>(scraping, CISUC_TOKEN)"]]
    Users -.-> LLM
    Groups -.-> LLM
    Batch -.-> LLM
    Static -.-> CISUCWeb
    ApiSrc -.-> CISUCWeb
    News -.-> CISUCWeb

    classDef process fill:#1168bd,color:#fff,stroke:#0b4884
    classDef decision fill:#f2b705,color:#1a1a1a,stroke:#b38600
    classDef startend fill:#08427b,color:#fff,stroke:#052e56
    classDef db fill:#1168bd,color:#fff,stroke:#0b4884
    classDef external fill:#8a8a8a,color:#fff,stroke:#6b6b6b
    classDef warn fill:#c0392b,color:#fff,stroke:#8e2a1f
    classDef boundary fill:none,stroke:#999,stroke-dasharray:4 3

    class Build,Cfg,Static,ApiSrc,News,Groups,Projs,Users,Rel,MD,Discover,Split,Filter,Classify,Dedup,Batch,Stop,Commit process
    class Sel,Retry decision
    class Trigger startend
    class RawOut,EnrOut,ChromaPrep db
    class LLM,CISUCWeb,Runtime external
    class Fail warn
    class S1,S2,S3,S4 boundary
```

## Notas

- **Fronteira do DVC**: o stage `preprocessing` de `dvc.yaml` só cobre as fases **1** e **2** (`run-preprocessing-data.sh` = ingestão + enriquecimento) — os `outs` versionados são `.local/raw` e `.local/enriched`. As fases **3** (embeddings) e **4** (commit da imagem) não são rastreadas pelo DVC: a imagem ChromaDB resultante é versionada por **tag de commit Git** em CI (ver "Image tagging" em `CLAUDE.md`), não por `dvc repro`.
- **Cada fase é independentemente re-executável** — só é preciso rodar de novo a fase cujo input mudou (ex.: mudar só o prompt de enriquecimento não exige repetir o scraping).
- **Descoberta de ficheiros na fase 3** prioriza o conteúdo enriquecido: um ficheiro bruto só é processado (sem contexto injetado pelo LLM) se a sua versão `enriched_<nome>.md` ainda não existir — o chunking nunca processa as duas versões do mesmo documento.
- **Falhas de enriquecimento são fail-fast**: um erro não-recuperável (não é 429/rate-limit/disconnect/timeout) propaga e aborta o stage inteiro, em vez de gravar um resumo parcial/alucinado.

---
**Ver também:** [`arquitetura_geral.md`](./arquitetura_geral.md) · [`fluxo_rag_retrieval.md`](./fluxo_rag_retrieval.md) · [`fluxo_evaluator_geracao_qa.md`](./fluxo_evaluator_geracao_qa.md)
