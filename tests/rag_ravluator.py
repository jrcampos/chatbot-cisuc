"""
CISUC RAG Evaluation Pipeline (com RAGAS, Lotes e Amostragem Estratificada)

Este script implementa a avaliação formal usando a framework Ragas:
1. Geração de Perguntas em Lotes (Batching) para cobrir 100-200+ perguntas sem estourar o contexto.
2. Execução do RAG Local (via Ollama) para recolher contextos e respostas.
3. Avaliação Formal (via Ragas) exportando os resultados e Tópicos para CSV.
"""

import os
import json
from pathlib import Path
import requests
import pandas as pd
from datasets import Dataset
from ragas import evaluate
from ragas.run_config import RunConfig
from ragas.metrics import (
    faithfulness,
    answer_relevancy,
    context_precision,
    context_recall
)
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent

# Configurações de API
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL_EVALUATOR = os.environ["OPENAI_MODEL_EVALUATOR"]
OPENAI_MODEL_EMBEDDINGS = os.environ["OPENAI_MODEL_EMBEDDINGS"]
RAG_API_URL = os.environ["RAG_API_URL"]  # via orquestrador (RAG não exposto)
ORCHESTRATOR_API_URL = os.environ["ORCHESTRATOR_API_URL"]

# Ficheiros de Estado (tests/qa_generation/output/, já gitignored)
QA_GENERATION_OUTPUT_DIR = SCRIPT_DIR / "qa_generation" / "output"
GROUND_TRUTH_FILE = QA_GENERATION_OUTPUT_DIR / "ragas_ground_truth.json"
TEST_DATASET_FILE = QA_GENERATION_OUTPUT_DIR / "ragas_test_dataset.json"
RESULTS_CSV = QA_GENERATION_OUTPUT_DIR / "ragas_evaluation_results.csv"

if not OPENAI_API_KEY:
    print("[ERRO] OPENAI_API_KEY não encontrada em secrets/evaluation.env!")
    exit()

juiz_llm = ChatOpenAI(model=OPENAI_MODEL_EVALUATOR, temperature=0.2, api_key=OPENAI_API_KEY)
juiz_embeddings = OpenAIEmbeddings(model=OPENAI_MODEL_EMBEDDINGS, api_key=OPENAI_API_KEY)


def fase_2_executar_rag_local():
    print("\n--- FASE 2: EXECUTAR TESTES NO SISTEMA LOCAL (OLLAMA) ---")
    
    if not os.path.exists(GROUND_TRUTH_FILE):
        print(f"[ERRO] {GROUND_TRUTH_FILE} não existe. Corre tests/qa_generation/generate_ground_truth.py primeiro.")
        return

    with open(GROUND_TRUTH_FILE, "r", encoding="utf-8") as f:
        qa_pairs = json.load(f).get("qa_pairs", [])

    ragas_dataset = []
    
    for i, item in enumerate(qa_pairs, 1):
        pergunta = item["question"]
        print(f"[{i}/{len(qa_pairs)}] ({item.get('topico', 'Geral')} - {item['dificuldade']}): {pergunta[:60]}...")
        
        # Resposta e contextos vêm da mesma chamada ao pipeline de produção
        alvos, contexts, answer, erro = [], [], "", None
        try:
            resp = requests.post(ORCHESTRATOR_AVALIACAO_URL, json={"pergunta": pergunta}, timeout=ORCHESTRATOR_TIMEOUT)
            resp.raise_for_status()
            dados_resp = resp.json()
            alvos, contexts, answer = dados_resp["alvos"], dados_resp["contexts"], dados_resp["response"].strip()
        except Exception as e:
            erro = str(e)
            print(f"   [ERRO ORQUESTRADOR] {e}")

        ragas_dataset.append({
            "question": pergunta,
            "answer": answer,
            "contexts": contexts,
            "alvos": alvos,
            "erro": erro,
            "ground_truth": item["ground_truth"],
            "dificuldade": item["dificuldade"],
            "topico": item.get("topico", "N/A")
        })

    with open(TEST_DATASET_FILE, "w", encoding="utf-8") as f:
        json.dump(ragas_dataset, f, indent=4, ensure_ascii=False)
    print(f"[OK] Respostas recolhidas! Dataset pronto para o Ragas em {TEST_DATASET_FILE}")


def fase_3_avaliar_com_ragas():
    print("\n--- FASE 3: AVALIAÇÃO RAGAS (LLM AS A JUDGE) ---")
    
    if not os.path.exists(TEST_DATASET_FILE):
        print(f"[ERRO] {TEST_DATASET_FILE} não encontrado. Corre a Fase 2 primeiro.")
        return

    with open(TEST_DATASET_FILE, "r", encoding="utf-8") as f:
        dados_todos = json.load(f)

    # Perguntas sem resposta do orquestrador (timeout, erro HTTP) não são avaliáveis
    dados = [d for d in dados_todos if not d.get("erro")]
    print(f"A excluir {len(dados_todos) - len(dados)} perguntas com erro do orquestrador.")

    # Para depuração: RAGAS_LIMITE=20 avalia só as primeiras N perguntas
    limite = int(os.getenv("RAGAS_LIMITE", "0"))
    if limite:
        dados = dados[:limite]

    dados_formatados = {
        "question": [d["question"] for d in dados],
        "answer": [d["answer"] for d in dados],
        "contexts": [d["contexts"] for d in dados],
        "ground_truth": [d["ground_truth"] for d in dados]
    }
    
    dataset_hf = Dataset.from_dict(dados_formatados)

    print(f"A analisar {len(dados)} perguntas... (Isto consome tokens OpenAI)")
    
    caminho_logs_txt, caminho_logs_juiz = ativar_logs_juiz()
    print(f"Logs do juiz: {caminho_logs_juiz} e {caminho_logs_txt}")

    try:
        resultado = evaluate(
            dataset=dataset_hf,
            metrics=[
                context_precision,
                context_recall,
                faithfulness,
                answer_relevancy
            ],
            llm=juiz_llm,
            embeddings=juiz_embeddings,
            run_config=RunConfig(timeout=300, max_workers=8)
        )
        
        print("\n=== PONTUAÇÃO GLOBAL (RAGAS SCORE) ===")
        print(resultado)
        
        df = resultado.to_pandas()
        resumir_juiz(caminho_logs_juiz, df)

        # Juntar a Dificuldade e Tópico no CSV final
        df['dificuldade'] = [d.get("dificuldade", "N/A") for d in dados]
        df['topico'] = [d.get("topico", "N/A") for d in dados]
        df['alvos'] = [" | ".join(d.get("alvos", [])) for d in dados]
        
        df.to_csv(RESULTS_CSV, index=False)
        print(f"\n✅ Relatório detalhado guardado em: {RESULTS_CSV}")
        print("Podes agrupar a coluna 'topico' no Excel para veres onde o modelo está a falhar mais!")
        
    except Exception as e:
        print(f"[ERRO RAGAS] Falha na avaliação: {e}")


if __name__ == "__main__":
    print("MENU DE AVALIAÇÃO RAGAS (ALTO VOLUME):")
    print("1. Recolher Respostas e Contextos Locais (Ollama) - Corre após alterar/repopular a DB")
    print("2. Executar Avaliação Ragas e Gerar Relatório CSV")

    escolha = input("\nEscolhe uma opção (1/2): ")

    if escolha == "1":
        fase_2_executar_rag_local()
    elif escolha == "2":
        fase_3_avaliar_com_ragas()
    else:
        print("Opção inválida.")