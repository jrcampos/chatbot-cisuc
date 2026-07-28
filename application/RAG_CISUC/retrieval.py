"""
 CISUC Hybrid Retrieval Engine

This module implements the core retrieval logic for the RAG system.
It performs a hybrid search combining:
- Lexical Search (BM25): Fast keyword-based retrieval using LangChain's BM25Retriever.
- Semantic Search (Vector): context-aware retrieval using ChromaDB and Ollama embeddings.

Results are fused using a Weighted Reciprocal Rank Fusion (RRF) algorithm 
to ensure the most relevant chunks are presented to the LLM.
"""
from __future__ import annotations
import chromadb
from chromadb.api import ClientAPI
import os
import time
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings
from langchain_ollama import OllamaEmbeddings
from langchain_core.documents import Document
from langchain_community.retrievers import BM25Retriever
from typing import Any

# ===== Configuration from Environment =====
# ChromaDB Connection
CHROMA_HOST: str = os.environ["CHROMA_HOST"]
CHROMA_PORT: int = int(os.environ["CHROMA_PORT"])
MAX_RETRYS: int = int(os.environ["RAG_MAX_RETRYS"])
RETRY_DELAY: int = int(os.environ["RAG_RETRY_DELAY"])

# Models Configuration
LLM_PROVIDER: str = os.environ["LLM_PROVIDER"].lower()
print(f"[INFO] A inicializar Motor Híbrido (Provider: {LLM_PROVIDER.upper()})")

RAG_MODEL: str = os.environ["MODEL_EMBEDDINGS"]

if LLM_PROVIDER == "openai":
    # --- OPENAI ---
    embeddings: OpenAIEmbeddings = OpenAIEmbeddings(model=RAG_MODEL)
else:
    # --- OLLAMA ---
    OLLAMA_URL: str = os.environ["OLLAMA_URL"]
    embeddings: OllamaEmbeddings = OllamaEmbeddings(base_url=OLLAMA_URL, model=RAG_MODEL)

print(f"       -> Modelo de Embeddings: {RAG_MODEL}")

# Connection state with retry logic for Docker race conditions
chroma_client: ClientAPI | None = None
vector_store: Chroma | None = None
retriever_vetorial: Any | None = None

# ============================================
# ChromaDB Warm-up: Sequential connection attempts
# ============================================
for tentativa in range(1, MAX_RETRYS + 1):
    try:
        print(f"[INFO] A tentar ligar ao ChromaDB em {CHROMA_HOST}:{CHROMA_PORT} (Tentativa {tentativa}/{MAX_RETRYS})...")
        chroma_client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
        
        # Verify connection status
        chroma_client.heartbeat()
        
        vector_store = Chroma(
            client=chroma_client,
            collection_name="cisuc_rag",
            embedding_function=embeddings,
            collection_metadata={"hnsw:space": "cosine"}
        )
        # Vector retriever configured with k=10 for initial candidate retrieval
        retriever_vetorial = vector_store.as_retriever(search_kwargs={"k": 10})
        print("[INFO] Ligação ao ChromaDB estabelecida com sucesso!")
        break
        
    except Exception as e:
        if tentativa < MAX_RETRYS:
            print(f"[INFO] ChromaDB ainda não está pronto. A aguardar {RETRY_DELAY} segundos...")
            time.sleep(RETRY_DELAY)
        else:
            print(f"[ERRO] Não foi possível conectar ao ChromaDB após {MAX_RETRYS} tentativas. Erro: {e}")
            chroma_client = None
            vector_store = None
            retriever_vetorial = None

# 2. Configure Lexical Retriever (BM25)
print("[INFO] A construir o Índice BM25...")
docs_para_bm25: list[Document] = []

if vector_store is not None:
    try:
        # Retrieve all documents currently in ChromaDB to build the BM25 index
        snapshot = vector_store.get()
        doc_contents: list[str] = snapshot.get('documents', [])
        doc_metadatas: list[dict[str, Any]] = snapshot.get('metadatas', []) or [{}] * len(doc_contents)
        
        print(f"[INFO] Foram encontrados {len(doc_contents)} chunks na base de dados!")
        
        for conteudo, metadados in zip(doc_contents, doc_metadatas):
            docs_para_bm25.append(Document(page_content=conteudo, metadata=metadados))
    except Exception as e:
        print(f"[ERRO] Falha ao extrair documentos para BM25: {e}")
else:
    print("[INFO] Base de dados indisponível (0 chunks).")

# Initialize BM25 retriever if documents were successfully retrieved
retriever_palavras_chave: BM25Retriever | None = None
if docs_para_bm25:
    retriever_palavras_chave = BM25Retriever.from_documents(docs_para_bm25)
    retriever_palavras_chave.k = 10

# 3. Hybrid Weights and Orchestration Ready
retriever_weights: dict[str, float] = {
    "bm25": 0.6,
    "vector": 0.4,
}
print("[INFO] Motor Híbrido pronto a receber pedidos!")


def get_relevant_chunks(query: str, top_k: int = 15) -> list[dict[str, Any]]:
    """
    Perform a weighted hybrid search using both BM25 and Vector retrievers.
    
    The results from both retrievers are merged using a custom Reciprocal Rank 
    Fusion (RRF) where each contribution is weighted:
    Score = Weight * (1 / (Rank + 1))

    Args:
        query: The user's search query.
        top_k: The final number of unique relevant chunks to return.

    Returns:
        list[dict[str, Any]]: A list of document dicts with 'text' and 'metadata'.
    """
    # 1. Clean and normalize query
    query = query.replace("'", "").replace('"', "").strip()
    print(f"\n[RAG DEBUG] A procurar a query: '{query}'")
    
    # 2. Execute retrievers in parallel (lexical)
    bm25_docs: list[Document] = []
    if retriever_palavras_chave is not None:
        try:
            bm25_docs = retriever_palavras_chave.invoke(query)
            print(f"[RAG DEBUG] BM25 encontrou: {len(bm25_docs)} docs.")
        except Exception as e:
            print(f"[ERRO BM25] {e}")

    # 3. Execute retrievers in parallel (semantic)
    vector_docs: list[Document] = []
    if retriever_vetorial is not None:
        try:
            vector_docs = retriever_vetorial.invoke(query)
            print(f"[RAG DEBUG] Vetores (Cosseno) encontrou: {len(vector_docs)} docs.")
        except Exception as e:
            print(f"[ERRO VETORES] {e}")

    # 4. Score Fusion (Weighted RRF)
    scores: dict[str, dict[str, Any]] = {}

    def apply_rrf(docs: list[Document], weight: float) -> None:
        """Helper to apply rank-based scoring to the global scores map."""
        for rank, doc in enumerate(docs):
            key = doc.page_content
            if key not in scores:
                scores[key] = {"doc": doc, "score": 0.0}
            # Weighted contribution based on reciprocal rank
            scores[key]["score"] += weight * (1.0 / (rank + 1))

    apply_rrf(bm25_docs, retriever_weights["bm25"])
    apply_rrf(vector_docs, retriever_weights["vector"])

    # 5. Sort by consolidated score and truncate
    sorted_entries = sorted(scores.values(), key=lambda x: x["score"], reverse=True)
    top_docs = [entry["doc"] for entry in sorted_entries][:top_k]
    
    print(f"[RAG DEBUG] RRF - Reciprocal Rank Fusion retornou: {len(top_docs)} documentos.")

    # 6. Format results for JSON serialization
    resultados: list[dict[str, Any]] = []
    for doc in top_docs:
        resultados.append({
            "text": doc.page_content,
            "metadata": getattr(doc, "metadata", {}) or {}
        })

    return resultados
