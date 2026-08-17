"""
Extração do corpus por entidade a partir da ChromaDB local.

O `enhancement.py` escreve ficheiros CONSOLIDADOS — um único
`enriched_api-users.md` com todos os investigadores, um único
`enriched_projects_full.md` com todos os projetos. O gerador de perguntas
trata cada documento como uma unidade indivisível e trunca-o aos 3000
caracteres, pelo que sem um passo de re-divisão todas as perguntas de
"Pessoas" sairiam de um ou dois investigadores. Este script faz essa
re-divisão: reconstrói um documento por entidade a partir dos chunks da
coleção `cisuc_rag`, agrupando-os pelos metadados que o `populate.py`
injeta.

A fonte é a Chroma, e não os ficheiros markdown, porque é a Chroma que o
RAG consulta em runtime: um corpus extraído dela garante que as perguntas
geradas têm resposta no que o sistema consegue efetivamente recuperar.

| Categoria     | Origem                          | Agrupamento    |
|---------------|---------------------------------|----------------|
| pessoas       | chunks type=researcher_profile  | Entity_Name    |
| projetos      | chunks type=research_project    | Entity_Name    |
| grupos        | chunks type=research_group      | source_file    |
| institucional | chunks type=general_research_data | source_file  |
| artigos       | news_combined.md (ver abaixo)   | bloco title:   |

Os ARTIGOS são a exceção: vêm de
`.local/raw/news/markdown/news_combined.md`, a saída direta de
`run-ingestion.sh --source news`. Os chunks de notícias na Chroma não têm
metadados por artigo (partilham todos o mesmo `source_file`/`Document_Title`
do ficheiro consolidado) e o splitting por número de caracteres não respeita
as fronteiras entre artigos. O markdown de ingestão, por não ter passado
pelo chunking, mantém um bloco `title:`/`date:`/`url:` por artigo,
permitindo reconstrução exata.

Extrai também, por pessoa, a lista de projetos em que a Chroma a associou
(metadados Section=Projects + Item_Name). O `generate_ground_truth.py` usa
essas ligações para construir pares pessoa<->projeto reais nas perguntas
estratégicas.

Output: tests/qa_generation/corpus/corpus.json, incluindo uma chave `_meta`
com a proveniência da extração (URL da Chroma, data, commit). O
`generate_ground_truth.py` lê apenas as categorias e ignora `_meta`.

Uso (com a Chroma local a correr e acessível em CHROMA_ANALYSIS_URL):
    python3 tests/qa_generation/extract_corpus.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent.parent
OUTPUT_DIR = SCRIPT_DIR / "corpus"

# Espelha preprocessing/paths.py (WORKSPACE=.local) sem importar dali: este
# script corre no host, onde a variável WORKSPACE que esse módulo exige não
# está definida.
WORKSPACE_DIR = Path(os.environ.get("WORKSPACE", BASE_DIR / ".local"))
NEWS_MARKDOWN_FILE = WORKSPACE_DIR / "raw" / "news" / "markdown" / "news_combined.md"

CHROMA_URL = os.environ.get("CHROMA_ANALYSIS_URL", "http://localhost:8000")
COLLECTION_NAME = os.environ.get("CHROMA_COLLECTION", "cisuc_rag")
API_BASE = f"{CHROMA_URL}/api/v2/tenants/default_tenant/databases/default_database"


def get_collection_id() -> str:
    resp = requests.get(f"{API_BASE}/collections", timeout=30)
    resp.raise_for_status()
    for col in resp.json():
        if col["name"] == COLLECTION_NAME:
            return col["id"]
    raise SystemExit(
        f"Coleção {COLLECTION_NAME!r} não encontrada em {CHROMA_URL}. "
        "Confirma que a ChromaDB local está a correr e acessível."
    )


def fetch_all_chunks(collection_id: str) -> list[dict]:
    """Lê a coleção inteira, paginando (a API limita o tamanho de cada resposta)."""
    chunks: list[dict] = []
    offset, limit = 0, 500
    while True:
        resp = requests.post(
            f"{API_BASE}/collections/{collection_id}/get",
            json={"limit": limit, "offset": offset, "include": ["metadatas", "documents"]},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        metadatas = data.get("metadatas", [])
        documents = data.get("documents", [])
        if not metadatas:
            break
        chunks.extend(
            {"metadata": m or {}, "text": d or ""}
            for m, d in zip(metadatas, documents)
        )
        offset += len(metadatas)
        if len(metadatas) < limit:
            break
    return chunks


def section_order(metadata: dict) -> int:
    """Ordem de reconstrução de um perfil: descrição, projetos, publicações.

    As listas de publicações são muito maiores do que o resto do perfil; sem
    esta ordenação, a secção de projetos ficaria fora dos 3000 caracteres que
    o gerador lê de cada documento.
    """
    section = metadata.get("Section")
    if section is None:
        return 0
    if section == "Projects":
        return 1
    if section == "Publications":
        return 2
    return 3


def join_chunks(entity_chunks: list[dict]) -> str:
    """Reconstrói o texto de uma entidade a partir dos seus chunks."""
    ordered = sorted(entity_chunks, key=lambda c: section_order(c["metadata"]))
    seen: set[str] = set()
    parts: list[str] = []
    for c in ordered:
        if c["text"] not in seen:
            seen.add(c["text"])
            parts.append(c["text"])
    return "\n\n".join(parts)


def build_corpus(chunks: list[dict]) -> dict:
    people_chunks: dict[str, list[dict]] = defaultdict(list)
    project_chunks: dict[str, list[dict]] = defaultdict(list)
    group_chunks: dict[str, list[dict]] = defaultdict(list)
    institutional_chunks: dict[str, list[dict]] = defaultdict(list)
    person_projects: dict[str, set[str]] = defaultdict(set)

    for chunk in chunks:
        meta = chunk["metadata"]
        doc_type = meta.get("type")

        if doc_type == "researcher_profile":
            name = meta.get("Entity_Name")
            if not name:
                continue
            people_chunks[name].append(chunk)
            if meta.get("Section") == "Projects" and meta.get("Item_Name"):
                person_projects[name].add(meta["Item_Name"])

        elif doc_type == "research_project":
            name = meta.get("Entity_Name")
            if name:
                project_chunks[name].append(chunk)

        elif doc_type == "research_group":
            group_chunks[meta.get("source_file", "unknown")].append(chunk)

        elif doc_type == "general_research_data":
            institutional_chunks[meta.get("source_file", "unknown")].append(chunk)

        # news_article fica de fora: os artigos vêm do markdown de ingestão

    return {
        "people": [
            {
                "name": name,
                "text": join_chunks(cs),
                "projects": sorted(person_projects.get(name, set())),
            }
            for name, cs in sorted(people_chunks.items())
        ],
        "projects": [
            {"name": name, "text": join_chunks(cs)}
            for name, cs in sorted(project_chunks.items())
        ],
        "groups": [
            {"id": source.removeprefix("enriched_").removesuffix(".md"), "text": join_chunks(cs)}
            for source, cs in sorted(group_chunks.items())
        ],
        "institutional": [
            {"id": source.removesuffix(".md"), "text": join_chunks(cs)}
            for source, cs in sorted(institutional_chunks.items())
        ],
    }


def parse_articles(markdown_file: Path) -> list[dict]:
    """Reconstrói um documento por artigo a partir do markdown de ingestão.

    Cada artigo é um bloco `---\ntitle: ...\n...\n---\n<corpo>`. Este ficheiro
    não passou pelo chunking, pelo que as fronteiras entre artigos são exatas.
    """
    if not markdown_file.exists():
        print(f"Aviso: {markdown_file} não existe — corpus gerado sem artigos.")
        return []

    text = markdown_file.read_text(encoding="utf-8")
    blocks = re.split(r"(?m)^---\ntitle:", text)[1:]

    articles = []
    for block in blocks:
        match = re.match(r"title:\s*(.+?)\n(.*?)\n---\n(.*)", "title:" + block, re.DOTALL)
        if not match:
            continue
        title, _frontmatter_rest, body = match.groups()
        body = re.sub(r"\n---\s*$", "", body.strip())
        articles.append({"title": title.strip(), "text": body})
    return articles


def git_commit() -> str:
    """SHA do commit em que a extração correu, para proveniência."""
    try:
        return subprocess.run(
            ["git", "-C", str(BASE_DIR), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--output", type=Path, default=OUTPUT_DIR / "corpus.json",
        help="Caminho do corpus a escrever.",
    )
    args = parser.parse_args()

    chunks = fetch_all_chunks(get_collection_id())
    print(f"Chunks lidos de {CHROMA_URL}: {len(chunks)}")

    corpus = build_corpus(chunks)
    corpus["articles"] = parse_articles(NEWS_MARKDOWN_FILE)
    corpus["_meta"] = {
        "chroma_url": CHROMA_URL,
        "collection": COLLECTION_NAME,
        "chunks": len(chunks),
        "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_commit": git_commit(),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False, indent=2)

    pares = sum(len(p["projects"]) for p in corpus["people"])
    print(f"Pessoas: {len(corpus['people'])}")
    print(f"Projetos: {len(corpus['projects'])}")
    print(f"Grupos: {len(corpus['groups'])}")
    print(f"Institucional: {len(corpus['institutional'])}")
    print(f"Artigos: {len(corpus['articles'])}")
    print(f"Pares pessoa<->projeto: {pares}")
    print(f"Corpus escrito em: {args.output}")


if __name__ == "__main__":
    main()
