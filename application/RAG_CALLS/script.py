"""
EU Funding Calls RAG API Service

FastAPI interface for the funding-calls retrieval engine, with the same contract and failure rules
as RAG_CISUC: POST /query {query, top_k} -> {results: [{text, metadata}]}, HTTP 500 on failure
(never a silent empty list); GET /health answers 200 only after a real reranker inference and a
full retrieval, else 503, and reports the data freshness of every source.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import retrieval
from external_sources.store.queries import connect_reader, source_freshness
from retrieval import get_relevant_calls

ready = False


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Warm-up: one reranker inference and one full retrieval; only then the service is ready."""
    global ready
    print("[INFO] A pré-aquecer o motor de concursos...")
    try:
        retrieval.reranker.predict([("warmup", "warmup")])
        found = get_relevant_calls("research and innovation", top_k=1)
        ready = True
        print(f"[INFO] Motor de concursos pronto ({len(found)} resultado no teste).")
    except Exception as e:  # noqa: BLE001 - not ready: /health answers 503
        print(f"[ERRO] Warm-up dos concursos falhou; o serviço NÃO fica pronto (/health devolve 503): {e}")
    yield
    print("[INFO] A desligar o RAG de concursos...")


app = FastAPI(
    title="EU Funding Calls RAG API",
    description="Hybrid lexical and semantic retrieval over open and forthcoming EU funding calls.",
    version="2.0.0",
    lifespan=lifespan,
)


class QueryRequest(BaseModel):
    query: str = Field(..., max_length=1000, description="The search string to find relevant calls for.")
    top_k: int = Field(default=8, ge=1, le=50, description="Number of calls to return.")


class QueryResponse(BaseModel):
    results: list[dict[str, Any]] = Field(..., description="Retrieved calls as cited text blocks with metadata.")


@app.post("/query", response_model=QueryResponse, summary="Retrieve relevant funding calls.")
def query_calls(request: QueryRequest) -> dict[str, Any]:
    try:
        return {"results": get_relevant_calls(request.query, request.top_k)}
    except Exception as e:  # noqa: BLE001 - a failure is an error, never an empty context
        print(f"[ERRO] Erro ao processar a consulta de concursos: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"Calls retrieval failed: {type(e).__name__}") from e


@app.get("/health", summary="Readiness and data freshness of every calls source.")
def health() -> Any:
    if not ready:
        return JSONResponse(status_code=503, content={"status": "starting"})
    try:
        with connect_reader() as conn:
            sources = source_freshness(conn)
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"status": "unavailable", "error": type(e).__name__})
    return {"status": "ok" if sources else "empty", "sources": sources}
