"""
CISUC Chatbot Orchestrator API

This module implements the primary coordination layer for the RAG system.
It handles:
- Model pre-warming during startup (lifespan management).
- Natural language query normalization and keyword extraction (via Fast SLM).
- Coordination with the RAG API for document retrieval.
- Streaming response generation using Heavy LLM and LangChain.
"""
from __future__ import annotations
import os
import requests
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from langchain_ollama import ChatOllama
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
import uvicorn
from typing import Generator, Any, AsyncGenerator
from fastapi.middleware.cors import CORSMiddleware
from langchain_openai import ChatOpenAI

# ===== Environment Configuration =====
# RAG Configuration
RAG_API_URL: str = os.environ["RAG_API_URL"]
TOP_K: int = int(os.environ["RAG_TOP_K"])

# Models Configuration
LLM_PROVIDER: str = os.environ["LLM_PROVIDER"].lower()
print(f"[INFO] A configurar Modelos no Orchestrator (Provider: {LLM_PROVIDER.upper()})...")

SLM_MODEL: str = os.environ["MODEL_SLM"]
LLM_MODEL: str = os.environ["MODEL_CHAT"]

if LLM_PROVIDER == "openai":
    # --- OPENAI ---
    slm_extrator: ChatOpenAI = ChatOpenAI(model=SLM_MODEL, temperature=0.0)
    llm_principal: ChatOpenAI = ChatOpenAI(model=LLM_MODEL, temperature=0.2)

else:
    # --- OLLAMA ---
    OLLAMA_URL: str = os.environ["OLLAMA_URL"]
    
    slm_extrator: ChatOllama = ChatOllama(base_url=OLLAMA_URL, model=SLM_MODEL, temperature=0.0, truncate=False)
    llm_principal: ChatOllama = ChatOllama(base_url=OLLAMA_URL, model=LLM_MODEL, temperature=0.5, truncate=False)

print(f"       -> Extrator (Rápido): {SLM_MODEL}")
print(f"       -> Gerador (Pesado): {LLM_MODEL}")

# ---------------------------------------------------------
# API Lifecycle (Model Warm-up)
# ---------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Manages the FastAPI application lifecycle, including critical model warm-up.
    Ensures both models are loaded into GPU memory.
    """
    print("[INFO] A iniciar a rotina de pré-aquecimento (Warm-up)...")
    try:
        print(f"[INFO] A carregar o SLM ({SLM_MODEL}) para extração...")
        slm_extrator.invoke("Warmup")
        
        print(f"[INFO] A carregar o LLM Massivo ({LLM_MODEL}) para geração...")
        llm_principal.invoke("Warmup")
        
        print("[INFO] Ambos os modelos carregados na VRAM com sucesso!")
    except Exception as e:
        print(f"[AVISO] Aviso no warm-up: {e}")

    yield # API is ready and running

    print("[INFO] A desligar o Orquestrador...")

# Initialize FastAPI App
app = FastAPI(
    title="CISUC Orchestrator API",
    description="Central brain for query processing and RAG coordination.",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------
# LangChain Prompts and Chains
# ---------------------------------------------------------

# 1. Target Extractor: Usamos o SLM Rápido aqui!
prompt_extracao = ChatPromptTemplate.from_messages([
    ("system", "A tua tarefa é extrair as palavras-chave principais desta pergunta do utilizador para usar num motor de busca.\n"
                "REGRA 1: Se a pergunta contiver um NOME PRÓPRIO (ex: pessoa ou projeto), devolve APENAS esse nome.\n"
                "REGRA 2: Se não houver nome próprio, devolve os 3 conceitos mais importantes, preferencialmente traduzidos para INGLÊS.\n"
                "REGRA 3: Se a pergunta referenciar MAIS DE UMA entidade nomeada (ex: uma pessoa E um projeto, ou um grupo E um projeto), "
                "devolve cada entidade separadamente, separadas por ' | ' (ex: 'João Silva | Projeto XPTO').\n"
                "REGRA 4: NUNCA inventes entidades nem placeholders (ex: 'Projeto 1', 'Investigador'). Usa apenas nomes que aparecem literalmente na pergunta; se não houver nenhum, devolve os conceitos da REGRA 2.\n"
                "Devolve APENAS o texto de pesquisa, sem aspas, sem pontuação extra e sem explicações."),
    ("human", "{pergunta}")
])
extrator_alvos = prompt_extracao | slm_extrator | StrOutputParser()

# 2. Final Answer Generator: Usamos o LLM Pesado aqui!
system_prompt = (
    "You are the official AI Assistant for CISUC (Centre for Informatics and Systems of the University of Coimbra).\n"
    "Your job is to answer questions using ONLY the provided context below.\n"
    "Answer the question directly when the context contains the answer. Do NOT apologize or say you lack information when the context has it.\n"
    "Questions may describe an entity indirectly (e.g. 'the researcher who works on X'): match the description against the context.\n"
    "Only if the context does not contain the answer, say 'I'm sorry, but I don't have that information in my current database.' Do NOT hallucinate or invent answers.\n"
    "Be professional, clear, and helpful. You can answer in Portuguese or English, depending on the language of the prompt.\n\n"
    "Context:\n{context}"
)

prompt_resposta = ChatPromptTemplate.from_messages([
    ("system", system_prompt),
    ("human", "{input}"),
])

gerador_resposta = prompt_resposta | llm_principal | StrOutputParser()


class ChatRequest(BaseModel):
    pergunta: str = Field(..., description="The user's question to be processed.")

class QueryRequest(BaseModel):
    query: str = Field(..., description="The search string to find relevant chunks for.")
    top_k: int = Field(default=15, description="Number of relevant documents to return.")

# ---------------------------------------------------------
# Main Logic (Streaming)
# ---------------------------------------------------------

def intercalar_resultados(listas: list[list[dict[str, Any]]], limite: int) -> list[dict[str, Any]]:
    """Round-robin merge of several RAG result lists, deduplicated, cut to `limite`.
    Interleaving keeps every query represented after the TOP_K cut."""
    vistos: set[tuple[str, str]] = set()
    saida: list[dict[str, Any]] = []
    for posicao in range(max((len(lista) for lista in listas), default=0)):
        for lista in listas:
            if posicao < len(lista):
                doc = lista[posicao]
                chave = (doc.get("metadata", {}).get("source_file", ""), doc.get("text", ""))
                if chave not in vistos:
                    vistos.add(chave)
                    saida.append(doc)
    return saida[:limite]

def recuperar_contexto(pergunta: str) -> tuple[list[str], list[dict[str, Any]]]:
    """Shared retrieval path for /chat and /chat/avaliacao (evaluation uses the same code).
    Queries the RAG with the SLM keywords (one per entity, REGRA 3) AND with the full
    question: keywords alone lose the descriptive part ("investigador com foco em X")."""
    tempo_extracao_start = time.time()
    alvo_limpo = extrator_alvos.invoke({"pergunta": pergunta}).strip()
    print(f"\n[DEBUG] Alvo fixado pelo SLM: '{alvo_limpo}' (Demorou: {time.time() - tempo_extracao_start:.2f}s)")

    alvos = [a.strip() for a in alvo_limpo.split("|") if a.strip()] or [alvo_limpo]
    print(f"[DEBUG] A pedir informações à API RAG para a pergunta completa e os alvos: {alvos}")

    def pesquisar(consulta: str) -> list[dict[str, Any]]:
        try:
            resposta_api = requests.post(RAG_API_URL, json={"query": consulta, "top_k": TOP_K}, timeout=30)
            resposta_api.raise_for_status()
            return resposta_api.json().get("results", [])
        except Exception as e:
            print(f"[ERRO] Falha ao comunicar com a API RAG para '{consulta[:60]}': {e}")
            return []

    # The full question gets half of the TOP_K slots; the keyword queries share the other half.
    resultados_pergunta = pesquisar(pergunta)
    resultados_alvos = intercalar_resultados([pesquisar(alvo) for alvo in alvos], TOP_K)
    return alvos, intercalar_resultados([resultados_pergunta, resultados_alvos], TOP_K)

def formatar_contexto(documentos: list[dict[str, Any]]) -> str:
    for i, doc in enumerate(documentos, 1):
        print(f"   [DOC {i}] Ficheiro: {doc['metadata'].get('source_file', 'N/A')[:50]}...")
    return "\n\n".join(doc["text"] for doc in documentos)

def gerador_streaming(pergunta_utilizador: str) -> Generator[str, Any, None]:
    start_time = time.time()

    _, documentos = recuperar_contexto(pergunta_utilizador)
    contexto_final = formatar_contexto(documentos)

    # Stream the final response (Com o LLM Pesado)
    print(f"[DEBUG] A gerar resposta final (Stream iniciado)...")

    for chunk in gerador_resposta.stream({"context": contexto_final, "input": pergunta_utilizador}):
        yield str(chunk)

    print(f"\n[DEBUG] Tempo total de orquestração: {time.time() - start_time:.2f} segundos")

@app.post("/chat", summary="Process a user question and stream the response.")
def chat_endpoint(request: ChatRequest) -> StreamingResponse:
    return StreamingResponse(
        gerador_streaming(request.pergunta),
        media_type="text/plain"
    )

# Same pipeline as /chat, non-streamed, and it also returns the chunks the LLM saw.
# Used by the RAGAS evaluation so contexts and answer come from one production run.
@app.post("/chat/avaliacao", summary="Answer plus the exact contexts used (for evaluation).")
def chat_avaliacao_endpoint(request: ChatRequest) -> dict[str, Any]:
    alvos, documentos = recuperar_contexto(request.pergunta)
    resposta = gerador_resposta.invoke({"context": formatar_contexto(documentos), "input": request.pergunta})
    return {
        "alvos": alvos,
        "contexts": [doc["text"] for doc in documentos],
        "response": resposta,
    }

# The RAG API is not exposed outside the Docker network: retrieval queries from
# outside (e.g. the evaluation script) go through here.
@app.post("/query", summary="Proxy a retrieval query to the RAG API.")
def query_endpoint(request: QueryRequest) -> dict[str, Any]:
    try:
        resposta_api = requests.post(RAG_API_URL, json=request.model_dump(), timeout=30)
        resposta_api.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"RAG API indisponível: {e}")
    return resposta_api.json()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002)