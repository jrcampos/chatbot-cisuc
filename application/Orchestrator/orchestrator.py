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
from fastapi import FastAPI
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
    llm_principal: ChatOllama = ChatOllama(base_url=OLLAMA_URL, model=LLM_MODEL, temperature=0.2, truncate=False)

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
                "Devolve APENAS o texto de pesquisa, sem aspas, sem pontuação extra e sem explicações."),
    ("human", "{pergunta}")
])
extrator_alvos = prompt_extracao | slm_extrator | StrOutputParser()

# 2. Final Answer Generator: Usamos o LLM Pesado aqui!
system_prompt = (
    "You are the official AI Assistant for CISUC (Centre for Informatics and Systems of the University of Coimbra).\n"
    "Your job is to answer questions using ONLY the provided context below.\n"
    "If the context does not contain the answer, politely say 'I'm sorry, but I don't have that information in my current database.' Do NOT hallucinate or invent answers.\n"
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

# ---------------------------------------------------------
# Main Logic (Streaming)
# ---------------------------------------------------------

def gerador_streaming(pergunta_utilizador: str) -> Generator[str, Any, None]:
    start_time = time.time()

    # Step 1: Optimize the search query (Agora super rápido com o SLM)
    tempo_extracao_start = time.time()
    alvo_limpo = extrator_alvos.invoke({"pergunta": pergunta_utilizador}).strip()

    print(f"\n[DEBUG] Alvo fixado pelo SLM: '{alvo_limpo}' (Demorou: {time.time() - tempo_extracao_start:.2f}s)")

    # Step 2: Retrieve relevant content via RAG API
    print(f"[DEBUG] A pedir informações à API RAG...")
    documentos: list[dict[str, Any]] = []
    try:
        resposta_api = requests.post(RAG_API_URL, json={"query": alvo_limpo, "top_k": TOP_K}, timeout=30)
        resposta_api.raise_for_status()
        documentos = resposta_api.json().get("results", [])
    except Exception as e:
        print(f"[ERRO] Falha ao comunicar com a API RAG: {e}")

    # Step 3: Assemble consolidated context
    textos_para_llm: list[str] = []
    for i, doc in enumerate(documentos, 1):
        ficheiro = doc['metadata'].get('source_file', 'N/A')
        print(f"   [DOC {i}] Ficheiro: {ficheiro[:50]}...")
        textos_para_llm.append(doc['text'])

    contexto_final = "\n\n".join(textos_para_llm)

    # Step 4: Stream the final response (Com o LLM Pesado)
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

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002)