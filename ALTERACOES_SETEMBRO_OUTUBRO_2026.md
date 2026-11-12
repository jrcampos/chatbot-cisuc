# Alterações de setembro a outubro de 2026 — CISUC Chatbot

**Data:** 2026-10-06
**Âmbito:** commits de `79201be` (11 de setembro) até `b1bbb8a` (5 de outubro), mais as alterações não commitadas em `application/docker-compose.nginx.yaml`.
**Objetivo:** registar o que mudou em cada camada (ingestão, RAG, orquestrador, infraestrutura, avaliação), porquê, e o que ainda não está validado.

---

## 1. Resumo

| Área | O que mudou | Porquê |
|---|---|---|
| **Ingestão / pré-processamento** | Filtro de boilerplate (menus e rodapés repetidos); extração de texto com separadores; relações grupo ↔ projeto nos Markdown enriquecidos | Menus colados ao conteúdo e falta de ligação entre grupos e projetos |
| **RAG (`retrieval.py`)** | Tokenizador único e case-insensitive para BM25; chave de deduplicação por ficheiro + texto; rerank lexical sobre um pool após RRF | BM25 não apanhava "Bycatch," vs "bycatch"; chunks certos ficavam fora do top-k |
| **Orquestrador** | Extração de múltiplas entidades (REGRA 3) e proibição de inventar placeholders (REGRA 4); pesquisa com a pergunta completa + palavras-chave, intercaladas; prompt de resposta mais direto; endpoint `/chat/avaliacao`; proxy `/query` | Perguntas com duas entidades falhavam; respostas com recusas em conteúdo existente; avaliação usava outro caminho |
| **Infraestrutura** | Timeouts de 300 s no nginx de desenvolvimento; porta 8002 publicada para a avaliação | Perguntas longas davam timeout em 60 s; avaliação precisa de acesso direto ao orquestrador |
| **Avaliação (`tests/rag_ravluator.py`)** | Fase 2 pelo caminho de produção; logs do juiz (JSONL + log do RAGAS); exclusão de erros; coluna `alvos`; URLs e modelos por variável de ambiente | Contextos avaliados não eram os usados em produção; NaN sem explicação |

**Resultado (ver `tests/qa_generation/output/ragas_evaluation_report_2026-10-06.md`):** recusas no Estratégico desceram de 68% para 40%. Os scores subiram, mas a comparação não isola o efeito de cada alteração (a Fase 2 mudou de caminho e de número de contextos).

---

## 2. Ingestão e pré-processamento

### 2.1 Filtro de boilerplate (`58b70ad`)

- **Ficheiro:** `preprocessing/ingestion/cisuc_scraper/extractors/content_extractor.py`, função `strip_corpus_boilerplate(static_dir, min_frequency=0.2)`.
- **Como funciona:** depois de escrever as páginas estáticas, conta em quantas páginas diferentes aparece cada linha exata. Linhas que aparecem em mais de 20% das páginas são removidas de todas.
- **Porquê assim:** em vez de uma lista de strings de navegação fixas (frágil e específica do site), usa um sinal genérico de repetição.
- **Chamada:** `preprocessing/ingestion/cisuc_scraper/data_sources/static_content.py`, logo após o fetch das páginas estáticas.
- **Remoção do filtro antigo:** as strings fixas ("CentreHistoryGoverning", etc.) foram retiradas do `FileGenerator`.
- **Testes:** `tests/unit/test_boilerplate_filter.py` (3 testes). **Não executados nesta sessão**: o `.venv` não tem `bs4`, por isso a importação falha. Ver secção 7.

### 2.2 Extração de texto com separadores (`58b70ad`)

- **Ficheiro:** `content_extractor.py`.
- **Alteração:** `get_text(strip=True)` passou a `get_text(separator=' ', strip=True)` em parágrafos, divs e cabeçalhos.
- **Porquê:** sem separador, o texto de elementos vizinhos ficava colado (ex.: `CentreHistoryGoverning`), o que criava "linhas" falsas e impedia a deteção de boilerplate.

### 2.3 Relações grupo ↔ projeto (`2b0fbb2`)

- **Ficheiro:** `preprocessing/enhancement/enhancement.py`.
- **Função nova:** `derive_group_project_links(enriched_users)`. Cruza os grupos de investigação de cada utilizador com os seus projetos, porque a API não tem essa ligação direta. Devolve dois mapas: grupo → projetos e projeto → grupos.
- **Markdown gerados:**
  - Ficheiros de grupo: secção nova `## Associated Projects` com os títulos dos projetos (ou "No projects found.").
  - Ficheiros de projeto: secção nova `### Research Groups Involved` com as siglas dos grupos.
- **Testes:** `tests/unit/test_enhancement_relations.py` (4 testes, passam).
- **Atenção:** estas alterações só chegam ao RAG depois de correr o enhancement, o embeddings e reiniciar o serviço RAG (o índice BM25 é construído no arranque). Não confirmei se essa reexecução já foi feita para a base de dados atual.

### 2.4 Ignorados no Git (`58b70ad`)

- `.gitignore` passou a ignorar `.python-version`, `pyproject.toml`, `uv.lock` e `CLAUDE.md`.
- **Atenção:** `CLAUDE.md` e `pyproject.toml`/`uv.lock` são ficheiros que a documentação do projeto assume existirem no repositório. Esta regra deve ser revista antes de um commit do estado atual.

---

## 3. RAG (`application/RAG_CISUC/retrieval.py`)

Pipeline atual por pergunta:

1. Limpeza da query (remove aspas).
2. **BM25** (LangChain `BM25Retriever`, top-10), com tokenizador próprio.
3. **Vetorial** (ChromaDB, cosseno, top-10).
4. **Fusão RRF ponderada:** BM25 com peso 0.6, vetorial com peso 0.4. Contribuição de cada resultado = peso × 1/(posição + 1).
5. **Pool:** os primeiros `max(2 × top_k, 20)` candidatos da fusão.
6. **Rerank lexical:** BM25 local sobre o pool (`BM25Okapi`), combinado 50/50 com o score RRF (ambos normalizados entre 0 e 1). Devolve os `top_k`.
7. Formatação em JSON (`text`, `metadata`).

### 3.1 Tokenizador único (`bec4c9b`)

- **Função:** `_tokenizar(texto) = re.findall(r"\w+", texto.lower())`.
- **Problema anterior:** o tokenizador por defeito do LangChain (`str.split`) é sensível a maiúsculas e mantém pontuação. "Bycatch," não batia com "bycatch". Ao consultar o índice, termos com maiúscula ou pontuação falhavam.
- **Uso:** o índice BM25 e o rerank usam a mesma função, para que a tokenização da query e dos documentos seja igual.

### 3.2 Chave de deduplicação (`bec4c9b`)

- **Função:** `_rrf_key(doc) = "<source_file>::<texto>"`.
- **Antes:** a chave era só o texto. Dois chunks com o mesmo texto de ficheiros diferentes colapsavam numa só entrada.

### 3.3 Rerank lexical (`bec4c9b`)

- **Função:** `_rerank_pool(candidates, query, rrf_scores, top_k)`.
- **Motivação:** o relatório de avaliação de setembro mostrou que o chunk certo costuma estar no pool da fusão, mas enterrado. Um reordenamento barato pode subir a precisão sem nova dependência de ML.
- **Limite conhecido:** é uma heurística lexical sobre o pool, não um cross-encoder. Só ajuda se o chunk certo já estiver no pool.
- **Testes:** `tests/unit/test_rerank.py` (6 testes: chaves, ordem de rerank, truncagem, tokenizador). Passam.

---

## 4. Orquestrador (`application/Orchestrator/orchestrator.py`)

### 4.1 Extração de alvos com o SLM (`04d8fdf`)

O prompt de extração (`llama3.1:8b`, via `extrator_alvos`) tem agora quatro regras:

- **REGRA 1:** se houver um nome próprio, devolve só esse nome.
- **REGRA 2:** se não houver, devolve os 3 conceitos mais importantes, de preferência em inglês.
- **REGRA 3 (nova):** se houver mais de uma entidade nomeada, devolve cada uma separada por ` | `.
- **REGRA 4 (nova):** nunca inventar entidades ou placeholders ("Projeto 1", "Investigador"). Só nomes que aparecem na pergunta; se não houver nenhum, aplica a REGRA 2.

**Porquê:** perguntas com duas entidades (pessoa ↔ projeto) geravam uma só pesquisa e perdiam uma delas. A REGRA 4 foi acrescentada porque o SLM passou a inventar placeholders ao aplicar a REGRA 3 (ex.: `Projeto 1 | Projeto 2`).

### 4.2 Recuperação com dois caminhos (`recuperar_contexto`)

Antes: uma pesquisa, com os alvos extraídos. Agora:

1. Pesquisa com a **pergunta completa** (metade dos `TOP_K` slots).
2. Pesquisa **por alvo** (a outra metade, dividida pelos alvos).
3. Intercalação round-robin com deduplicação (`intercalar_resultados`), cortada a `TOP_K`.

**Porquê:** só com as palavras-chave perdia-se a parte descritiva da pergunta ("o investigador que trabalha em X"). Com a pergunta completa, a parte descritiva volta a entrar.

**Limite:** `TOP_K` vem de `RAG_TOP_K` (`config/chatbot.env`, 15).

### 4.3 Prompt de resposta (`system_prompt`)

Antes: "se o contexto não tiver a resposta, diz que não tens essa informação". Isto era interpretado como recusa mesmo quando a resposta estava no contexto.

Agora o prompt diz:

- Responder diretamente quando o contexto tem a resposta, sem pedir desculpa.
- Fazer correspondência com descrições indiretas ("o investigador que trabalha em X").
- Só usar a frase de recusa quando o contexto não tem a resposta.

**Resultado:** recusas no Estratégico desceram de 68% para 40% (ver relatório de 6 de outubro). A recusa residual ainda aparece, sobretudo onde o retrieval não traz a informação.

### 4.4 Endpoints

- **`POST /chat`:** streaming, igual ao anterior na forma. Usa `recuperar_contexto` e `formatar_contexto`.
- **`POST /chat/avaliacao` (novo):** mesmo pipeline, sem streaming. Devolve `{"alvos", "contexts", "response"}`. Usado pela Fase 2 da avaliação para que a resposta e os contextos venham da mesma execução de produção.
- **`POST /query` (novo):** proxy para a API RAG. A API RAG não está exposta fora da rede Docker, por isso scripts no host passam por aqui. Erros de comunicação devolvem 502.

### 4.5 Limitações conhecidas

- **Falhas silenciosas no RAG:** se a API RAG falhar, `pesquisar` devolve lista vazia e só regista `[ERRO]` no log. A resposta sai sem contexto e sem aviso ao utilizador.
- **Timeout de 30 s** nas chamadas à API RAG, fixo no código.
- **Aquecimento dos modelos** (`lifespan`): se o modelo não carregar, o erro só aparece como aviso.

---

## 5. Infraestrutura

### 5.1 Timeouts no nginx de desenvolvimento (`662522e`)

- **Ficheiro:** `application/development-nginx.conf`.
- **Alteração:** `proxy_read_timeout 300s` e `proxy_send_timeout 300s` na rota `/chat`.
- **Porquê:** perguntas complexas ultrapassavam o timeout de 60 s e ficavam sem resposta. Depois desta alteração, os timeouts da avaliação caíram de 49 para 4.

### 5.2 Porta 8002 e proxy de desenvolvimento (não commitado)

- **Ficheiro:** `application/docker-compose.nginx.yaml`.
- **Situação:** o proxy `development_proxy` serve a GUI e `/chat` na porta 80. O orquestrador mantém a porta 8002 publicada, através de `${ORCHESTRATOR_HOST_PORT}:8002`.
- **Porquê:** a avaliação chama `http://127.0.0.1:8002/chat/avaliacao` diretamente. Sem a porta publicada, a Fase 2 falhava com `Connection refused`.
- **Atenção:** a variável `ORCHESTRATOR_HOST_PORT` é obrigatória (o compose falha se não estiver definida).

---

## 6. Avaliação RAGAS (`tests/rag_ravluator.py`)

| Alteração | Commit | Efeito |
|---|---|---|
| Ground truth apontado para `tests/qa_generation/output`, correção do carregamento do `.env` | `79201be` (11 de setembro) | Script encontra o dataset e lê `secrets/evaluation.env` |
| Variáveis de modelo, menu e URLs | `ea58e6c` | Modelo do juiz e URLs configuráveis por variável de ambiente |
| Fase 2 pelo caminho de produção (`/chat/avaliacao`) | `b1bbb8a` | Resposta e contextos vêm da mesma execução de produção (antes eram do `/query` com `top_k=5`) |
| `ORCHESTRATOR_TIMEOUT` (300 s por defeito) | `b1bbb8a` | Timeout deixou de estar fixo em 60 s |
| Exclusão de linhas com erro do orquestrador na Fase 3 | `b1bbb8a` | Linhas sem resposta não entram no cálculo |
| Coluna `alvos` no CSV | `b1bbb8a` | Permite ver que entidades foram extraídas em cada pergunta |
| `RAGAS_LIMITE` | `b1bbb8a` | Avaliar só as primeiras N perguntas, para depuração |
| Logs do juiz: JSONL por chamada (`RegistoChamadasJuiz`) e log do RAGAS | `b1bbb8a` | Permite ver a saída bruta do juiz e os erros por tipo |

**Porquê os logs:** o `context_precision` tinha 48% de NaN sem explicação. Os logs da execução de 6 de outubro mostram que os NaN correspondem a erros de parsing do juiz (ver relatório de 6 de outubro).

**Não alterado:** o dataset de perguntas (`ragas_ground_truth.json`, de 10 de setembro) e o juiz (`gpt-5.4`). Qualquer comparação entre execuções usa o mesmo dataset.

---

## 7. Testes

| Ficheiro | Testes | Estado nesta sessão |
|---|---|---|
| `tests/unit/test_rerank.py` | 6 | Passam |
| `tests/unit/test_enhancement_relations.py` | 4 | Passam |
| `tests/unit/test_boilerplate_filter.py` | 3 | Não executados: o `.venv` não tem `bs4` |
| Outros testes em `tests/unit/` | — | Falham na recolha: `ModuleNotFoundError` (`bs4`, `ingestion`). São dependências do pré-processamento, não do runtime |

Os testes de pré-processamento precisam do ambiente do `preprocessing/requirements.txt`, que não está no `.venv` de testes.

---

## 8. Pendente e não validado

1. **Reexecução do pré-processamento e do índice** (enhancement, embeddings, reinício do RAG) para que o boilerplate e as relações grupo ↔ projeto cheguem ao RAG. Não confirmado.
2. **Pergunta do diretor do CISUC.** A informação está em `cisuc-day.md`, mas o BM25 não associa "diretor" a "Director", e o vetor dilui a pergunta. Não foi corrigido.
3. **`context_precision` com 144 NaN** na execução de 6 de outubro. Causa provável: parsing do juiz (ver relatório de 6 de outubro). Não corrigido.
4. **`PYTHONUNBUFFERED=1`** nos Dockerfiles do orquestrador e do RAG. Sem isto, os `print` não aparecem nos logs do Docker. Não aplicado.
5. **Alterações não commitadas** em `docker-compose.nginx.yaml` (porta 8002).
6. **Regras de `.gitignore`** para `CLAUDE.md`, `pyproject.toml` e `uv.lock`: rever antes de commit.
7. **Variáveis não usadas** em `secrets/evaluation.env` (`RAG_API_URL`, `ORCHESTRATOR_API_URL`): remover ou documentar.
8. **Recusas residuais:** 83 de 508 respostas, sobretudo no Estratégico. Ver secção 5 do relatório de 6 de outubro.
9. **Impacto de cada alteração não isolado.** As mudanças foram feitas em conjunto, e a execução de 6 de outubro mudou o número de contextos. Para isolar efeitos, é preciso repetir a avaliação com uma alteração de cada vez.

---

## 9. Ficheiros de referência

- `tests/qa_generation/output/ragas_evaluation_report.md` (setembro)
- `tests/qa_generation/output/ragas_evaluation_report_atualizado.md` (5 de outubro)
- `tests/qa_generation/output/ragas_evaluation_report_2026-10-06.md` (6 de outubro)
- `tests/qa_generation/output/logs/` (logs da execução de 6 de outubro)
