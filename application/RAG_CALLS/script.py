"""
EU Funding Calls RAG API Service

FastAPI interface for the funding-calls retrieval engine, with the same contract as RAG_CISUC:
POST /query {query, top_k} -> {results: [{text, metadata}]}. The Orchestrator calls it when the
question is about funding calls. GET /health reports the data freshness of every source.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field

from external_sources.store.queries import connect_reader, source_freshness
from retrieval import get_relevant_calls


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Warm-up: one retrieval loads the embedding model and checks the database."""
    print("[INFO] A pré-aquecer o motor de concursos...")
    try:
        found = get_relevant_calls("research and innovation", top_k=1)
        print(f"[INFO] Motor de concursos pronto ({len(found)} resultado no teste).")
    except Exception as e:  # noqa: BLE001 - the loader may not have published yet
        print(f"[AVISO] Aviso no warm-up dos concursos: {e}")
    yield
    print("[INFO] A desligar o RAG de concursos...")


app = FastAPI(
    title="EU Funding Calls RAG API",
    description="Hybrid lexical and semantic retrieval over open and forthcoming EU funding calls.",
    version="1.0.0",
    lifespan=lifespan,
)


class QueryRequest(BaseModel):
    query: str = Field(..., description="The search string to find relevant calls for.")
    top_k: int = Field(default=8, ge=1, le=50, description="Number of calls to return.")


class QueryResponse(BaseModel):
    results: list[dict[str, Any]] = Field(..., description="Retrieved calls as cited text blocks with metadata.")


@app.post("/query", response_model=QueryResponse, summary="Retrieve relevant funding calls.")
def query_calls(request: QueryRequest) -> dict[str, Any]:
    try:
        return {"results": get_relevant_calls(request.query, request.top_k)}
    except Exception as e:  # noqa: BLE001 - the orchestrator answers without calls, never crashes
        print(f"[ERRO] Erro ao processar a consulta de concursos: {type(e).__name__}: {e}")
        return {"results": []}


@app.get("/health", summary="Data freshness of every calls source.")
def health() -> dict[str, Any]:
    try:
        with connect_reader() as conn:
            sources = source_freshness(conn)
    except Exception as e:  # noqa: BLE001
        return {"status": "unavailable", "error": type(e).__name__}
    for source in sources:
        source["data_age"] = str(source["data_age"]) if source.get("data_age") is not None else None
    return {"status": "ok" if sources else "empty", "sources": sources}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8003)
