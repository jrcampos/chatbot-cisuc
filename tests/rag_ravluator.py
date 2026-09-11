"""
CISUC RAG Evaluation Pipeline (com RAGAS)

Este script implementa a avaliação formal usando a framework Ragas, a partir
do ground truth produzido por tests/qa_generation/generate_ground_truth.py:
1. Execução do RAG Local para recolher contextos e respostas.
2. Avaliação Formal (via Ragas) exportando os resultados e Tópicos para CSV.
"""

import os
import json
import requests
import pandas as pd
from pathlib import Path
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

# Carregar variáveis de ambiente a partir de secrets/evaluation.env
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
OPENAI_MODEL_EVALUATOR = os.getenv("OPENAI_MODEL_EVALUATOR", "gpt-5.4")
OPENAI_MODEL_EMBEDDINGS = os.getenv("OPENAI_MODEL_EMBEDDINGS", "text-embedding-3-small")
RAG_API_URL = os.getenv("RAG_API_URL", "http://127.0.0.1:8001/query")
ORCHESTRATOR_API_URL = os.getenv("ORCHESTRATOR_API_URL", "http://127.0.0.1:8080/chat")

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
        
        # 1. Obter os Chunks (Contexts)
        contexts = []
        try:
            resp_rag = requests.post(RAG_API_URL, json={"query": pergunta, "top_k": 5}, timeout=10)
            if resp_rag.status_code == 200:
                contexts = [c.get("text") for c in resp_rag.json().get("results", [])]
        except Exception as e:
            print(f"   [AVISO RAG] {e}")

        # 2. Obter a Resposta do LLM (Answer)
        answer = ""
        try:
            with requests.post(ORCHESTRATOR_API_URL, json={"pergunta": pergunta}, stream=True, timeout=60) as r:
                for chunk in r.iter_content(chunk_size=None, decode_unicode=True):
                    if chunk:
                        answer += chunk
        except Exception as e:
             answer = f"[ERRO NO ORQUESTRADOR]: {e}"

        ragas_dataset.append({
            "question": pergunta,
            "answer": answer.strip(),
            "contexts": contexts,
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
        dados = json.load(f)

    dados_formatados = {
        "question": [d["question"] for d in dados],
        "answer": [d["answer"] for d in dados],
        "contexts": [d["contexts"] for d in dados],
        "ground_truth": [d["ground_truth"] for d in dados]
    }
    
    dataset_hf = Dataset.from_dict(dados_formatados)

    print(f"A analisar {len(dados)} perguntas... (Isto consome tokens OpenAI)")
    
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
        
        # Juntar a Dificuldade e Tópico no CSV final
        df['dificuldade'] = [d.get("dificuldade", "N/A") for d in dados]
        df['topico'] = [d.get("topico", "N/A") for d in dados]
        
        df.to_csv(RESULTS_CSV, index=False)
        print(f"\n✅ Relatório detalhado guardado em: {RESULTS_CSV}")
        print("Podes agrupar a coluna 'topico' no Excel para veres onde o modelo está a falhar mais!")
        
    except Exception as e:
        print(f"[ERRO RAGAS] Falha na avaliação: {e}")


if __name__ == "__main__":
    print("MENU DE AVALIAÇÃO RAGAS:")
    print("1. Recolher Respostas e Contextos Locais (Ollama) - Corre após alterar/repopular a DB")
    print("2. Executar Avaliação Ragas e Gerar Relatório CSV")

    escolha = input("\nEscolhe uma opção (1/2): ")

    if escolha == "1":
        fase_2_executar_rag_local()
    elif escolha == "2":
        fase_3_avaliar_com_ragas()
    else:
        print("Opção inválida.")