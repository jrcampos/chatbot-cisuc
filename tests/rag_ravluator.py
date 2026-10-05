"""
CISUC RAG Evaluation Pipeline (com RAGAS, Lotes e Amostragem Estratificada)

Este script implementa a avaliação formal usando a framework Ragas:
1. Geração de Perguntas em Lotes (Batching) para cobrir 100-200+ perguntas sem estourar o contexto.
2. Execução do RAG Local (via Ollama) para recolher contextos e respostas.
3. Avaliação Formal (via Ragas) exportando os resultados e Tópicos para CSV.
"""

import os
import json
import logging
import time
from datetime import datetime
from pathlib import Path
import requests
import pandas as pd
from dotenv import load_dotenv
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
from langchain_core.callbacks import BaseCallbackHandler

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
# Fase 2 usa o mesmo caminho de produção (retrieval + geração) e devolve os contextos reais
ORCHESTRATOR_AVALIACAO_URL = os.getenv("ORCHESTRATOR_AVALIACAO_URL", "http://127.0.0.1:8002/chat/avaliacao")
ORCHESTRATOR_TIMEOUT = int(os.getenv("ORCHESTRATOR_TIMEOUT", "300"))

# Ficheiros de Estado (tests/qa_generation/output/, já gitignored)
QA_GENERATION_OUTPUT_DIR = SCRIPT_DIR / "qa_generation" / "output"
GROUND_TRUTH_FILE = QA_GENERATION_OUTPUT_DIR / "ragas_ground_truth.json"
TEST_DATASET_FILE = QA_GENERATION_OUTPUT_DIR / "ragas_test_dataset.json"
RESULTS_CSV = QA_GENERATION_OUTPUT_DIR / "ragas_evaluation_results.csv"
LOGS_DIR = QA_GENERATION_OUTPUT_DIR / "logs"
METRICAS = ["context_precision", "context_recall", "faithfulness", "answer_relevancy"]


class RegistoChamadasJuiz(BaseCallbackHandler):
    """Grava uma linha JSON por chamada ao juiz: saída bruta (ou erro), para ver porque é que o RAGAS devolve NaN."""

    def __init__(self, caminho: Path):
        self.caminho = caminho
        self._inicio: dict = {}

    def _escrever(self, registo: dict) -> None:
        with self.caminho.open("a", encoding="utf-8") as f:
            f.write(json.dumps(registo, ensure_ascii=False) + "\n")

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self._inicio[str(run_id)] = time.time()

    def on_llm_end(self, response, *, run_id, **kwargs):
        geracoes = response.generations[0] if response.generations else []
        self._escrever({
            "run_id": str(run_id),
            "seg": round(time.time() - self._inicio.pop(str(run_id), time.time()), 2),
            "estado": "ok",
            "saida": geracoes[0].text if geracoes else "",
        })

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._escrever({
            "run_id": str(run_id),
            "seg": round(time.time() - self._inicio.pop(str(run_id), time.time()), 2),
            "estado": "erro",
            "erro": f"{type(error).__name__}: {error}",
        })


def ativar_logs_juiz() -> tuple[Path, Path]:
    """Logs da execução: texto do logger 'ragas' (erros de Job com tipo e mensagem) e JSONL das chamadas ao juiz."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
    caminho_txt = LOGS_DIR / f"ragas_{carimbo}.log"
    caminho_jsonl = LOGS_DIR / f"juiz_{carimbo}.jsonl"

    handler = logging.FileHandler(caminho_txt, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger_ragas = logging.getLogger("ragas")
    logger_ragas.setLevel(logging.INFO)
    logger_ragas.addHandler(handler)

    juiz_llm.callbacks = [RegistoChamadasJuiz(caminho_jsonl)]
    return caminho_txt, caminho_jsonl


def resumir_juiz(caminho_jsonl: Path, df) -> None:
    """Resumo no terminal: falhas do juiz por tipo e linhas NaN por métrica."""
    registos = [json.loads(l) for l in caminho_jsonl.read_text(encoding="utf-8").splitlines() if l.strip()]
    erros = [r for r in registos if r["estado"] == "erro"]
    print(f"\nChamadas ao juiz: {len(registos)} (erros: {len(erros)})")
    for r in erros[:5]:
        print(f"   erro: {r['erro'][:200]}")
    print("Linhas NaN por métrica:")
    for m in METRICAS:
        print(f"   {m}: {int(df[m].isna().sum())}")

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