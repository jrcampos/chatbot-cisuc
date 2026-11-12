"""
Source routing for the Orchestrator: which retrieval service answers a question.

The fast SLM that extracts the search keywords also starts its answer with a source tag:
[CISUC] (CISUC's own knowledge base, ChromaDB via RAG_CISUC), [CALLS] (open and forthcoming EU
funding calls, Postgres via RAG_CALLS) or [AMBOS] (both). A missing or unknown tag falls back to
CISUC, unless it names a call identifier.
Nothing here calls a model or a service, so it is unit-tested directly.
"""
from __future__ import annotations

import re
from collections.abc import Callable
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


_EDGE = "[](){}.,;:!?\"'"


def has_call_identifier(text: str) -> bool:
    """A call/topic identifier: >= 3 ASCII alphanumeric parts joined by '-' or '/', one of them a number of
    >= 4 digits (HORIZON-CL4-2027-04-..., EuropeAid/187686/ID/ACT/MK, FT-CASCADE-43977648, ERC-2027-COG).
    The number part keeps hyphenated words ('state-of-the-art') out. Plain string checks: linear time."""
    for token in text.split():
        parts = token.strip(_EDGE).replace("/", "-").split("-")
        if (len(parts) >= 3 and all(p.isascii() and p.isalnum() for p in parts)
                and any(p.isdigit() and len(p) >= 4 for p in parts)):
            return True
    return False


def parse_targets(raw: str, calls_enabled: bool) -> tuple[tuple[str, ...], list[str]]:
    """(sources to query, search targets) from the SLM output '[TAG] target | target'. Without a valid
    tag, an output naming a call identifier goes to the calls (the SLM sometimes writes '[ID]' instead
    of '[CALLS] ID'); anything else goes to CISUC."""
    text = (raw or "").strip()
    match = _TAG.match(text)
    if match:
        sources = _ROUTES[match.group(1).lower()]
        text = text[match.end():]
    elif has_call_identifier(text):
        sources = (CALLS,)
    else:
        sources = (CISUC,)
    if not calls_enabled:
        sources = (CISUC,)
    targets = [t.strip() for t in text.split("|") if t.strip()]
    return sources, [t[1:-1].strip() if t.startswith("[") and t.endswith("]") else t for t in targets]


def intercalar_resultados(listas: list[list[dict[str, Any]]], limite: int,
                          key: Callable[[dict[str, Any]], Any]) -> list[dict[str, Any]]:
    """Round-robin merge of several RAG result lists, deduplicated by `key`, cut to `limite`.
    Interleaving keeps every query represented after the cut. `key` is the chunk for CISUC and
    the natural key for calls."""
    vistos: set[Any] = set()
    saida: list[dict[str, Any]] = []
    for posicao in range(max((len(lista) for lista in listas), default=0)):
        for lista in listas:
            if posicao < len(lista):
                doc = lista[posicao]
                chave = key(doc)
                if chave not in vistos:
                    vistos.add(chave)
                    saida.append(doc)
    return saida[:limite]


def combinar(resultados_pergunta: list[dict[str, Any]], resultados_por_alvo: list[list[dict[str, Any]]],
             limite: int, key: Callable[[dict[str, Any]], Any]) -> list[dict[str, Any]]:
    """The full question gets half of the slots; the keyword queries share the other half."""
    resultados_alvos = intercalar_resultados(resultados_por_alvo, limite, key)
    return intercalar_resultados([resultados_pergunta, resultados_alvos], limite, key)


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
