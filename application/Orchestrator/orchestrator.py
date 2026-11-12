"""
CISUC Chatbot Orchestrator API

This module implements the primary coordination layer for the RAG system.
It handles:
- Model pre-warming during startup (lifespan management).
- Natural language query normalization and keyword extraction (via Fast SLM).
- Source routing (CISUC knowledge base and/or EU funding calls) from the same SLM call.
- Coordination with the RAG APIs (RAG_CISUC, RAG_CALLS) for document retrieval.
- Streaming response generation using Heavy LLM and LangChain.
"""
from __future__ import annotations
import os
import requests
import time
from datetime import date
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
from rag_client import pesquisar_varias
from routing import CALLS, CISUC, ROUTING_RULE, build_context, calls_key, cisuc_key, combinar, parse_targets

# ===== Environment Configuration =====
# RAG Configuration
RAG_API_URL: str = os.environ["RAG_API_URL"]
TOP_K: int = int(os.environ["RAG_TOP_K"])

# Funding calls (RAG_CALLS). CALLS_SOURCE=off keeps the CISUC-only behaviour.
CALLS_ENABLED: bool = os.environ["CALLS_SOURCE"].lower() == "on"
CALLS_API_URL: str = os.environ["CALLS_API_URL"]
CALLS_TOP_K: int = int(os.environ["CALLS_TOP_K"])

# Models Configuration
LLM_PROVIDER: str = os.environ["LLM_PROVIDER"].lower()
LLM_TEMPERATURE: float = float(os.environ["LLM_TEMPERATURE"])
print(f"[INFO] A configurar Modelos no Orchestrator (Provider: {LLM_PROVIDER.upper()})...")

SLM_MODEL: str = os.environ["MODEL_SLM"]
LLM_MODEL: str = os.environ["MODEL_CHAT"]

if LLM_PROVIDER == "openai":
    # --- OPENAI ---
    slm_extrator: ChatOpenAI = ChatOpenAI(model=SLM_MODEL, temperature=0.0)
    llm_principal: ChatOpenAI = ChatOpenAI(model=LLM_MODEL, temperature=LLM_TEMPERATURE)

else:
    # --- OLLAMA ---
    OLLAMA_URL: str = os.environ["OLLAMA_URL"]
    
    slm_extrator: ChatOllama = ChatOllama(base_url=OLLAMA_URL, model=SLM_MODEL, temperature=0.0, truncate=False)
    llm_principal: ChatOllama = ChatOllama(base_url=OLLAMA_URL, model=LLM_MODEL, temperature=LLM_TEMPERATURE, truncate=False)

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
                + (ROUTING_RULE if CALLS_ENABLED else "") +
                "REGRA 1: Se a pergunta contiver um NOME PRÓPRIO (ex: pessoa ou projeto), devolve APENAS esse nome.\n"
                "REGRA 2: Se não houver nome próprio, devolve os 3 conceitos mais importantes, preferencialmente traduzidos para INGLÊS.\n"
                "REGRA 3: Se a pergunta referenciar MAIS DE UMA entidade nomeada (ex: uma pessoa E um projeto, ou um grupo E um projeto), "
                "devolve cada entidade separadamente, separadas por ' | ' (ex: 'João Silva | Projeto XPTO').\n"
                "REGRA 4: Se a pergunta citar o IDENTIFICADOR de um concurso (ex: HORIZON-CL4-2027-04-DIGITAL-EMERGING-05), devolve-o exatamente.\n"
                "Devolve APENAS o texto de pesquisa, sem aspas, sem pontuação extra e sem explicações."),
    ("human", "{pergunta}")
])
extrator_alvos = prompt_extracao | slm_extrator | StrOutputParser()

# 2. Final Answer Generator: Usamos o LLM Pesado aqui!
system_prompt = (
    "You are the official AI Assistant for CISUC (Centre for Informatics and Systems of the University of Coimbra).\n"
    "Your job is to answer questions using ONLY the provided context below.\n"
    "If the context does not contain the answer, politely say 'I'm sorry, but I don't have that information in my current database.' Do NOT hallucinate or invent answers.\n"
    "Be professional, clear, and helpful. You can answer in Portuguese or English, depending on the language of the prompt.\n"
    "Today's date is {today}.\n"
    "When you mention an EU funding call, give its identifier, status and deadline, and cite the URL from its Sources. "
    "Only present calls that appear in the context; never invent identifiers, budgets or dates. If sources disagree "
    "on a value, say so and give the range.\n\n"
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

def recuperar_contexto(pergunta: str) -> tuple[tuple[str, ...], list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Route with the SLM tag, then query each selected RAG with the full question AND the SLM
    keywords (one per entity, REGRA 3): keywords alone lose the descriptive part of the question. 
    A failing RAG call raises HTTP 502: never a silent empty context."""
    tempo_extracao_start = time.time()
    alvo_limpo = extrator_alvos.invoke({"pergunta": pergunta}).strip()
    print(f"\n[DEBUG] Alvo fixado pelo SLM: '{alvo_limpo}' (Demorou: {time.time() - tempo_extracao_start:.2f}s)")

    fontes, alvos = parse_targets(alvo_limpo, CALLS_ENABLED)
    print(f"[DEBUG] Fontes: {fontes} | pergunta completa + {len(alvos)} alvo(s): {alvos}")

    documentos: list[dict[str, Any]] = []
    concursos: list[dict[str, Any]] = []
    if CISUC in fontes:
        resultados_pergunta, *por_alvo = pesquisar_varias(RAG_API_URL, [pergunta, *alvos], TOP_K)
        documentos = combinar(resultados_pergunta, por_alvo, TOP_K, cisuc_key)
    if CALLS in fontes:
        resultados_pergunta, *por_alvo = pesquisar_varias(CALLS_API_URL, [pergunta, *alvos], CALLS_TOP_K)
        concursos = combinar(resultados_pergunta, por_alvo, CALLS_TOP_K, calls_key)

    for i, doc in enumerate(documentos, 1):
        print(f"   [DOC {i}] Ficheiro: {doc['metadata'].get('source_file', 'N/A')[:50]}...")
    for i, doc in enumerate(concursos, 1):
        print(f"   [CALL {i}] {doc['metadata'].get('natural_key', 'N/A')}")
    return fontes, alvos, documentos, concursos


def gerador_streaming(pergunta_utilizador: str, contexto_final: str, start_time: float) -> Generator[str, Any, None]:
    print("[DEBUG] A gerar resposta final (Stream iniciado)...")
    resposta = {"context": contexto_final, "input": pergunta_utilizador, "today": date.today().isoformat()}
    for chunk in gerador_resposta.stream(resposta):
        yield str(chunk)
    print(f"\n[DEBUG] Tempo total de orquestração: {time.time() - start_time:.2f} segundos")


@app.post("/chat", summary="Process a user question and stream the response.")
def chat_endpoint(request: ChatRequest) -> StreamingResponse:
    start_time = time.time()
    # Retrieve BEFORE opening the stream: a RAG failure then becomes a proper HTTP 502
    # instead of a 200 whose body is cut off mid-stream.
    fontes, _, documentos, concursos = recuperar_contexto(request.pergunta)
    return StreamingResponse(
        gerador_streaming(request.pergunta, build_context(documentos, concursos, fontes), start_time),
        media_type="text/plain",
    )

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