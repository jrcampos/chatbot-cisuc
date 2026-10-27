# Fluxo de Código — RAG Hybrid Engine (BM25 + Vetorial + RRF)

Detalha a lógica interna de `application/RAG_CISUC/retrieval.py`, chamado pelo [Orchestrator](./fluxo_orquestrador.md) via `POST /query` e nunca exposto fora da rede `chatbot_net` (ver [`arquitetura_geral.md`](./arquitetura_geral.md)).

## 1. Arranque — ligação ao ChromaDB + construção do índice BM25

```mermaid
flowchart TD
    Start(["Container RAG inicia"]) --> ConfigEmbed{"LLM_PROVIDER<br/>== 'openai'?"}
    ConfigEmbed -->|sim| EmbedOpenAI["embeddings = OpenAIEmbeddings(MODEL_EMBEDDINGS)"]
    ConfigEmbed -->|não| EmbedOllama["embeddings = OllamaEmbeddings(OLLAMA_URL, MODEL_EMBEDDINGS)"]
    EmbedOpenAI --> ConnLoop
    EmbedOllama --> ConnLoop

    ConnLoop["tentativa 1..MAX_RETRYS:<br/>chroma_client = HttpClient(CHROMA_HOST, CHROMA_PORT)<br/>chroma_client.heartbeat()"] --> ConnOk{"Conectou?"}
    ConnOk -->|"não, restam tentativas"| Sleep["sleep(RETRY_DELAY)"] --> ConnLoop
    ConnOk -->|"não, esgotou"| Fail["vector_store = None<br/>retriever_vetorial = None<br/>(degrada para só-BM25, se houver índice)"]
    ConnOk -->|sim| VS["vector_store = Chroma(collection='cisuc_rag', hnsw:space=cosine)<br/>retriever_vetorial = as_retriever(k=10)"]

    VS --> Snapshot["vector_store.get()<br/>snapshot COMPLETO da coleção"]
    Fail --> BuildBM25
    Snapshot --> BuildBM25{"docs_para_bm25<br/>não vazio?"}
    BuildBM25 -->|sim| IndexBM25["retriever_palavras_chave =<br/>BM25Retriever.from_documents(docs, preprocess_func=_tokenizar)<br/>k=10"]
    BuildBM25 -->|não| NoBM25["retriever_palavras_chave = None"]

    IndexBM25 --> Ready(["Motor híbrido pronto<br/>pesos fixos: bm25=0.6, vector=0.4"])
    NoBM25 --> Ready

    ChromaDB[("ChromaDB")]
    ConnLoop -.-> ChromaDB
    Snapshot -.-> ChromaDB

    classDef process fill:#1168bd,color:#fff,stroke:#0b4884
    classDef decision fill:#f2b705,color:#1a1a1a,stroke:#b38600
    classDef startend fill:#08427b,color:#fff,stroke:#052e56
    classDef db fill:#1168bd,color:#fff,stroke:#0b4884
    classDef warn fill:#c0392b,color:#fff,stroke:#8e2a1f

    class EmbedOpenAI,EmbedOllama,VS,Snapshot,IndexBM25,NoBM25 process
    class ConfigEmbed,ConnOk,BuildBM25 decision
    class Start,Ready startend
    class ChromaDB db
    class Fail warn
```

> **Isto corre uma única vez, no arranque do container.** O índice BM25 (e o snapshot que o alimentou) não se atualiza sem reiniciar o serviço — uma reindexação da ChromaDB em produção só se reflete na busca lexical após restart do RAG API.

## 2. `get_relevant_chunks(query, top_k=15)` — busca híbrida, RRF ponderado e rerank

Este é o caminho percorrido a cada pedido do Orchestrator. Tem três estágios: (a) duas buscas independentes, (b) fusão por Weighted Reciprocal Rank Fusion sobre um pool alargado, (c) uma segunda passagem de rescoring lexical sobre esse pool antes do corte final.

```mermaid
flowchart TD
    Start(["get_relevant_chunks(query, top_k=15)"]) --> Clean["normaliza query<br/>(remove aspas simples/duplas, strip)"]

    Clean --> BM25Search["retriever_palavras_chave.invoke(query)<br/>BM25Okapi sobre o índice completo → até 10 docs"]
    Clean --> VecSearch["retriever_vetorial.invoke(query)<br/>kNN por similaridade de cosseno → até 10 docs"]

    BM25Search --> RRF
    VecSearch --> RRF["apply_rrf(bm25_docs, peso=0.6)<br/>apply_rrf(vector_docs, peso=0.4)<br/><br/>score[chave] += peso × 1/(rank+1)<br/>chave = source_file::texto"]

    RRF --> SortRRF["ordena scores RRF, desc"]
    SortRRF --> Pool["pool = top max(top_k×2, 20) candidatos<br/>(ex.: top_k=15 → pool de 30)"]

    Pool --> Rerank["_rerank_pool(pool, query, rrf_scores, top_k)"]
    Rerank --> LocalBM25["BM25Okapi NOVO, só sobre o pool<br/>tokenizado via query.lower().split()"]
    Rerank --> NormRRF["normaliza os scores RRF do pool (min-max)"]
    LocalBM25 --> NormBM25["normaliza os scores BM25 locais (min-max)"]

    NormBM25 --> Blend["combined = 0.5 × bm25_norm + 0.5 × rrf_norm"]
    NormRRF --> Blend
    Blend --> FinalSort["ordena por combined desc<br/>corta a top_k"]
    FinalSort --> Out(["[{text, metadata}, ...] → Orchestrator"])

    ChromaDB[("ChromaDB")]
    BM25Idx[("Índice BM25<br/>em memória")]
    VecSearch -.-> ChromaDB
    BM25Search -.-> BM25Idx

    classDef process fill:#1168bd,color:#fff,stroke:#0b4884
    classDef startend fill:#08427b,color:#fff,stroke:#052e56
    classDef db fill:#1168bd,color:#fff,stroke:#0b4884
    classDef note fill:none,stroke:#999,stroke-dasharray:2 2,color:#555

    class Clean,BM25Search,VecSearch,RRF,SortRRF,Pool,Rerank,LocalBM25,NormRRF,NormBM25,Blend,FinalSort process
    class Start,Out startend
    class ChromaDB,BM25Idx db
```

### Por que RRF seguido de um rerank, e não só RRF

- **Fusão (RRF ponderado)**: combina dois espaços de score não comparáveis (BM25 é frequência-termo, cosseno é similaridade semântica) usando só a *posição* de cada doc em cada lista — robusto, mas grosseiro: o doc certo pode estar fundido mas enterrado no meio do pool.
- **Rescoring**: o pool é alargado para 2×`top_k` (mínimo 20) propositalmente *antes* do corte, para dar à segunda passagem material suficiente para recuperar o doc certo quando ele está "enterrado" no RRF. Essa segunda passagem roda um BM25 local só sobre o pool, mistura 50/50 com o score RRF normalizado, e só então corta para `top_k`.

> **Detalhe de implementação:** o índice BM25 principal usa `_tokenizar()` (regex `\w+`, minúsculas), enquanto o rerank local usa `query.lower().split()` — tokenizadores diferentes nas duas passagens. Documentado aqui porque está no código tal como é, não porque seja necessariamente o comportamento pretendido.

---
**Ver também:** [`arquitetura_geral.md`](./arquitetura_geral.md) · [`fluxo_orquestrador.md`](./fluxo_orquestrador.md)
