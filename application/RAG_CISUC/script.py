"""
CISUC RAG API Service

This module implements the FastAPI interface for the hybrid retrieval engine.
It exposes endpoints for retrieving relevant document chunks based on 
natural language queries, serving as the data source for the Orchestrator.
"""
from __future__ import annotations
from contextlib import asynccontextmanager
from fastapi import FastAPI
from pydantic import BaseModel, Field
from typing import Any, AsyncGenerator
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from retrieval import get_relevant_chunks

# ---------------------------------------------------------
# API Lifecycle (RAG Engine Pre-warming)
# ---------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Manages the RAG API lifecycle, performing essential warm-up operations.

    Triggers the initial loading of embeddings into VRAM and the construction 
    of the BM25 index to ensure immediate responsiveness upon the first request.
    """
    print("[INFO] A pré-aquecer o Motor Híbrido do RAG...")
    try:
        # Perform a dummy retrieval to initialize indices and model weights
        print("[INFO] A carregar modelo de Embeddings e índices. Aguarde...")
        get_relevant_chunks("warmup test", top_k=1)
        print("[INFO] Vetores e BM25 pré-aquecidos! Motor pronto!")
    except Exception as e:
        print(f"[AVISO] Aviso no warm-up do RAG: {e}")

    yield # API is ready to accept requests

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
        print(f"[ERRO] Erro ao processar a consulta: {e}")
        return {"results": [], "error": str(e)}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)