# Fluxo de Código — Evaluator: Avaliação RAGAS (LLM-as-Judge)

Detalha `tests/rag_ravluator.py`, Fases 2 e 3: a segunda metade do Evaluator, que corre o ground truth produzido em [`fluxo_evaluator_geracao_qa.md`](./fluxo_evaluator_geracao_qa.md) contra a pipeline de produção real do [Orchestrator](./fluxo_orquestrador.md) e pontua os resultados com a framework Ragas.

## 0. Visão geral (Fase 2 → Fase 3)

```mermaid
flowchart LR
    GT[("ragas_ground_truth.json<br/>508 qa_pairs<br/>(ver fluxo_evaluator_geracao_qa.md)")] --> Fase2["Fase 2<br/>fase_2_executar_rag_local()"]
    Orch[["Orchestrator<br/>POST /chat/avaliacao<br/>(ver fluxo_orquestrador.md)"]]
    Fase2 <-->|"1 pedido por pergunta"| Orch
    Fase2 --> TD[("ragas_test_dataset.json")]
    TD --> Fase3["Fase 3<br/>fase_3_avaliar_com_ragas()"]
    Juiz[["Juiz LLM<br/>MODEL_EVALUATOR (OpenAI/Ollama)"]]
    Fase3 <--> Juiz
    Fase3 --> CSV[("ragas_evaluation_results.csv")]

    classDef process fill:#1168bd,color:#fff,stroke:#0b4884
    classDef db fill:#1168bd,color:#fff,stroke:#0b4884
    classDef external fill:#8a8a8a,color:#fff,stroke:#6b6b6b

    class Fase2,Fase3 process
    class GT,TD,CSV db
    class Orch,Juiz external
```

As duas fases rodam separadamente (`--fase 2` ou `--fase 3`) porque a Fase 2 é um gargalo de I/O (chamadas HTTP sequenciais ao Orchestrator) e a Fase 3 é um gargalo de chamadas ao juiz — permite reexecutar a avaliação (Fase 3) sobre o mesmo `ragas_test_dataset.json` sem repetir a coleta.

## 1. Fase 2 — recolha de respostas e contextos (produção real)

```mermaid
flowchart TD
    Start(["fase_2_executar_rag_local()"]) --> Load["lê ragas_ground_truth.json → qa_pairs (508)"]
    Load --> LoopQ["para cada pergunta i (1..508)"]
    LoopQ --> Call["POST ORCHESTRATOR_AVALIACAO_URL<br/>/chat/avaliacao {pergunta}<br/>timeout=ORCHESTRATOR_TIMEOUT"]
    Call --> OK{"sucesso?"}
    OK -->|sim| Parse["dados_resp = {alvos, contexts, response}<br/>— mesma pipeline de produção do /chat<br/>(extração SLM + RRF + geração)"]
    OK -->|"não (timeout, HTTP erro)"| Err["erro = str(exceção)<br/>answer='', contexts=[]"]
    Parse --> Append["acumula {question, answer, contexts, alvos,<br/>erro, ground_truth, dificuldade, topico}"]
    Err --> Append
    Append --> More{"mais perguntas?"}
    More -->|sim| LoopQ
    More -->|não| Write[("ragas_test_dataset.json")]

    classDef process fill:#1168bd,color:#fff,stroke:#0b4884
    classDef decision fill:#f2b705,color:#1a1a1a,stroke:#b38600
    classDef startend fill:#08427b,color:#fff,stroke:#052e56
    classDef db fill:#1168bd,color:#fff,stroke:#0b4884
    classDef external fill:#8a8a8a,color:#fff,stroke:#6b6b6b
    classDef warn fill:#c0392b,color:#fff,stroke:#8e2a1f

    class Load,Parse,Append process
    class OK,More decision
    class Start startend
    class Write db
    class Call external
    class Err warn
```

> **Por que via `/chat/avaliacao` e não chamando a RAG API diretamente:** resposta e contextos vêm da **mesma chamada** ao pipeline de produção — se a avaliação usasse uma reimplementação paralela da recuperação, um bug exclusivo dessa reimplementação poderia inflar ou deflacionar scores sem refletir o sistema real.

## 2. Fase 3 — avaliação RAGAS (LLM como juiz)

```mermaid
flowchart TD
    Start(["fase_3_avaliar_com_ragas()"]) --> Load["lê ragas_test_dataset.json"]
    Load --> Filter["remove linhas com erro<br/>(falha do orquestrador na Fase 2 — não avaliáveis)"]
    Filter --> Sample{"RAGAS_LIMITE<br/>definido?"}
    Sample -->|"sim (depuração)"| Strat["amostra_estratificada()<br/>proporcional por 'topico', seed=42<br/>(substitui corte cru dados[:N], que vesaria<br/>só a primeira categoria do ground truth)"]
    Sample -->|"não (corrida oficial)"| Full["usa as ~508 linhas completas"]
    Strat --> Build
    Full --> Build["monta HF Dataset<br/>{question, answer, contexts, ground_truth}"]

    Build --> Logs["ativar_logs_juiz()<br/>logger 'ragas' → ficheiro .log<br/>callback RegistoChamadasJuiz → .jsonl<br/>(1 linha por chamada ao juiz: ok/erro + tempo)"]
    Logs --> Eval["ragas.evaluate(dataset, metrics, llm=juiz_llm_ragas,<br/>embeddings=juiz_embeddings, run_config=JUIZ_RUN_CONFIG)"]

    Eval --> M1["context_precision_topk<br/>(LLMContextPrecisionWithReference customizada:<br/>só os CONTEXT_PRECISION_TOP_K contextos<br/>melhor rankeados, não os 15 de produção)"]
    Eval --> M2["context_recall"]
    Eval --> M3["faithfulness"]
    Eval --> M4["answer_relevancy<br/>(strictness=ANSWER_RELEVANCY_STRICTNESS)"]

    M1 --> Judge
    M2 --> Judge
    M3 --> Judge
    M4 --> Judge
    Judge[["Juiz LLM<br/>+ DiskCacheBackend<br/>(evita repetir chamadas idênticas<br/>pergunta+contexto+resposta)"]] --> Scores["scores por linha × métrica<br/>(NaN quando o juiz falha a parsear)"]

    Scores --> Summary["resumir_juiz()<br/>conta erros do juiz + linhas NaN por métrica"]
    Summary --> Cols["adiciona colunas dificuldade/topico/alvos"]
    Cols --> CSV[("ragas_evaluation_results.csv")]

    classDef process fill:#1168bd,color:#fff,stroke:#0b4884
    classDef decision fill:#f2b705,color:#1a1a1a,stroke:#b38600
    classDef startend fill:#08427b,color:#fff,stroke:#052e56
    classDef db fill:#1168bd,color:#fff,stroke:#0b4884
    classDef external fill:#8a8a8a,color:#fff,stroke:#6b6b6b

    class Load,Filter,Strat,Full,Build,Logs,M1,M2,M3,M4,Scores,Summary,Cols process
    class Sample decision
    class Start startend
    class CSV db
    class Judge external
```

### Por que `context_precision_topk` tem nome próprio

A métrica `context_precision` original do Ragas avalia **todos** os contextos recuperados — aqui seriam os 15 de produção (`RAG_TOP_K`), o que custa uma chamada ao juiz por contexto por pergunta. `ContextPrecisionTopK` é uma subclasse que corta `retrieved_contexts` para os `CONTEXT_PRECISION_TOP_K` melhor rankeados antes de avaliar — o maior item de custo da avaliação — sem alterar os contextos vistos por `faithfulness`/`context_recall`, nem o `RAG_TOP_K=15` de produção. Como o valor computado (precisão@top_k) não é comparável à métrica original, tem nome próprio em vez de reusar `context_precision`.

### Resiliência do juiz

`RunConfig(max_retries=RAGAS_MAX_RETRIES)` substitui o padrão do Ragas (10 retries) — falhas de parsing de um juiz local (Ollama) tendem a ser deterministicas, então repetir até 10× só desperdiça chamadas sem nunca salvar a linha. O `DiskCacheBackend` é o único item do pipeline sem nenhuma contrapartida de qualidade: só evita refazer trabalho (chamadas idênticas) já pago em corridas anteriores.

---
**Ver também:** [`arquitetura_geral.md`](./arquitetura_geral.md) · [`fluxo_orquestrador.md`](./fluxo_orquestrador.md) · [`fluxo_evaluator_geracao_qa.md`](./fluxo_evaluator_geracao_qa.md)
