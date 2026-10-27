# Fluxo de Código — Orchestrator

Detalha a lógica interna de `application/Orchestrator/orchestrator.py`: o único serviço que fala com o LLM (ver [`arquitetura_geral.md`](./arquitetura_geral.md)). Dois dos seus endpoints (`/chat` e `/chat/avaliacao`) partilham exatamente o mesmo caminho de recuperação — `/chat/avaliacao` é o que o [Evaluator](./fluxo_evaluator_ragas.md) usa para obter resposta + contextos de uma única execução de produção, em vez de simular a pipeline à parte.

## 1. Arranque — pré-aquecimento dos modelos (`lifespan`)

```mermaid
flowchart TD
    Start(["Container inicia (uvicorn)"]) --> Config["Lê env vars:<br/>LLM_PROVIDER, MODEL_SLM, MODEL_CHAT,<br/>RAG_API_URL, RAG_TOP_K, LLM_TEMPERATURE"]
    Config --> Decision{"LLM_PROVIDER<br/>== 'openai'?"}
    Decision -->|sim| InitOpenAI["slm_extrator = ChatOpenAI(MODEL_SLM, temp=0.0)<br/>llm_principal = ChatOpenAI(MODEL_CHAT, temp=LLM_TEMPERATURE)"]
    Decision -->|"não (ollama)"| InitOllama["slm_extrator = ChatOllama(OLLAMA_URL, MODEL_SLM)<br/>llm_principal = ChatOllama(OLLAMA_URL, MODEL_CHAT)"]
    InitOpenAI --> Lifespan
    InitOllama --> Lifespan
    Lifespan["FastAPI lifespan() assíncrono"] --> WarmSLM["slm_extrator.invoke('Warmup')"]
    WarmSLM --> WarmLLM["llm_principal.invoke('Warmup')"]
    WarmLLM --> Try{"Exceção?"}
    Try -->|sim| Warn["imprime [AVISO], não bloqueia o arranque"]
    Try -->|não| Ok["ambos os modelos carregados"]
    Warn --> Ready(["yield — API pronta a receber pedidos"])
    Ok --> Ready

    classDef process fill:#1168bd,color:#fff,stroke:#0b4884
    classDef decision fill:#f2b705,color:#1a1a1a,stroke:#b38600
    classDef startend fill:#08427b,color:#fff,stroke:#052e56
    classDef warn fill:#c0392b,color:#fff,stroke:#8e2a1f

    class Config,InitOpenAI,InitOllama,Lifespan,WarmSLM,WarmLLM,Ok process
    class Decision,Try decision
    class Start,Ready startend
    class Warn warn
```

> O warm-up só aquece os modelos para a primeira chamada ser rápida — uma falha aqui é só um aviso em log, nunca impede o arranque do serviço.

## 2. Pedido de chat — `/chat` vs `/chat/avaliacao`

Ambos os endpoints chamam a mesma `recuperar_contexto()` e `formatar_contexto()`; divergem só no passo final de geração (streaming vs. invocação única) e no formato de resposta.

```mermaid
flowchart TD
    ReqChat(["POST /chat {pergunta}<br/>(vindo do Gateway/GUI)"]) --> RC
    ReqAval(["POST /chat/avaliacao {pergunta}<br/>(vindo do Evaluator, fase 2 do RAGAS)"]) --> RC

    RC["recuperar_contexto(pergunta)<br/>— ver diagrama 3"] --> FC["formatar_contexto(documentos)<br/>concatena doc['text'] com '\\n\\n'<br/>+ imprime lista de ficheiros (debug)"]
    FC --> Branch{"Qual endpoint?"}
    Branch -->|/chat| Stream["gerador_resposta.stream({context, input=pergunta})<br/>LLM pesado, token a token"]
    Branch -->|/chat/avaliacao| Invoke["gerador_resposta.invoke({context, input=pergunta})<br/>resposta completa, não-streamed"]
    Stream --> OutStream(["StreamingResponse<br/>text/plain → GUI"])
    Invoke --> OutJson(["{alvos, contexts, response}<br/>→ rag_ravluator.py (Fase 2)"])

    LLM[["LLM Service<br/>(OpenAI/Ollama, MODEL_CHAT)"]]
    Stream -.-> LLM
    Invoke -.-> LLM

    classDef process fill:#1168bd,color:#fff,stroke:#0b4884
    classDef decision fill:#f2b705,color:#1a1a1a,stroke:#b38600
    classDef startend fill:#08427b,color:#fff,stroke:#052e56
    classDef external fill:#8a8a8a,color:#fff,stroke:#6b6b6b

    class RC,FC,Stream,Invoke process
    class Branch decision
    class ReqChat,ReqAval,OutStream,OutJson startend
    class LLM external
```

Prompt do gerador final (`system_prompt`, resumido): responder **apenas** com base no `{context}` injetado, nunca alucinar, admitir quando a resposta não está no contexto, responder em PT ou EN conforme a pergunta.

## 3. `recuperar_contexto()` — extração de alvos + fan-out + fusão

O núcleo da orquestração: transforma uma pergunta em texto livre em uma ou mais queries de busca, dispara-as contra a RAG API, e funde os resultados preservando representação de todas as entidades perguntadas.

```mermaid
flowchart TD
    Start(["recuperar_contexto(pergunta)"]) --> Extract["extrator_alvos.invoke({pergunta})<br/>SLM + prompt_extracao, temperature=0.0"]
    Extract --> Rules["REGRA 1: nome próprio → devolve só o nome<br/>REGRA 2: sem nome próprio → 3 conceitos (EN)<br/>REGRA 3: múltiplas entidades → 'A | B | C'"]
    Rules --> Split["alvos = split('|') limpo<br/>(fallback: [alvo_limpo] se vazio)"]

    Split --> P1["pesquisar(pergunta completa)"]
    Split --> LoopA["para cada alvo em alvos:<br/>pesquisar(alvo)"]

    P1 --> Post1["POST RAG_API_URL<br/>{query, top_k=TOP_K}"]
    LoopA --> PostN["POST RAG_API_URL<br/>{query=alvo, top_k=TOP_K}  (uma chamada por alvo)"]

    Post1 --> R1["resultados_pergunta"]
    PostN --> RN["[resultados_alvo_1, resultados_alvo_2, ...]"]

    RN --> MergeA["intercalar_resultados(listas_por_alvo, TOP_K)<br/>round-robin + dedup por (source_file, text)"]
    MergeA --> RA["resultados_alvos"]

    R1 --> MergeFinal
    RA --> MergeFinal["intercalar_resultados([resultados_pergunta, resultados_alvos], TOP_K)<br/>metade dos slots → pergunta completa<br/>metade dos slots → entidades (alvos)"]
    MergeFinal --> End(["(alvos, documentos) — devolvido ao chamador"])

    RAGAPI[("RAG API — /query<br/>ver fluxo_rag_retrieval.md")]
    Post1 -.-> RAGAPI
    PostN -.-> RAGAPI

    classDef process fill:#1168bd,color:#fff,stroke:#0b4884
    classDef io fill:#2f8f4e,color:#fff,stroke:#1f6336
    classDef startend fill:#08427b,color:#fff,stroke:#052e56
    classDef external fill:#8a8a8a,color:#fff,stroke:#6b6b6b
    classDef note fill:none,stroke:#999,stroke-dasharray:2 2,color:#555

    class Extract,Split,MergeA,MergeFinal process
    class Post1,PostN io
    class Start,End startend
    class RAGAPI external
    class Rules note
```

**Por que a pergunta completa *e* os alvos?** Os keywords sozinhos perdem a parte descritiva da pergunta (ex: "o investigador com foco em X") — por isso a pergunta completa também é pesquisada, e os dois conjuntos de resultados são intercalados, não concatenados, para que nenhuma das duas fontes domine o `TOP_K` final.

**`intercalar_resultados()`** (dedup + round-robin, usada duas vezes acima):

```mermaid
flowchart LR
    In["listas: list[list[dict]]<br/>limite: int"] --> Loop["para posicao in 0..max(len(lista)):<br/>para cada lista: pega lista[posicao]"]
    Loop --> Key["chave = (metadata.source_file, text)"]
    Key --> Seen{"chave já vista?"}
    Seen -->|sim| Skip["descarta (duplicado)"]
    Seen -->|não| Add["adiciona a saida[]"]
    Skip --> Loop
    Add --> Cut["corta saida[:limite]"]

    classDef process fill:#1168bd,color:#fff,stroke:#0b4884
    classDef decision fill:#f2b705,color:#1a1a1a,stroke:#b38600

    class In,Loop,Key,Add,Cut process
    class Seen decision
```

## 4. `/query` — proxy simples

A RAG API não tem porta exposta no host (ver `arquitetura_geral.md`), então chamadas externas (ex: scripts de debug) passam por aqui:

```mermaid
flowchart LR
    Caller(["Chamador externo<br/>(fora da rede chatbot_net)"]) --> EP["POST /query {query, top_k}"]
    EP --> Fwd["POST RAG_API_URL<br/>(repassa o corpo tal e qual)"]
    Fwd --> OK{"sucesso?"}
    OK -->|sim| Pass(["devolve o JSON da RAG API"])
    OK -->|"não (RequestException)"| Err(["HTTPException 502<br/>'RAG API indisponível'"])

    classDef process fill:#1168bd,color:#fff,stroke:#0b4884
    classDef decision fill:#f2b705,color:#1a1a1a,stroke:#b38600
    classDef startend fill:#08427b,color:#fff,stroke:#052e56

    class EP,Fwd process
    class OK decision
    class Caller,Pass,Err startend
```

---
**Ver também:** [`arquitetura_geral.md`](./arquitetura_geral.md) · [`fluxo_rag_retrieval.md`](./fluxo_rag_retrieval.md) · [`fluxo_evaluator_ragas.md`](./fluxo_evaluator_ragas.md)
