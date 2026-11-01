"""
CISUC RAG API Service

This module implements the FastAPI interface for the hybrid retrieval engine.
It exposes endpoints for retrieving relevant document chunks based on 
natural language queries, serving as the data source for the Orchestrator.
"""
from __future__ import annotations
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Any, AsyncGenerator
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import retrieval
from retrieval import get_relevant_chunks

# True only after the cross-encoder and the retrievers were exercised successfully.
# Served by /health, which the container healthcheck (and so the Orchestrator start) waits on.
ready: bool = False

# ---------------------------------------------------------
# API Lifecycle (RAG Engine Pre-warming)
# ---------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Manages the RAG API lifecycle, performing essential warm-up operations.

    Runs one real inference on the cross-encoder and one full retrieval so the first
    user request does not pay for it, and only then reports the service as ready.
    """
    global ready
    print("[INFO] A pré-aquecer o Motor Híbrido do RAG...")
    try:
        # Direct inference: exercises the reranker even if the database is empty
        print("[INFO] A aquecer o reranker (cross-encoder)...")
        retrieval.reranker.predict([("warmup", "warmup")])
        # Full retrieval: initialises the indices and the embedding model
        print("[INFO] A carregar modelo de Embeddings e índices. Aguarde...")
        get_relevant_chunks("warmup test", top_k=1)
        if retrieval.retriever_vetorial is None or retrieval.retriever_palavras_chave is None:
            raise RuntimeError("ChromaDB/BM25 indisponíveis (ver erros de arranque acima)")
        ready = True
        print("[INFO] Reranker, vetores e BM25 pré-aquecidos! Motor pronto!")
    except Exception as e:
        print(f"[ERRO] Warm-up do RAG falhou; o serviço NÃO fica pronto (/health devolve 503): {e}")

    yield # API accepts connections; /health tells whether it is actually ready

    print("[INFO] A desligar o RAG API...")


# Initialize FastAPI App
app = FastAPI(
    title="CISUC RAG API",
    description="Interface for hybrid lexical and semantic document retrieval.",
    version="1.0.0",
    lifespan=lifespan
)

# Configure CORS for inter-service communication
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

class QueryRequest(BaseModel):
    """
    Data model for the document retrieval request.
    """
    query: str = Field(..., description="The search string to find relevant chunks for.")
    top_k: int = Field(default=15, description="Number of relevant documents to return.")

class QueryResponse(BaseModel):
    """
    Data model for the document retrieval response.
    """
    results: list[dict[str, Any]] = Field(..., description="List of retrieved document chunks with metadata.")

@app.post("/query", response_model=QueryResponse, summary="Retrieve relevant document chunks.")
def query_docs(request: QueryRequest) -> dict[str, Any]:
    """
    Handles retrieval requests from the Orchestrator.

    Executes a hybrid search across the configured indices and returns a 
    fused list of the most relevant content segments.
    """
    try:
        results = get_relevant_chunks(request.query, request.top_k)
        return {"results": results}
    except Exception as e:
        # Never answer 200 with an empty list on failure: callers would treat it as "no context".
        print(f"[ERRO] Erro ao processar a consulta: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health", summary="Readiness: 200 only after the reranker and retrievers are warmed up.")
def health() -> dict[str, str]:
    if not ready:
        raise HTTPException(status_code=503, detail="RAG not ready")
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)