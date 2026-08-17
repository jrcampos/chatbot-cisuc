"""
CISUC RAG Evaluation — Fase 1b: Validação do Ground Truth.

Verifica se cada par pergunta/ground_truth em ragas_ground_truth.json é
suportado pelo texto de onde foi gerado. Compara apenas contra o texto do
corpus (tests/qa_generation/corpus/corpus.json) — não faz qualquer chamada
ao RAG API, ao Orchestrator ou à Chroma em runtime. Isto é distinto da
avaliação em tests/rag_ravluator.py (Fases 2/3), que compara as RESPOSTAS
DO CHATBOT com este ground_truth; aqui valida-se o próprio ground_truth
antes de ele ser usado como referência nessa avaliação.

Como se sabe que texto gerou cada pergunta: generate_ground_truth.py não
grava essa proveniência por pergunta, mas a amostragem e o agrupamento em
lotes são inteiramente determinísticos (random.seed(42), mesma sequência de
chamadas a random.shuffle/sample/choice sobre o mesmo corpus.json), e
ragas_ground_truth.json é escrito pela mesma ordem em que os lotes são
gerados. construir_plano(), abaixo, replica exatamente essa sequência sem
chamar a OpenAI, para reconstruir, por posição, os documentos de origem de
cada pergunta.

Esta função tem de ser mantida manualmente em sincronia com a ordem de
chamadas em generate_ground_truth.fase_1_gerar_ground_truth(): qualquer
alteração aí (nova categoria, ordem diferente, meta diferente) tem de ser
replicada aqui. atribuir_pares_aos_lotes() verifica esse alinhamento antes
de qualquer chamada à OpenAI e aborta em vez de continuar silenciosamente
se o número total de perguntas ou os tópicos gravados não baterem certo com
o plano reconstruído.

Cada lote é validado com uma chamada separada à OpenAI (mesmo modelo do
gerador, OPENAI_MODEL_EVALUATOR), desta vez como verificador de factos, não
gerador: recebe o mesmo texto-fonte do lote e as perguntas/respostas
geradas a partir dele, e devolve um veredito por pergunta:

- suportado: a resposta está completa e corretamente fundamentada no texto.
- parcialmente_suportado: parcialmente correta, incompleta ou imprecisa.
- nao_suportado: não fundamentada pelo texto (inventada, errada, sem relação).

Nunca corrige automaticamente — para "parcialmente_suportado" e
"nao_suportado" apenas sugere uma correção em sugestao_correcao, para
revisão manual. "erro_validacao" marca perguntas cujo veredito o modelo não
devolveu (falha de parsing) — tratadas como sinalizadas, não como aprovadas.

Retomável: o progresso é gravado em VALIDATION_FILE após cada lote. Se
interrompido (Ctrl+C, ou limite diário de tokens atingido), correr de novo
o mesmo comando continua a partir do último lote concluído, sem repetir
chamadas já pagas. Nunca escreve em ragas_ground_truth.json — só lê.

Output (em tests/qa_generation/output/, caminhos absolutos — não dependem do
diretório a partir do qual o script é chamado):
- ragas_ground_truth_validacao.json — veredito de todas as perguntas, mais o
  marcador de retoma concluido_ate_lote.
- ragas_ground_truth_sinalizados.json — apenas os pares parcialmente_suportado/
  nao_suportado/erro_validacao, para revisão manual.

Uso:
    python3 tests/qa_generation/validate_ground_truth.py
"""

import json
import random
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(SCRIPT_DIR))
import generate_ground_truth as ggt  # noqa: E402 — reutiliza env, constantes, montar_lotes, invocar_llm

OUTPUT_DIR = SCRIPT_DIR / "output"
GROUND_TRUTH_FILE = OUTPUT_DIR / "ragas_ground_truth.json"
VALIDATION_FILE = OUTPUT_DIR / "ragas_ground_truth_validacao.json"
FLAGGED_FILE = OUTPUT_DIR / "ragas_ground_truth_sinalizados.json"


def construir_plano():
    """Reconstrói a sequência exata de lotes gerados por
    generate_ground_truth.fase_1_gerar_ground_truth(), sem chamar a OpenAI.
    Ver nota de sincronização no docstring do módulo.
    """
    categorias, corpus = ggt.agrupar_ficheiros()
    random.seed(42)

    plano = []

    def adicionar(topico, ficheiros, meta_key, max_lote, baralhar=True, um_por_documento=False):
        for lote, qtd in ggt.montar_lotes(ficheiros, ggt.META_PERGUNTAS[meta_key], max_lote, baralhar, um_por_documento):
            plano.append({"topico": topico, "lote": lote, "qtd_pedir": qtd})

    adicionar("Grupos", categorias["grupos"], "Grupos", 2)
    adicionar("Pessoas", categorias["pessoas"], "Pessoas", 6, um_por_documento=True)
    adicionar("Projetos", categorias["projetos"], "Projetos", 6, um_por_documento=True)
    adicionar("Institucional", categorias["institucional"], "Institucional", 4)
    adicionar("Artigos", categorias["artigos"], "Artigos", 6)
    pares_pessoa_projeto = ggt.montar_pares_pessoa_projeto(corpus, n_pares=50)
    adicionar("Estrategico", pares_pessoa_projeto, "Estrategico_PessoaProjeto", 4, baralhar=False)
    adicionar("Estrategico", categorias["grupos"], "Estrategico_GrupoProjeto", 2)

    return plano


def carregar_qa_pairs():
    with GROUND_TRUTH_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)["qa_pairs"]


def atribuir_pares_aos_lotes(plano, qa_pairs):
    """Fatia qa_pairs pela ordem do plano, associando a cada lote os seus
    pares pergunta/resposta. Aborta (RuntimeError) em vez de prosseguir se o
    total ou os tópicos não baterem certo — sinal de que
    generate_ground_truth.py mudou desde a última geração, ou de que
    ragas_ground_truth.json não corresponde a este corpus.json.
    """
    total_esperado = sum(lote["qtd_pedir"] for lote in plano)
    if total_esperado != len(qa_pairs):
        raise RuntimeError(
            f"Desalinhamento: o plano reconstruído espera {total_esperado} perguntas, "
            f"mas {GROUND_TRUTH_FILE.name} tem {len(qa_pairs)}. Não avançar — "
            "confirma se generate_ground_truth.py ou corpus.json mudaram desde a última geração."
        )

    idx = 0
    for lote in plano:
        qtd = lote["qtd_pedir"]
        pares_lote = qa_pairs[idx: idx + qtd]
        topicos_no_lote = {p["topico"] for p in pares_lote}
        if topicos_no_lote != {lote["topico"]}:
            raise RuntimeError(
                f"Desalinhamento no índice {idx}: esperava tópico {lote['topico']!r}, "
                f"encontrado {topicos_no_lote}. Não avançar."
            )
        lote["pares"] = pares_lote
        lote["indices"] = list(range(idx, idx + qtd))
        idx += qtd
    return plano


def montar_prompt_validacao(lote):
    contexto = ""
    for nome_doc, texto_doc in lote["lote"]:
        # Mesmo limite de 3000 caracteres usado na geração, para verificar
        # exatamente o texto que o modelo viu ao gerar estas perguntas.
        contexto += f"\n\n--- Doc: {nome_doc} ---\n" + texto_doc[:3000]

    perguntas_texto = "\n".join(
        f"{i + 1}. Pergunta: {p['question']}\n   Resposta: {p['ground_truth']}"
        for i, p in enumerate(lote["pares"])
    )

    system_prompt = """És um Verificador de Factos rigoroso.
Vais receber documentos-fonte e uma lista numerada de pares pergunta/resposta
alegadamente gerados exclusivamente a partir desses documentos.

Para cada par, verifica se a resposta é suportada pelo texto fornecido, sem
inventar, extrapolar ou usar conhecimento externo ao texto.

Classifica cada par com um veredito:
- "suportado": a resposta está completa e corretamente fundamentada no texto.
- "parcialmente_suportado": parcialmente correta, incompleta ou imprecisa.
- "nao_suportado": não fundamentada pelo texto (inventada, errada, ou sem relação).

Para "parcialmente_suportado" e "nao_suportado", sugere a resposta correta em
"sugestao_correcao", com base exclusivamente no texto fornecido. Para
"suportado", usa null.

Devolve ESTRITAMENTE JSON com esta estrutura, com um veredito por cada
pergunta fornecida, na mesma numeração:
{
    "vereditos": [
        {"numero": 1, "veredito": "suportado", "sugestao_correcao": null}
    ]
}
"""

    human_prompt = f"{contexto}\n\n--- PARES A VERIFICAR ---\n{perguntas_texto}"
    return system_prompt, human_prompt


def validar_lote(lote):
    system_prompt, human_prompt = montar_prompt_validacao(lote)
    resposta = ggt.invocar_llm(system_prompt, human_prompt)
    veredictos_por_numero = {
        v.get("numero"): v for v in resposta.get("vereditos", []) if isinstance(v.get("numero"), int)
    }

    resultados = []
    for i, par in enumerate(lote["pares"]):
        v = veredictos_por_numero.get(i + 1)
        resultados.append({
            "index": lote["indices"][i],
            "topico": par["topico"],
            "dificuldade": par["dificuldade"],
            "question": par["question"],
            "ground_truth": par["ground_truth"],
            "veredito": v.get("veredito", "erro_validacao") if v else "erro_validacao",
            "sugestao_correcao": v.get("sugestao_correcao") if v else None,
        })
    return resultados


def carregar_progresso():
    if VALIDATION_FILE.exists():
        with VALIDATION_FILE.open("r", encoding="utf-8") as f:
            dados = json.load(f)
        return dados.get("concluido_ate_lote", 0), dados.get("resultados", [])
    return 0, []


def gravar_progresso(concluido_ate_lote, resultados):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with VALIDATION_FILE.open("w", encoding="utf-8") as f:
        json.dump({"concluido_ate_lote": concluido_ate_lote, "resultados": resultados}, f, indent=2, ensure_ascii=False)

    sinalizados = [r for r in resultados if r["veredito"] != "suportado"]
    with FLAGGED_FILE.open("w", encoding="utf-8") as f:
        json.dump({"total_sinalizados": len(sinalizados), "sinalizados": sinalizados}, f, indent=2, ensure_ascii=False)


def fase_1b_validar_ground_truth():
    print("\n--- FASE 1b: VALIDAÇÃO DO GROUND TRUTH ---")
    qa_pairs = carregar_qa_pairs()
    plano = atribuir_pares_aos_lotes(construir_plano(), qa_pairs)

    concluido_ate_lote, resultados = carregar_progresso()
    if concluido_ate_lote:
        print(f"A retomar a partir do lote {concluido_ate_lote + 1}/{len(plano)} "
              f"(progresso anterior em {VALIDATION_FILE.name}).")

    for i, lote in enumerate(plano):
        if i < concluido_ate_lote:
            continue
        print(f" -> A validar lote {i + 1}/{len(plano)} ({lote['topico']}, {len(lote['pares'])} perguntas)...")
        try:
            resultados.extend(validar_lote(lote))
            gravar_progresso(i + 1, resultados)
        except Exception as e:
            print(f"   [ERRO] Falha no lote {i + 1}: {e}")
            print(f"   Progresso guardado até ao lote {i}/{len(plano)}. Corre o script de novo para retomar.")
            return

    sinalizados = [r for r in resultados if r["veredito"] != "suportado"]
    print(f"\n✅ VALIDAÇÃO CONCLUÍDA. {len(resultados)} perguntas verificadas.")
    print(f"   Suportadas: {len(resultados) - len(sinalizados)}")
    print(f"   Sinalizadas para revisão: {len(sinalizados)} -> {FLAGGED_FILE.name}")


if __name__ == "__main__":
    fase_1b_validar_ground_truth()
