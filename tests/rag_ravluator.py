"""
CISUC RAG Evaluation Pipeline (com RAGAS, Lotes e Amostragem Estratificada)

Este script implementa a avaliação formal usando a framework Ragas:
1. Geração de Perguntas em Lotes (Batching) para cobrir 100-200+ perguntas sem estourar o contexto.
2. Execução do RAG Local (via Ollama) para recolher contextos e respostas.
3. Avaliação Formal (via Ragas) exportando os resultados e Tópicos para CSV.
"""

import os
import json
import glob
import random
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
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser

# Carregar variáveis de ambiente
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
load_dotenv(os.path.join(BASE_DIR, "configs", "settings.env"))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Configurações de API
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
RAG_API_URL = os.getenv("RAG_API_URL", "http://127.0.0.1:8001/query")
ORCHESTRATOR_API_URL = os.getenv("ORCHESTRATOR_API_URL", "http://127.0.0.1:8080/chat")

# Ficheiros de Estado
GROUND_TRUTH_FILE = "ragas_ground_truth.json"
TEST_DATASET_FILE = "ragas_test_dataset.json"
RESULTS_CSV = "ragas_evaluation_results.csv"

# ========================================================
# CONFIGURAÇÃO DE AMOSTRAGEM (ALVO: 200 PERGUNTAS)
# Podes alterar estes números livremente!
# ========================================================
META_PERGUNTAS = {
    "Grupos": 20,
    "Pessoas": 70,
    "Projetos": 60,
    "Institucional": 20,
    "Estrategico": 30
}

if not OPENAI_API_KEY:
    print("[ERRO] OPENAI_API_KEY não encontrada no .env!")
    exit()

juiz_llm = ChatOpenAI(model="gpt-5.4", temperature=0.2, api_key=OPENAI_API_KEY)
juiz_embeddings = OpenAIEmbeddings(model="text-embedding-3-small", api_key=OPENAI_API_KEY)


def agrupar_ficheiros():
    """Identifica e categoriza corretamente os ficheiros (Corrigido para ficheiros enriquecidos)."""
    pasta_enriched = os.path.join(BASE_DIR, "enriched_output")
    pasta_data = os.path.join(BASE_DIR, "data")

    ficheiros_enriquecidos = glob.glob(f"{pasta_enriched}/*.md") if os.path.exists(pasta_enriched) else []
    nomes_originais_ja_enriquecidos = [os.path.basename(c).replace("enriched_", "") for c in ficheiros_enriquecidos if os.path.basename(c).startswith("enriched_")]

    ficheiros_brutos = []
    if os.path.exists(pasta_data):
        for root, dirs, files in os.walk(pasta_data):
            if "api" in dirs:
                dirs.remove("api")  
            for file in files:
                if file.endswith(".md") and file not in nomes_originais_ja_enriquecidos:
                    ficheiros_brutos.append(os.path.join(root, file))

    todos_ficheiros = ficheiros_enriquecidos + ficheiros_brutos
    categorias = {"grupos": [], "pessoas": [], "projetos": [], "institucional": []}
    
    for f in todos_ficheiros:
        nome = os.path.basename(f).lower()
        # A ordem destes IFs é crucial para não classificar pessoas enriquecidas como grupos
        if "api-user" in nome or "user" in nome or "pessoa" in nome:
            categorias["pessoas"].append(f)
        elif "project" in nome:
            categorias["projetos"].append(f)
        elif nome.startswith("enriched_"):
            categorias["grupos"].append(f)
        else:
            categorias["institucional"].append(f)
            
    return categorias


def gerar_qas_em_lotes(topico, ficheiros, instrucao_base, meta_perguntas, max_ficheiros_por_lote=4):
    """
    Divide os ficheiros em lotes menores e faz múltiplos pedidos à OpenAI 
    até atingir a meta exata de perguntas pedida para este tópico.
    """
    resultados = []
    if not ficheiros or meta_perguntas <= 0:
        return resultados
        
    random.shuffle(ficheiros)
    
    # Criar sub-listas (lotes) de ficheiros
    lotes = [ficheiros[i:i + max_ficheiros_por_lote] for i in range(0, len(ficheiros), max_ficheiros_por_lote)]
    
    # Quantas perguntas vamos pedir por cada lote?
    perguntas_por_lote = max(1, meta_perguntas // len(lotes)) if lotes else meta_perguntas
    perguntas_geradas = 0
    
    for i, lote in enumerate(lotes):
        faltam = meta_perguntas - perguntas_geradas
        if faltam <= 0:
            break
            
        # No último lote, pedimos exatamente o resto que falta para bater certo
        qtd_pedir = faltam if i == len(lotes) - 1 else min(perguntas_por_lote, faltam)
        
        contexto = ""
        for f_path in lote:
            with open(f_path, "r", encoding="utf-8") as f:
                # Limite seguro de 3000 caracteres por ficheiro no lote
                contexto += f"\n\n--- Doc: {os.path.basename(f_path)} ---\n" + f.read()[:3000] 
        
        prompt_dataset = ChatPromptTemplate.from_messages([
            ("system", """És um Arquiteto de Testes de Qualidade.
            Baseado EXCLUSIVAMENTE no texto fornecido, {instrucao_base}
            Gera EXATAMENTE {qtd_pedir} perguntas.
            
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
            """),
            ("human", "{contexto}")
        ])

        cadeia = prompt_dataset | juiz_llm | JsonOutputParser()
        
        print(f" -> [{topico}] A processar Lote {i+1}/{len(lotes)} (a pedir {qtd_pedir} QAs)...")
        try:
            res = cadeia.invoke({
                "contexto": contexto,
                "instrucao_base": instrucao_base,
                "qtd_pedir": str(qtd_pedir),
                "topico": topico
            })
            pares = res.get("qa_pairs", [])
            resultados.extend(pares)
            perguntas_geradas += len(pares)
        except Exception as e:
            print(f"   [ERRO] Falha no lote {i+1}: {e}")
            
    return resultados


def fase_1_gerar_ground_truth():
    """Gera as perguntas iterando por lotes para cobrir todas as metas estabelecidas."""
    print("\n--- FASE 1: GERAÇÃO DO GROUND TRUTH (EM LOTES) ---")
    categorias = agrupar_ficheiros()
    
    print(f"Ficheiros Categorizados Corretamente: {len(categorias['grupos'])} Grupos, {len(categorias['pessoas'])} Pessoas, {len(categorias['projetos'])} Projetos, {len(categorias['institucional'])} Institucional.")

    random.seed(42)
    dataset_final = []

    # 1. GRUPOS
    instrucao = "Foca-te nas áreas de pesquisa principais, investigadores associados e objetivos gerais destes grupos de investigação."
    dataset_final.extend(gerar_qas_em_lotes("Grupos", categorias['grupos'], instrucao, META_PERGUNTAS["Grupos"], max_ficheiros_por_lote=2))

    # 2. PESSOAS
    instrucao = "Foca-te exclusivamente nos interesses de pesquisa, cargos que ocupam ou publicações dos investigadores mencionados. Cobre várias pessoas diferentes."
    dataset_final.extend(gerar_qas_em_lotes("Pessoas", categorias['pessoas'], instrucao, META_PERGUNTAS["Pessoas"], max_ficheiros_por_lote=6))

    # 3. PROJETOS
    instrucao = "Foca-te no objetivo, financiamento, acrónimo ou consórcio dos projetos mencionados. Cobre projetos diferentes."
    dataset_final.extend(gerar_qas_em_lotes("Projetos", categorias['projetos'], instrucao, META_PERGUNTAS["Projetos"], max_ficheiros_por_lote=6))

    # 4. INSTITUCIONAL
    instrucao = "Gera perguntas genéricas sobre o centro CISUC (ex: história, localização, estatísticas, laboratórios)."
    dataset_final.extend(gerar_qas_em_lotes("Institucional", categorias['institucional'], instrucao, META_PERGUNTAS["Institucional"], max_ficheiros_por_lote=4))

    # 5. ESTRATÉGICO
    ficheiros_estrategicos = categorias['grupos'][:4] + categorias['projetos'][:6] + categorias['pessoas'][:6]
    instrucao = "Gera perguntas ESTRATÉGICAS que exijam cruzar dados (Ex: Que projetos pertencem a determinado grupo? Em que projetos trabalha o investigador X?). Dificuldade deve ser obrigatoriamente 'dificil'."
    dataset_final.extend(gerar_qas_em_lotes("Estrategico", ficheiros_estrategicos, instrucao, META_PERGUNTAS["Estrategico"], max_ficheiros_por_lote=8))

    if dataset_final:
        with open(GROUND_TRUTH_FILE, "w", encoding="utf-8") as f:
            json.dump({"qa_pairs": dataset_final}, f, indent=4, ensure_ascii=False)
        print(f"\n✅ SUCESSO! Total de {len(dataset_final)} QAs gerados e guardados em {GROUND_TRUTH_FILE}")
    else:
        print("[ERRO] Não foi possível gerar nenhuma pergunta.")


def fase_2_executar_rag_local():
    print("\n--- FASE 2: EXECUTAR TESTES NO SISTEMA LOCAL (OLLAMA) ---")
    
    if not os.path.exists(GROUND_TRUTH_FILE):
        print(f"[ERRO] {GROUND_TRUTH_FILE} não existe. Corre a Fase 1 primeiro.")
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
    print("MENU DE AVALIAÇÃO RAGAS (ALTO VOLUME):")
    print("1. Gerar Dataset Extenso (OpenAI) - Corre isto SÓ UMA VEZ!")
    print("2. Recolher Respostas e Contextos Locais (Ollama) - Corre após alterar/repopular a DB")
    print("3. Executar Avaliação Ragas e Gerar Relatório CSV")
    
    escolha = input("\nEscolhe uma opção (1/2/3): ")
    
    if escolha == "1":
        fase_1_gerar_ground_truth()
    elif escolha == "2":
        fase_2_executar_rag_local()
    elif escolha == "3":
        fase_3_avaliar_com_ragas()
    else:
        print("Opção inválida.")