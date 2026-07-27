"""
Standalone CISUC Chatbot (Local Testing)

This script provides a monolithic, terminal-based chatbot interface for testing 
the RAG pipeline locally without the need for external API services.
It initializes ChromaDB connections, builds a hybrid BM25/Vector retriever, 
and executes a LangChain pipeline (keyword extraction -> retrieval -> generation).
"""

import time
import chromadb
from typing import Any
from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings, ChatOllama
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain_core.output_parsers import StrOutputParser
from langchain_core.documents import Document

# Imports for Hybrid Search
from langchain_community.retrievers import BM25Retriever
from langchain.retrievers import EnsembleRetriever

RAG_MODEL: str = "paraphrase-multilingual:278m-mpnet-base-v2-fp16"
LLM_MODEL: str = "gemma4:31b_custom"
CHUNKS_TO_RETRIEVE: int = 15

def iniciar_chatbot() -> None:
    """
    Initialize and run the interactive terminal chatbot.
    
    This function handles the setup of local vector stores, the construction 
    of the hybrid lexical/semantic retriever, and the execution of the 
    LangChain conversation loop.
    """
    print("Iniciando o CISUC Chatbot com Motor Híbrido (Vetores + BM25)...\n")

    # ==========================================
    # 1. VECTOR ENGINE (Cosine Similarity - "Meanings")
    # ==========================================
    embeddings = OllamaEmbeddings(
        base_url="http://10.3.2.171:80",
        model=RAG_MODEL
    )

    chroma_client = chromadb.HttpClient(host='localhost', port=8000)
    vector_store = Chroma(
        client=chroma_client,
        collection_name="cisuc_rag",
        embedding_function=embeddings,
        collection_metadata={"hnsw:space": "cosine"}
    )

    # Vector retriever pulls top 10 candidate chunks
    retriever_vetorial = vector_store.as_retriever(search_kwargs={"k": 10})

    # ==========================================
    # 2. LEXICAL ENGINE (BM25 - "Exact Keywords")
    # ==========================================
    print("A carregar os textos para o motor Lexical (BM25)...")
    todos_os_docs = vector_store.get()
    docs_para_bm25: list[Document] = []
    
    for idx, conteudo in enumerate(todos_os_docs.get('documents', [])):
        metadatas = todos_os_docs.get('metadatas')
        metadados = metadatas[idx] if metadatas else {}
        docs_para_bm25.append(Document(page_content=conteudo, metadata=metadados))

    retriever_palavras_chave = BM25Retriever.from_documents(docs_para_bm25)
    # BM25 pulls top 10 candidate chunks
    retriever_palavras_chave.k = 10 

    # ==========================================
    # 3. THE ARBITRATOR (Ensemble)
    # ==========================================
    retriever_hibrido = EnsembleRetriever(
        retrievers=[retriever_palavras_chave, retriever_vetorial],
        weights=[0.6, 0.4] # Slight bias towards exact keywords (60%)
    )

    # ==========================================
    # 4. LLM CONFIGURATION
    # ==========================================
    llm = ChatOllama(
        base_url="http://10.3.2.171:80",
        model=LLM_MODEL,
        temperature=0.2,
        truncate=False,
    )

    # Final Answer Prompt
    system_prompt = (
        "You are the official AI Assistant for CISUC (Centre for Informatics and Systems of the University of Coimbra).\n"
        "Your job is to answer questions using ONLY the provided context below.\n"
        "If the context does not contain the answer, politely say 'I'm sorry, but I don't have that information in my current database.' Do NOT hallucinate or invent answers.\n"
        "Be professional, clear, and helpful. You can answer in Portuguese or English, depending on the language of the prompt.\n\n"
        "Context:\n{context}"
    )
    prompt_resposta = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("human", "{input}"),
    ])

    # ==========================================
    # 5. LLM TARGET EXTRACTOR
    # ==========================================
    prompt_extracao = ChatPromptTemplate.from_messages([
        ("system", "A tua tarefa é extrair as palavras-chave principais desta pergunta do utilizador para usar num motor de busca.\n"
                   "REGRA 1: Se a pergunta contiver um NOME PRÓPRIO (ex: pessoa ou projeto), devolve APENAS esse nome.\n"
                   "REGRA 2: Se não houver nome próprio, devolve os 3 conceitos mais importantes, preferencialmente traduzidos para INGLÊS.\n"
                   "Devolve APENAS o texto de pesquisa, sem aspas, sem pontuação extra e sem explicações."),
        ("human", "{pergunta}")
    ])
    
    extrator_alvos = prompt_extracao | llm | StrOutputParser()

    def format_docs(pergunta_utilizador: str) -> str:
        """
        Intermediate function in the pipeline that extracts keywords, retrieves 
        documents, and formats them into a single context string.
        """
        print("\n[DEBUG] A analisar a pergunta do utilizador...")
        
        # LLM extracts the core of the question to use as a search target
        alvo_limpo = extrator_alvos.invoke({"pergunta": pergunta_utilizador}).strip()
        print(f"   -> Alvo fixado pelo LLM: '{alvo_limpo}'")
        
        # Hybrid retriever collects relevant documents
        docs_finais: list[Document] = retriever_hibrido.invoke(alvo_limpo)
        
        # Truncate to avoid exceeding the context window
        docs_finais = docs_finais[:CHUNKS_TO_RETRIEVE]
        
        print(f"\n[DEBUG] Chunks recolhidos e enviados para leitura (Top {len(docs_finais)}):")
        textos_para_llm: list[str] = []
        for i, doc in enumerate(docs_finais, 1):
            ficheiro = doc.metadata.get('source_file', 'N/A')
            tipo = doc.metadata.get('type', 'N/A')
            
            print(f"   {i}. Ficheiro: {ficheiro} | Tipo: {tipo}")
            print(f"      Excerto: {doc.page_content[:90].replace(chr(10), ' ')}...\n") 
            textos_para_llm.append(doc.page_content)
            
        print("-" * 50)
        return "\n\n".join(textos_para_llm)

    # The Final Pipeline
    rag_chain = (
        # Original input feeds both the context generator and the final prompt
        {"context": format_docs, "input": RunnablePassthrough()}
        | prompt_resposta
        | llm
        | StrOutputParser()
    )
    
    # ==========================================
    # 6. AUXILIARY FUNCTIONS
    # ==========================================
    dicionario_siglas: dict[str, str] = {
        " BAI ": " Bio-inspired Artificial Intelligence (BAI) ", 
        " IS ": " Information Systems (IS) ",
        " SSE ": " Software and Systems Engineering (SSE) ",
        " CMS ": " Cognitive and Media Systems (CMS) ",
        " NCS ": " Networks, Communications, and Security (NCS) ",
        " AC ": " Adaptive Computation (AC) "
    }

    def limpar_pergunta(texto: str) -> str:
        """Expand known CISUC acronyms within the user's query."""
        texto_espacos = f" {texto} "
        for sigla, nome_completo in dicionario_siglas.items():
            texto_espacos = texto_espacos.replace(sigla, nome_completo).replace(sigla.lower(), nome_completo)
        return texto_espacos.strip()

    # ==========================================
    # 7. CONVERSATION LOOP
    # ==========================================
    print("==================================================")
    print("CISUC Chatbot Online! (Escreve 'sair' para fechar)")
    print("==================================================\n")

    while True:
        pergunta = input("\n>> Tu: ")
        
        start_time = time.time()
        if pergunta.lower() in ['sair', 'exit', 'quit']:
            print(">> CISUC Bot: Adeus!")
            break
            
        if not pergunta.strip():
            continue

        pergunta_limpa = limpar_pergunta(pergunta)
        
        # Invoke the full RAG pipeline
        resposta = rag_chain.invoke(pergunta_limpa)
        
        print(f"\r>> CISUC Bot: {resposta}\n")
        print(f"[DEBUG] Tempo de resposta: {time.time() - start_time:.2f} segundos")

if __name__ == "__main__":
    iniciar_chatbot()