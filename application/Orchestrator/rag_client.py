"""
Client for the RAG API used by the Orchestrator.

Kept separate from orchestrator.py (which builds LLM clients at import time) so the
retrieval calls can be unit-tested on their own.
"""

from __future__ import annotations

import os
from typing import Any

import requests
from fastapi import HTTPException

RAG_API_URL: str = os.environ["RAG_API_URL"]
TOP_K: int = int(os.environ["RAG_TOP_K"])
# Per-request budget (seconds) for one RAG call served on its own.
REQUEST_TIMEOUT: float = float(os.environ["RAG_REQUEST_TIMEOUT"])


def pesquisar(consulta: str, timeout: float) -> list[dict[str, Any]]:
    """One RAG query. Any failure raises HTTP 502: an empty list would be indistinguishable
    from "no relevant context" and the LLM would answer (and be evaluated) without any."""
    try:
        resposta = requests.post(
            RAG_API_URL, json={"query": consulta, "top_k": TOP_K}, timeout=timeout
        )
        resposta.raise_for_status()
        return resposta.json()["results"]
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"[ERRO] Falha ao comunicar com a API RAG para '{consulta[:60]}': {e}")
        raise HTTPException(
            status_code=502, detail=f"RAG API indisponível para '{consulta[:60]}': {e}"
        ) from e


def pesquisar_varias(consultas: list[str]) -> list[list[dict[str, Any]]]:
    """Run several queries and return the result lists in the SAME order as `consultas`
    (intercalar_resultados is order-sensitive)."""
    return [pesquisar(c, REQUEST_TIMEOUT) for c in consultas]
