# Fluxo de Código — Evaluator: Geração do Ground Truth (QA)

Detalha `tests/qa_generation/{extract_corpus,generate_ground_truth,validate_ground_truth}.py`: a primeira metade do Evaluator, que produz o dataset de referência consumido pela avaliação RAGAS (ver [`fluxo_evaluator_ragas.md`](./fluxo_evaluator_ragas.md)). Corre offline, contra uma ChromaDB local, e faz chamadas reais e faturadas à OpenAI (exceto `extract_corpus.py`, que é grátis).

## 0. Visão geral do pipeline

```mermaid
flowchart LR
    Chroma[("ChromaDB local<br/>coleção cisuc_rag")] --> Extract["extract_corpus.py"]
    News[/"news_combined.md<br/>(run-ingestion.sh --source news)"/] --> Extract
    Extract --> Corpus[("corpus.json<br/>people/projects/groups/<br/>institutional/articles")]

    Corpus --> Generate["generate_ground_truth.py<br/>(billed, OpenAI)"]
    Generate --> GT[("ragas_ground_truth.json<br/>508 qa_pairs")]

    GT --> Validate["validate_ground_truth.py<br/>(billed, OpenAI)"]
    Corpus -.->|"replay determinístico<br/>do batching (sem custo)"| Validate
    Validate --> Val[("ragas_ground_truth_validacao.json")]
    Validate --> Flag[("ragas_ground_truth_sinalizados.json")]

    GT ==>|"usado como entrada"| RAGAS["ver fluxo_evaluator_ragas.md"]

    classDef process fill:#1168bd,color:#fff,stroke:#0b4884
    classDef db fill:#1168bd,color:#fff,stroke:#0b4884
    classDef io fill:#2f8f4e,color:#fff,stroke:#1f6336
    classDef external fill:#8a8a8a,color:#fff,stroke:#6b6b6b

    class Extract,Generate,Validate process
    class Chroma,Corpus,GT,Val,Flag db
    class News io
    class RAGAS external
```

## 1. `extract_corpus.py` — corpus por entidade

Os ficheiros que o `enhancement.py` escreve são **consolidados** (um único `enriched_api-users.md` com todos os investigadores); o gerador trunca cada documento a 3000 caracteres, então sem re-divisão todas as perguntas de "Pessoas" sairiam de uma ou duas entidades. Este script reconstrói um documento por entidade a partir dos chunks da própria ChromaDB — a mesma base que o RAG consulta em runtime, garantindo que as perguntas geradas têm resposta no que o sistema consegue efetivamente recuperar.

```mermaid
flowchart TD
    Start(["main()"]) --> GetCol["get_collection_id()<br/>GET /collections → procura 'cisuc_rag'"]
    GetCol --> Fetch["fetch_all_chunks(id)<br/>pagina POST .../get (limit=500) até esgotar"]
    Fetch --> Build["build_corpus(chunks)"]

    Build --> Bucket{"metadata.type"}
    Bucket -->|researcher_profile| People["agrupa por Entity_Name<br/>+ regista Section=Projects & Item_Name<br/>→ person_projects[nome]"]
    Bucket -->|research_project| Projects["agrupa por Entity_Name"]
    Bucket -->|research_group| Groups["agrupa por source_file"]
    Bucket -->|general_research_data| Inst["agrupa por source_file"]
    Bucket -->|news_article| Ignore["ignorado aqui<br/>(artigos vêm do markdown, não da Chroma)"]

    People --> Join["join_chunks() por entidade:<br/>ordena (descrição, Projects, Publications)<br/>remove duplicados exatos"]
    Projects --> Join
    Groups --> Join
    Inst --> Join

    Join --> Articles["parse_articles(news_combined.md)<br/>split regex em blocos '---\\ntitle:'"]
    Articles --> Meta["adiciona _meta<br/>(chroma_url, collection, chunks, git_commit)"]
    Meta --> Write[("corpus.json")]

    classDef process fill:#1168bd,color:#fff,stroke:#0b4884
    classDef decision fill:#f2b705,color:#1a1a1a,stroke:#b38600
    classDef startend fill:#08427b,color:#fff,stroke:#052e56
    classDef db fill:#1168bd,color:#fff,stroke:#0b4884

    class GetCol,Fetch,Build,People,Projects,Groups,Inst,Join,Articles,Meta process
    class Bucket decision
    class Start startend
    class Write db
```

**Por que artigos não vêm da Chroma:** os chunks de notícias partilham todos o mesmo `source_file`/`Document_Title` do ficheiro consolidado, e o splitting por número de caracteres não respeita fronteiras entre artigos. O markdown de ingestão (pré-chunking) mantém um bloco `title:`/`date:`/`url:` exato por artigo.

## 2. `generate_ground_truth.py` — amostragem, batching e geração (508 perguntas)

```mermaid
flowchart TD
    Start(["fase_1_gerar_ground_truth()"]) --> Seed["random.seed(42) — torna a corrida inteira reprodutível"]
    Seed --> Load["agrupar_ficheiros()<br/>lê corpus.json → categorias (nome, texto)"]

    Load --> Cat1["Grupos — meta 18, lote=2"]
    Cat1 --> Cat2["Pessoas — meta 140, lote=6, um_por_documento"]
    Cat2 --> Cat3["Projetos — meta 120, lote=6, um_por_documento"]
    Cat3 --> Cat4["Institucional — meta 40, lote=4"]
    Cat4 --> Cat5["Artigos — meta 40, lote=6"]
    Cat5 --> Pairs["montar_pares_pessoa_projeto(n=50)<br/>amostra pessoas com projeto real associado,<br/>escolhe 1 projeto por pessoa, pares adjacentes"]
    Pairs --> Cat6["Estrategico Pessoa↔Projeto — meta 100<br/>baralhar=False (preserva o par no mesmo lote)"]
    Cat6 --> Cat7["Estrategico Grupo↔Projeto — meta 50<br/>usa o texto do próprio grupo (já cita os projetos)"]
    Cat7 --> Write[("ragas_ground_truth.json<br/>{qa_pairs: [{dificuldade, topico, question, ground_truth}]}")]

    classDef process fill:#1168bd,color:#fff,stroke:#0b4884
    classDef startend fill:#08427b,color:#fff,stroke:#052e56
    classDef db fill:#1168bd,color:#fff,stroke:#0b4884

    class Seed,Load,Cat1,Cat2,Cat3,Cat4,Cat5,Pairs,Cat6,Cat7 process
    class Start startend
    class Write db
```

Cada `Cat*` acima invoca a mesma subrotina `gerar_qas_em_lotes()`:

```mermaid
flowchart TD
    Start(["gerar_qas_em_lotes(topico, ficheiros, meta)"]) --> ML["montar_lotes()"]
    ML --> Strategy{"um_por_documento?"}
    Strategy -->|"sim (Pessoas/Projetos)"| S1["amostra exatamente `meta` documentos<br/>1 pergunta por doc por lote<br/>(evita 64 lotes de 1 pergunta + resto desproporcional)"]
    Strategy -->|"não"| S2["baralha (se baralhar=True), divide em lotes de N<br/>meta ÷ nº lotes, resto absorvido pelo último lote"]
    S1 --> ForEach
    S2 --> ForEach

    ForEach["para cada lote:<br/>contexto += '--- Doc: nome ---' + texto[:3000]"] --> LLMCall["invocar_llm()<br/>POST api.openai.com/v1/chat/completions<br/>response_format=json_object, temperature=0.2"]
    LLMCall --> Force["força par['topico'] = topico real<br/>(modelo às vezes devolve rótulo mais específico, ex. 'Eventos')"]
    Force --> Accumulate["acumula em resultados[]"]
    Accumulate --> More{"mais lotes?"}
    More -->|sim| ForEach
    More -->|não| End(["devolve resultados[]"])

    LLM[["LLM Evaluator<br/>(OPENAI_MODEL_EVALUATOR)"]]
    LLMCall -.-> LLM

    classDef process fill:#1168bd,color:#fff,stroke:#0b4884
    classDef decision fill:#f2b705,color:#1a1a1a,stroke:#b38600
    classDef startend fill:#08427b,color:#fff,stroke:#052e56
    classDef external fill:#8a8a8a,color:#fff,stroke:#6b6b6b

    class ML,S1,S2,ForEach,LLMCall,Force,Accumulate process
    class Strategy,More decision
    class Start,End startend
    class LLM external
```

## 3. `validate_ground_truth.py` — fact-checking retomável

Não chama o RAG API, o Orchestrator nem a Chroma em runtime — valida cada `ground_truth` **contra o próprio texto-fonte que o gerou**, antes de esse ground truth ser usado como referência na avaliação RAGAS.

```mermaid
flowchart TD
    Start(["fase_1b_validar_ground_truth()"]) --> Plan["construir_plano()<br/>replica EXATAMENTE a sequência random.seed(42)<br/>de generate_ground_truth (sem chamar a OpenAI)"]
    Plan --> Load["carregar_qa_pairs()<br/>lê ragas_ground_truth.json"]
    Load --> Assign["atribuir_pares_aos_lotes(plano, qa_pairs)<br/>fatia qa_pairs pela ordem do plano"]

    Assign --> Check{"total e tópicos<br/>batem certo?"}
    Check -->|não| Abort(["RuntimeError — aborta<br/>(corpus.json ou generate_ground_truth.py<br/>mudaram desde a geração)"])
    Check -->|sim| Progress["carregar_progresso()<br/>lê concluido_ate_lote de execuções anteriores"]

    Progress --> LoopB{"para cada lote<br/>a partir de concluido_ate_lote"}
    LoopB --> ValBatch["validar_lote()<br/>reenvia texto-fonte + perguntas/respostas<br/>ao modelo, como 'Verificador de Factos'"]
    ValBatch --> Verdict["veredito por pergunta:<br/>suportado / parcialmente_suportado /<br/>nao_suportado / erro_validacao<br/>(+ sugestao_correcao, exceto suportado)"]
    Verdict --> Save["grava progresso APÓS CADA LOTE<br/>(retomável: Ctrl+C, erro de API, rate limit)"]
    Save --> LoopB

    LoopB -->|todos concluídos| Split["separa sinalizados<br/>(tudo com veredito != suportado)"]
    Split --> Out1[("ragas_ground_truth_validacao.json<br/>+ concluido_ate_lote")]
    Split --> Out2[("ragas_ground_truth_sinalizados.json<br/>só para revisão manual")]

    classDef process fill:#1168bd,color:#fff,stroke:#0b4884
    classDef decision fill:#f2b705,color:#1a1a1a,stroke:#b38600
    classDef startend fill:#08427b,color:#fff,stroke:#052e56
    classDef db fill:#1168bd,color:#fff,stroke:#0b4884
    classDef warn fill:#c0392b,color:#fff,stroke:#8e2a1f

    class Plan,Load,Assign,Progress,ValBatch,Verdict,Save,Split process
    class Check,LoopB decision
    class Start startend
    class Out1,Out2 db
    class Abort warn
```

> Correções nunca são aplicadas automaticamente — a saída é uma lista de revisão, não um dataset corrigido. O script só abre `ragas_ground_truth.json` em modo leitura.

---
**Ver também:** [`arquitetura_geral.md`](./arquitetura_geral.md) · [`fluxo_evaluator_ragas.md`](./fluxo_evaluator_ragas.md)
