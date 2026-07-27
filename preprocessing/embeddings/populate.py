"""
Vector Database Population Module

This module implements the structural chunking and embedding pipeline for the CISUC RAG system.
It processes enriched and raw Markdown files, applies hierarchical header-based 
splitting followed by recursive character splitting, injects contextual 
metadata, and populates a ChromaDB vector store via OpenAI or Ollama embeddings.
"""

import os
import hashlib
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from langchain_ollama import OllamaEmbeddings
from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma
import chromadb
from typing import Any

from pathlib import Path

from preprocessing.paths import (
    RAW_DIR,
    ENRICHED_DIR,
    ensure_local_directories,
)

def populate_vector_db() -> None:
    """
    Execute the full pipeline to process documents and populate the vector database.

    This includes:
    1. Defining hierarchical chunking strategies.
    2. Discovering and filtering enriched/raw Markdown files.
    3. Performing structural splitting and semantic metadata injection.
    4. Deduplicating chunks using MD5 hashing.
    5. Batch uploading vectors to the ChromaDB instance using dynamic Embeddings.
    """
    ensure_local_directories()
    
    print("A iniciar o Chunking Estrutural de todos os ficheiros...")

    # 1. Define Chunking Strategy
    headers_to_split_on: list[tuple[str, str]] = [
        ("#", "Document_Title"),
        ("##", "Entity_Name"),
        ("###", "Section"),
        ("####", "Item_Name") 
    ]

    markdown_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=headers_to_split_on,
        strip_headers=False 
    )

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1500, 
        chunk_overlap=250,
        separators=["\n\n", "\n", " ", ""] 
    )

    # 2. Intelligent Directory Mapping (agora com caminhos absolutos)
    pasta_enriched = ENRICHED_DIR
    pasta_data = RAW_DIR

    ficheiros_enriquecidos: list[Path] = []

    if pasta_enriched.exists():
        ficheiros_enriquecidos = sorted(pasta_enriched.rglob("*.md"))
    else:
        print(f"Aviso: Pasta '{pasta_enriched}' não encontrada.")

    nomes_originais_ja_enriquecidos: list[str] = []

    for caminho in ficheiros_enriquecidos:
        nome_ficheiro = caminho.name

        if nome_ficheiro.startswith("enriched_"):
            nome_original = nome_ficheiro.removeprefix("enriched_")
            nomes_originais_ja_enriquecidos.append(nome_original)

    ficheiros_brutos_a_processar: list[Path] = []

    if pasta_data.exists():
        for caminho in sorted(pasta_data.rglob("*.md")):
            if "api" in caminho.parts:
                continue

            if caminho.name not in nomes_originais_ja_enriquecidos:
                ficheiros_brutos_a_processar.append(caminho)
    else:
        print(f"Aviso: Pasta '{pasta_data}' não encontrada.")

    ficheiros_para_processar = ficheiros_enriquecidos + ficheiros_brutos_a_processar

    print(f"-> Encontrados {len(ficheiros_enriquecidos)} ficheiros enriquecidos.")
    print(f"-> Encontrados {len(ficheiros_brutos_a_processar)} ficheiros brutos novos/não enriquecidos.")
    print(f"-> Total a processar: {len(ficheiros_para_processar)}\n")

    if not ficheiros_para_processar:
        print("Erro: Nenhum ficheiro para processar.")
        return

    # 3. Process Files and Inject Contextual Metadata
    todos_os_chunks: list[Any] = []

    for caminho_ficheiro in ficheiros_para_processar:
        nome_ficheiro = caminho_ficheiro.name

        with caminho_ficheiro.open("r", encoding="utf-8") as f:
            md_content = f.read()

        chunks_estruturais = markdown_splitter.split_text(md_content)
        chunks_finais_do_ficheiro = text_splitter.split_documents(chunks_estruturais)

        chunks_limpos_deste_ficheiro: list[Any] = []

        for chunk in chunks_finais_do_ficheiro:
            texto_lower = chunk.page_content.lower()

            if "no projects found" in texto_lower or "no public records found" in texto_lower:
                continue 

            texto_limpo = chunk.page_content.replace("-", "").strip()
            if len(texto_limpo) < 50:
                continue 

            nome_lower = nome_ficheiro.lower()

            if nome_lower == "enriched_api-users.md":
                doc_type = "researcher_profile"
            elif nome_lower == "enriched_projects_full.md":
                doc_type = "research_project"
            elif "news" in caminho_ficheiro.parts:
                doc_type = "news_article"
            elif nome_lower.startswith("enriched_"):
                doc_type = "research_group"
            else:
                doc_type = "general_research_data"

            chunk.metadata["source_file"] = nome_ficheiro
            chunk.metadata["type"] = doc_type
            chunk.metadata["is_enriched"] = "true" if nome_ficheiro.startswith("enriched_") else "false"

            prefix_parts: list[str] = [f"Context: {doc_type.replace('_', ' ').title()}"]
            if "Entity_Name" in chunk.metadata:
                prefix_parts.append(f"About: {chunk.metadata['Entity_Name']}")
            if "Section" in chunk.metadata:
                prefix_parts.append(f"Section: {chunk.metadata['Section']}")
            if "Item_Name" in chunk.metadata:
                prefix_parts.append(f"Item: {chunk.metadata['Item_Name']}")

            prefixo_final = " | ".join(prefix_parts)
            chunk.page_content = f"[{prefixo_final}]\n{chunk.page_content}"
            chunks_limpos_deste_ficheiro.append(chunk)

        todos_os_chunks.extend(chunks_limpos_deste_ficheiro)

    print(f"\nTotal: {len(todos_os_chunks)} chunks gerados após divisões e injeção de contexto.")

    # 4. Configure Embeddings and Connect to ChromaDB
    llm_provider = os.getenv("LLM_PROVIDER", "ollama").strip().lower()

    print(
        "\nA configurar Motor de Embeddings "
        f"(Provider: {llm_provider.upper()})..."
    )

    if llm_provider == "openai":
        openai_api_key = os.getenv("OPENAI_API_KEY")

        if not openai_api_key:
            raise RuntimeError(
                "OPENAI_API_KEY não está definida, "
                "mas LLM_PROVIDER=openai."
            )

        rag_model = os.getenv(
            "OPENAI_MODEL_EMBEDDINGS",
            "text-embedding-3-small",
        )

        embeddings = OpenAIEmbeddings(
            model=rag_model,
            api_key=openai_api_key,
        )

    elif llm_provider == "ollama":
        rag_model = os.getenv(
            "OLLAMA_MODEL_EMBEDDINGS",
            "paraphrase-multilingual:278m-mpnet-base-v2-fp16",
        )

        ollama_url = os.getenv(
            "OLLAMA_URL",
            "http://10.3.1.241:8080",
        )

        embeddings = OllamaEmbeddings(
            base_url=ollama_url,
            model=rag_model,
        )

    else:
        raise ValueError(
            f"LLM_PROVIDER inválido: {llm_provider!r}. "
            "Valores suportados: 'openai' ou 'ollama'."
        )

    print(f"[INFO] Modelo de embeddings: {rag_model}")

    chroma_host = os.getenv("CHROMA_HOST", "localhost")
    chroma_port = int(os.getenv("CHROMA_PORT", "8000"))
    chroma_collection = os.getenv(
        "CHROMA_COLLECTION",
        "cisuc_rag",
    )

    print(
        f"[INFO] A ligar ao ChromaDB em "
        f"{chroma_host}:{chroma_port}..."
    )

    chroma_client = chromadb.HttpClient(
        host=chroma_host,
        port=chroma_port,
    )

    try:
        chroma_client.delete_collection(chroma_collection)
        print(
            "[INFO] Coleção antiga apagada com sucesso "
            "(prevenção de conflito de dimensões)."
        )
    except Exception:
        print(
            f"[INFO] A coleção '{chroma_collection}' "
            "não existia."
        )

    vector_store = Chroma(
        client=chroma_client,
        collection_name=chroma_collection,
        embedding_function=embeddings,
        collection_metadata={
            "description": "Base de dados vetorial do CISUC para RAG",
            "hnsw:space": "cosine",
        },
    )
    
    # 5. Batch Processing with Unique IDs (Hashing)
    tamanho_batch: int = 200 
    print("\nA aplicar filtro avançado de IDs (Texto + Metadados)...")

    chunks_unicos: list[Any] = []
    ids_unicos: list[str] = []
    seen_ids: set[str] = set()

    for chunk in todos_os_chunks:
        conteudo_para_hash: str = chunk.page_content + str(chunk.metadata)
        hash_id: str = hashlib.md5(conteudo_para_hash.encode('utf-8')).hexdigest()

        if hash_id not in seen_ids:
            seen_ids.add(hash_id)
            chunks_unicos.append(chunk)
            ids_unicos.append(hash_id)

    total_unicos: int = len(chunks_unicos)
    print(f"Filtro aplicado: de {len(todos_os_chunks)} originais, temos {total_unicos} chunks únicos.")
    print(f"A enviar vetores em pacotes de {tamanho_batch}...\n")

    for i in range(0, total_unicos, tamanho_batch):
        batch_chunks: list[Any] = chunks_unicos[i : i + tamanho_batch]
        batch_ids: list[str] = ids_unicos[i : i + tamanho_batch]

        print(f" -> A processar do {i} ao {i + len(batch_chunks)} (de {total_unicos})...")
        vector_store.add_documents(documents=batch_chunks, ids=batch_ids)

    print("\n=> Base de dados populada com sucesso!")

if __name__ == "__main__":
    populate_vector_db()