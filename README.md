# CISUC Chatbot

![CISUC Logo](GUI/src/assets/cisuc_logo.svg)

A high-performance **Retrieval-Augmented Generation (RAG)** system designed specifically for **CISUC (Centre for Informatics and Systems of the University of Coimbra)**. 

The system automates the collection of research data, enriches it using LLMs, and serves it through a hybrid search pipeline (Lexical + Vector) to provide accurate, context-aware answers about members, groups, projects, and news.

## 🚀 System Architecture

```text
User Interface (Web/CLI)
       │
       ▼
Orchestrator API (Brain) ◄───────► LLM (Ollama/OpenAI)
       │
       ▼
    RAG API ◄────────────────────┐
       │                         │
       ▼                         │
Hybrid Search Engine ───────────► ChromaDB (Vector Store)
(BM25 + Cosine Similarity)
```

## 📂 Project Modules

### 1. Data Ingestion (`1_ingestion/`)
The entry point of the pipeline. It handles multiple data sources:
- **Web Crawler:** Recursively scans static CISUC pages, avoiding duplicates and noise (menus, footers).
- **API Ingestion:** Consumes internal JSON endpoints for researchers, projects, and publications.
- **News Scraper:** Uses Playwright/Selenium to capture dynamically rendered news articles.
- **Format Manager:** Normalizes all raw data into structured Markdown and JSON.

### 2. Semantic Enhancement (`2_enhancement/`)
Uses `gpt-4o-mini` (or local LLMs) to add a "metadata layer" to the raw data:
- **Alias Generation:** Creates name variations for researchers (e.g., "J. Campos", "João Campos") to improve retrieval hit rates.
- **Research Summarization:** distills long lists of publications into concise "Research Focus" summaries.
- **Cross-Referencing:** Automatically injects researcher bios into project descriptions.

### 3. Structural Embeddings (`3_embeddings/`)
Converts enhanced Markdown into searchable vectors:
- **Structural Chunking:** Respects Markdown hierarchy (`#`, `##`, `###`) to maintain section integrity.
- **Semantic Context Injection:** Every chunk is prefixed with its context (e.g., `[Context: Project | About: DBench | Section: Members]`) to prevent "RAG amnesia".
- **Deduplication:** Uses MD5 hashing to ensure only unique or updated content is stored.

### 4. RAG API (`RAG_CISUC/`)
The retrieval engine featuring **Hybrid Search**:
- **Lexical (BM25):** Optimized for keyword matching (names, acronyms).
- **Vector (ChromaDB):** Optimized for semantic meaning and conceptual queries.
- **RRF Fusion:** Combines results from both methods to provide the most relevant context.

### 5. Orchestrator (`Orchestrator/`)
The central coordinator:
- **Entity Extraction:** Uses LLM to clean user queries into searchable keywords.
- **Context Assembly:** Fetches top-K chunks and builds the prompt for the final LLM response.
- **Streaming:** Supports real-time token streaming to the UI.

### 6. User Interfaces
- **Web GUI (`GUI/`):** A modern React + Vite interface with streaming support.
- **Terminal Client (`UI_Chatbot/`):** A lightweight CLI tool for testing and legacy use.

## 🛠️ Setup & Installation

### Prerequisites
- **Python 3.12+**
- **Docker & Docker Compose**
- **Ollama** (for local embeddings and chat)

### Environment Configuration
Create a `.env` file in the root directory:

```env
# LLM / Embeddings
OLLAMA_URL=http://your-ip:8080
LLM_MODEL_CHAT=gemma4:31b_custom
LLM_MODEL_EMBEDDINGS=paraphrase-multilingual:278m-mpnet-base-v2-fp16

# RAG Settings
RAG_TOP_K=15
RAG_MAX_RETRYS=5

# API Tokens
CISUC_TOKEN=your_token_here
OPENAI_API_KEY=your_key_here
```

### Quick Start
1. **Build and start containers:**
   ```bash
   docker compose up -d --build
   ```
2. **Run the full ingestion pipeline:**
   ```bash
   ./run_full_ingestion_pipeline.sh
   ```
3. **Access the Chatbot:**
   - Web: `http://localhost`
   - CLI: `python UI_Chatbot/chatbot.py`

## 🧪 Testing
The project includes an extensive test suite:
```bash
python -m pytest tests/unit/
```
Currently covering content extractors, cleaners, and orchestrator configurations.

## 🗺️ Roadmap
- [ ] Integration of PDF extraction (Doclin).
- [ ] Implementation of RAG Evaluation (Ragas).
- [ ] Delta-update mechanism for incremental ingestion.
- [ ] Session history persistence (Redis/JSON DB).

---
*Developed for CISUC - Centre for Informatics and Systems of the University of Coimbra.*
