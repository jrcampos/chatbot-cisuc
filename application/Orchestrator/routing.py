"""
Source routing for the Orchestrator: which retrieval service answers a question.

The fast SLM that extracts the search keywords also starts its answer with a source tag:
[CISUC] (CISUC's own knowledge base, ChromaDB via RAG_CISUC), [CALLS] (open and forthcoming EU
funding calls, Postgres via RAG_CALLS) or [AMBOS] (both). A missing or unknown tag falls back to
CISUC, which is the behaviour before the calls source existed. Nothing here calls a model or a
service, so it is unit-tested directly.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

CISUC = "cisuc"
CALLS = "calls"

_TAG = re.compile(r"^\s*\[\s*(CISUC|CALLS|AMBOS|BOTH)\s*\]\s*", re.IGNORECASE)
_ROUTES = {"cisuc": (CISUC,), "calls": (CALLS,), "ambos": (CISUC, CALLS), "both": (CISUC, CALLS)}

ROUTING_RULE = (
    "REGRA 0: Começa SEMPRE a resposta com UMA etiqueta de fonte:\n"
    "  [CALLS] se a pergunta é sobre oportunidades de financiamento: concursos, calls, candidaturas, "
    "bolsas, grants, prazos de submissão, orçamentos de concursos ou programas de financiamento "
    "(Horizon Europe, ERC, MSCA, Digital Europe, LIFE, EDF, ...);\n"
    "  [AMBOS] se cruza o CISUC com oportunidades de financiamento (ex: 'que concursos servem para o grupo X do CISUC');\n"
    "  [CISUC] para tudo o resto (investigadores, projetos, publicações, grupos, notícias do CISUC).\n"
    "Depois da etiqueta, um espaço e o texto de pesquisa (ex: '[CALLS] quantum computing').\n"
)


def parse_targets(raw: str, calls_enabled: bool) -> tuple[tuple[str, ...], list[str]]:
    """(sources to query, search targets) from the SLM output '[TAG] target | target'."""
    text = (raw or "").strip()
    match = _TAG.match(text)
    sources: tuple[str, ...] = (CISUC,)
    if match:
        sources = _ROUTES[match.group(1).lower()]
        text = text[match.end():]
    if not calls_enabled:
        sources = (CISUC,)
    targets = [t.strip() for t in text.split("|") if t.strip()] or ([text.strip()] if text.strip() else [])
    return sources, targets


def retrieve(
    post: Callable[[str], list[dict[str, Any]]],
    targets: Iterable[str],
    top_k: int,
    key: Callable[[dict[str, Any]], Any],
) -> list[dict[str, Any]]:
    """One retrieval per target (as for CISUC), deduplicated by `key`, capped at `top_k`.
    A failing target is logged and skipped; the others still answer."""
    documents: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for target in targets:
        try:
            results = post(target)
        except Exception as e:  # noqa: BLE001 - one unreachable service must not fail the answer
            print(f"[ERRO] Falha na recuperação para '{target}': {e}")
            continue
        for doc in results:
            k = key(doc)
            if k not in seen:
                seen.add(k)
                documents.append(doc)
    return documents[:top_k]


def cisuc_key(doc: dict[str, Any]) -> tuple[str, str]:
    return (doc.get("metadata", {}).get("source_file", ""), doc.get("text", ""))


def calls_key(doc: dict[str, Any]) -> str:
    return doc.get("metadata", {}).get("natural_key", "") or doc.get("text", "")


def build_context(cisuc_docs: list[dict[str, Any]], call_docs: list[dict[str, Any]], sources: tuple[str, ...]) -> str:
    """The context the answer model reads: one labelled section per queried source."""
    sections: list[str] = []
    if CISUC in sources:
        body = "\n\n".join(doc["text"] for doc in cisuc_docs) or "(no matching CISUC documents)"
        sections.append("### CISUC knowledge base\n\n" + body)
    if CALLS in sources:
        body = "\n\n---\n\n".join(doc["text"] for doc in call_docs) or "(no matching open or forthcoming funding calls)"
        sections.append("### EU funding calls (open and forthcoming, EU Funding & Tenders Portal)\n\n" + body)
    return "\n\n".join(sections)
