"""
CISUC RAG Evaluation — Fase 1: Geração do Ground Truth.

Gera perguntas e respostas de referência (ground truth) por lotes, a partir
do corpus por entidade em tests/qa_generation/corpus/corpus.json (produzido
por extract_corpus.py), cobrindo seis fontes: Grupos, Pessoas, Projetos,
Institucional, Artigos e perguntas Estratégicas que cruzam pessoas/projetos
e grupos/projetos.

A lógica de geração segue o mesmo desenho do protótipo em
tests/rag_ravluator.py: geração em lotes por tópico (gerar_qas_em_lotes) até
atingir a meta de perguntas de cada categoria, truncagem de 3000 caracteres
por documento no lote, o mesmo prompt base ("Arquiteto de Testes de
Qualidade") com uma instrução por tópico, e o mesmo modelo/temperatura
(configuráveis via secrets/evaluation.env). As metas por categoria
(META_PERGUNTAS) e as instruções de cada tópico foram ajustadas para este
corpus — ver comentários junto a cada valor.

Diferença técnica face a tests/rag_ravluator.py: chama a API da OpenAI via
REST/requests em vez de langchain_openai, com response_format json_object
para obter o mesmo JSON que ali é validado por JsonOutputParser. Assim o
único requisito externo destes scripts é `requests`.

Output: tests/qa_generation/output/ragas_ground_truth.json
({"qa_pairs": [...]}), no mesmo formato consumido pelas Fases 2 e 3 de
tests/rag_ravluator.py. O caminho é absoluto (ver OUTPUT_DIR), pelo que não
depende do diretório a partir do qual o script é chamado.

Uso:
    python3 tests/qa_generation/generate_ground_truth.py
"""

import os
import json
import random
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent.parent

# Carregar variáveis de ambiente (adaptação: secrets/evaluation.env do layout atual)
def carregar_env(caminho: Path) -> None:
    if not caminho.exists():
        return
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        os.environ.setdefault(chave.strip(), valor.strip())

carregar_env(BASE_DIR / "secrets" / "evaluation.env")

# Configurações de API
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL_EVALUATOR", "gpt-5.4")

# Ficheiros de Estado (caminhos absolutos: não dependem do diretório de onde
# o script é chamado, e mantêm os outputs dentro de tests/qa_generation/)
OUTPUT_DIR = SCRIPT_DIR / "output"
GROUND_TRUTH_FILE = OUTPUT_DIR / "ragas_ground_truth.json"
CORPUS_FILE = SCRIPT_DIR / "corpus" / "corpus.json"

# ========================================================
# CONFIGURAÇÃO DE AMOSTRAGEM (ALVO: 508 PERGUNTAS)
#
# Grupos: apenas 6 grupos existem no corpus, por isso a meta segue
# diretamente a especificação original (3 perguntas por grupo) em vez de um
# múltiplo do valor de tests/rag_ravluator.py.
#
# Estrategico: dividido em dois sub-alvos (ver fase_1_gerar_ground_truth),
# com um total bem acima do dobro de tests/rag_ravluator.py (30), para
# priorizar esta categoria — é a que mais exige cruzar informação de duas
# fontes, o cenário onde os modelos servidos via Ollama tendem a falhar mais.
#
# Pessoas, Projetos, Institucional: aproximadamente o dobro dos valores de
# tests/rag_ravluator.py (70, 60, 20), para dar mais material de avaliação
# sem ir até 1 pergunta por entidade (153 pessoas / 386 projetos).
#
# Artigos é uma categoria nova: não existia em tests/rag_ravluator.py.
# ========================================================
META_PERGUNTAS = {
    "Grupos": 18,
    "Pessoas": 140,
    "Projetos": 120,
    "Institucional": 40,
    "Artigos": 40,
    "Estrategico_PessoaProjeto": 100,
    "Estrategico_GrupoProjeto": 50,
}

if not OPENAI_API_KEY:
    print("[ERRO] OPENAI_API_KEY não encontrada em secrets/evaluation.env!")
    exit()


def invocar_llm(system_prompt: str, human_prompt: str) -> dict:
    """Equivalente REST da cadeia prompt | juiz_llm | JsonOutputParser do original."""
    resposta = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        json={
            "model": OPENAI_MODEL,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": human_prompt},
            ],
        },
        timeout=300,
    )
    resposta.raise_for_status()
    conteudo = resposta.json()["choices"][0]["message"]["content"]
    return json.loads(conteudo)


def agrupar_ficheiros():
    """Carrega o corpus e organiza os documentos por categoria.

    Os "ficheiros" são os documentos por entidade do corpus.json (nome +
    texto), já categorizados pelos metadados da Chroma (pessoas, projetos,
    grupos, institucional) ou pela estrutura do markdown de notícias
    (artigos). Devolve também o corpus completo, para construir os pares
    pessoa<->projeto usados nas perguntas estratégicas.
    """
    with CORPUS_FILE.open("r", encoding="utf-8") as f:
        corpus = json.load(f)

    categorias = {
        "grupos": [(g["id"], g["text"]) for g in corpus["groups"]],
        "pessoas": [(p["name"], p["text"]) for p in corpus["people"]],
        "projetos": [(p["name"], p["text"]) for p in corpus["projects"]],
        "institucional": [(i["id"], i["text"]) for i in corpus["institutional"]],
        "artigos": [(a["title"], a["text"]) for a in corpus.get("articles", [])],
    }
    return categorias, corpus


def montar_pares_pessoa_projeto(corpus, n_pares):
    """Constrói pares (pessoa, projeto) com ligação real, via corpus['people'][i]['projects'].

    Cada pessoa no corpus guarda a lista de projetos em que a Chroma a
    associou (metadado Section=Projects + Item_Name em extract_corpus.py).
    Amostra pessoas com pelo menos um projeto associado e, para cada uma,
    escolhe um desses projetos ao acaso — garantindo que o texto da pessoa e
    o texto do projeto no mesmo lote falam realmente um do outro, em vez de
    depender do modelo inferir uma relação entre documentos não relacionados.
    """
    projetos_por_nome = {p["name"]: p["text"] for p in corpus["projects"]}
    pessoas_com_projetos = [p for p in corpus["people"] if p["projects"]]

    amostra = random.sample(pessoas_com_projetos, min(n_pares, len(pessoas_com_projetos)))

    pares = []
    for pessoa in amostra:
        nome_projeto = random.choice(pessoa["projects"])
        texto_projeto = projetos_por_nome.get(nome_projeto)
        if texto_projeto is None:
            continue
        pares.append((pessoa["name"], pessoa["text"]))
        pares.append((nome_projeto, texto_projeto))
    return pares


def montar_lotes(ficheiros, meta_perguntas, max_ficheiros_por_lote, baralhar=True, um_por_documento=False):
    """Constrói a mesma sequência de lotes (documentos + quantidade a pedir) usada em gerar_qas_em_lotes.

    Extraído para uma função à parte, e não implementado apenas dentro de
    gerar_qas_em_lotes, para que validate_ground_truth.py possa reconstruir
    exatamente que documentos geraram cada pergunta guardada em
    ragas_ground_truth.json — sem chamar a OpenAI outra vez — chamando esta
    mesma função com os mesmos argumentos e o mesmo random.seed(42). Ambos
    os scripts dependem desta função devolver sempre a mesma sequência para
    os mesmos argumentos; qualquer alteração aqui tem de manter isso.

    Devolve uma lista de (lote, qtd_pedir), onde lote é a lista de
    (nome_doc, texto_doc) desse lote.
    """
    if not ficheiros or meta_perguntas <= 0:
        return []

    ficheiros = list(ficheiros)
    if um_por_documento:
        ficheiros = random.sample(ficheiros, min(meta_perguntas, len(ficheiros)))
    elif baralhar:
        random.shuffle(ficheiros)

    lotes_docs = [ficheiros[i:i + max_ficheiros_por_lote] for i in range(0, len(ficheiros), max_ficheiros_por_lote)]

    # Quantas perguntas vamos pedir por cada lote?
    perguntas_por_lote = max(1, meta_perguntas // len(lotes_docs)) if lotes_docs else meta_perguntas
    perguntas_geradas = 0

    resultado = []
    for i, lote in enumerate(lotes_docs):
        if um_por_documento:
            qtd_pedir = len(lote)
        else:
            faltam = meta_perguntas - perguntas_geradas
            if faltam <= 0:
                break
            # No último lote, pedimos exatamente o resto que falta para bater certo
            qtd_pedir = faltam if i == len(lotes_docs) - 1 else min(perguntas_por_lote, faltam)
        resultado.append((lote, qtd_pedir))
        perguntas_geradas += qtd_pedir

    return resultado


def gerar_qas_em_lotes(topico, ficheiros, instrucao_base, meta_perguntas, max_ficheiros_por_lote=4, baralhar=True, um_por_documento=False):
    """
    Divide os ficheiros em lotes menores (montar_lotes) e faz múltiplos
    pedidos à OpenAI até atingir a meta exata de perguntas pedida para este
    tópico. (Lógica idêntica a tests/rag_ravluator.py, exceto quando
    um_por_documento=True — ver montar_lotes.)

    baralhar=False preserva a ordem da lista recebida: usado para os pares
    pessoa<->projeto de montar_pares_pessoa_projeto, onde a posição de cada
    entrada (pessoa seguida do seu projeto) importa para que ambas caiam no
    mesmo lote.
    """
    resultados = []
    lotes = montar_lotes(ficheiros, meta_perguntas, max_ficheiros_por_lote, baralhar, um_por_documento)

    for i, (lote, qtd_pedir) in enumerate(lotes):
        contexto = ""
        for nome_doc, texto_doc in lote:
            # Limite seguro de 3000 caracteres por ficheiro no lote
            contexto += f"\n\n--- Doc: {nome_doc} ---\n" + texto_doc[:3000]

        instrucao_quantidade = (
            f"Gera EXATAMENTE {qtd_pedir} perguntas, uma por cada documento listado acima (não repitas o mesmo documento)."
            if um_por_documento
            else f"Gera EXATAMENTE {qtd_pedir} perguntas."
        )

        system_prompt = f"""És um Arquiteto de Testes de Qualidade.
            Baseado EXCLUSIVAMENTE no texto fornecido, {instrucao_base}
            {instrucao_quantidade}

            Devolve ESTRITAMENTE em formato JSON com esta estrutura:
            {{
                "qa_pairs": [
                    {{
                        "dificuldade": "facil",
                        "topico": "{topico}",
                        "question": "...",
                        "ground_truth": "..."
                    }}
                ]
            }}
            """

        print(f" -> [{topico}] A processar Lote {i+1}/{len(lotes)} (a pedir {qtd_pedir} QAs)...")
        try:
            res = invocar_llm(system_prompt, contexto)
            pares = res.get("qa_pairs", [])
            # O modelo por vezes devolve um "topico" mais específico do que o
            # pedido no prompt (ex.: "Eventos", "História" em vez de
            # "Institucional") — força sempre o tópico real do lote, em vez
            # de confiar no que o modelo escreveu nesse campo.
            for par in pares:
                par["topico"] = topico
            resultados.extend(pares)
        except Exception as e:
            print(f"   [ERRO] Falha no lote {i+1}: {e}")

    return resultados


def fase_1_gerar_ground_truth():
    """Gera as perguntas iterando por lotes para cobrir todas as metas estabelecidas."""
    print("\n--- FASE 1: GERAÇÃO DO GROUND TRUTH (EM LOTES) ---")
    categorias, corpus = agrupar_ficheiros()

    print(f"Ficheiros Categorizados Corretamente: {len(categorias['grupos'])} Grupos, {len(categorias['pessoas'])} Pessoas, {len(categorias['projetos'])} Projetos, {len(categorias['institucional'])} Institucional, {len(categorias['artigos'])} Artigos.")

    random.seed(42)
    dataset_final = []

    # 1. GRUPOS (3 perguntas por grupo, 6 grupos = 18; ver META_PERGUNTAS)
    instrucao = "Foca-te nas áreas de pesquisa principais, investigadores associados e objetivos gerais destes grupos de investigação."
    dataset_final.extend(gerar_qas_em_lotes("Grupos", categorias['grupos'], instrucao, META_PERGUNTAS["Grupos"], max_ficheiros_por_lote=2))

    # 2. PESSOAS (1 pergunta por pessoa amostrada, sem restos desproporcionais)
    instrucao = "Foca-te exclusivamente nos interesses de pesquisa, cargos que ocupam ou publicações dos investigadores mencionados. Cobre várias pessoas diferentes."
    dataset_final.extend(gerar_qas_em_lotes("Pessoas", categorias['pessoas'], instrucao, META_PERGUNTAS["Pessoas"], max_ficheiros_por_lote=6, um_por_documento=True))

    # 3. PROJETOS (1 pergunta por projeto amostrado, sem restos desproporcionais)
    instrucao = "Foca-te no objetivo, financiamento, acrónimo ou consórcio dos projetos mencionados. Cobre projetos diferentes."
    dataset_final.extend(gerar_qas_em_lotes("Projetos", categorias['projetos'], instrucao, META_PERGUNTAS["Projetos"], max_ficheiros_por_lote=6, um_por_documento=True))

    # 4. INSTITUCIONAL
    instrucao = "Gera perguntas genéricas sobre o centro CISUC (ex: história, localização, estatísticas, laboratórios)."
    dataset_final.extend(gerar_qas_em_lotes("Institucional", categorias['institucional'], instrucao, META_PERGUNTAS["Institucional"], max_ficheiros_por_lote=4))

    # 5. ARTIGOS
    instrucao = "Foca-te no assunto principal de cada notícia (ex: evento, prémio, tese, publicação, palestra) e na(s) pessoa(s) ou projeto(s) envolvidos. Cobre artigos diferentes."
    dataset_final.extend(gerar_qas_em_lotes("Artigos", categorias['artigos'], instrucao, META_PERGUNTAS["Artigos"], max_ficheiros_por_lote=6))

    # 6a. ESTRATÉGICO — Pessoa <-> Projeto, sobre pares com ligação real no
    # corpus (ver montar_pares_pessoa_projeto), não documentos arbitrários.
    pares_pessoa_projeto = montar_pares_pessoa_projeto(corpus, n_pares=50)
    instrucao = "Gera perguntas ESTRATÉGICAS que cruzem a pessoa com o projeto em que trabalha (Ex: Em que projeto financiado pela FCT participa o investigador X? Qual o papel de X no projeto Y?). Dificuldade deve ser obrigatoriamente 'dificil'."
    dataset_final.extend(gerar_qas_em_lotes("Estrategico", pares_pessoa_projeto, instrucao, META_PERGUNTAS["Estrategico_PessoaProjeto"], max_ficheiros_por_lote=4, baralhar=False))

    # 6b. ESTRATÉGICO — Grupo <-> Projeto. O texto de cada grupo já
    # descreve os seus próprios projetos, por isso não precisa de pares
    # artificiais como em 6a.
    instrucao = "Gera perguntas ESTRATÉGICAS que cruzem o grupo de investigação com os projetos que lhe estão associados (Ex: Que projetos financiados estão associados a este grupo? Qual o objetivo do projeto Y conduzido por este grupo?). Dificuldade deve ser obrigatoriamente 'dificil'."
    dataset_final.extend(gerar_qas_em_lotes("Estrategico", categorias['grupos'], instrucao, META_PERGUNTAS["Estrategico_GrupoProjeto"], max_ficheiros_por_lote=2))

    if dataset_final:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(GROUND_TRUTH_FILE, "w", encoding="utf-8") as f:
            json.dump({"qa_pairs": dataset_final}, f, indent=4, ensure_ascii=False)
        print(f"\n✅ SUCESSO! Total de {len(dataset_final)} QAs gerados e guardados em {GROUND_TRUTH_FILE}")
    else:
        print("[ERRO] Não foi possível gerar nenhuma pergunta.")


if __name__ == "__main__":
    fase_1_gerar_ground_truth()
