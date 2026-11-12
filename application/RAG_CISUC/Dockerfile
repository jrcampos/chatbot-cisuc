# Dockerfile for the RAG_CISUC FastAPI application

FROM python:3.12-slim

WORKDIR /app

# Unbuffered stdout so [INFO]/[RAG DEBUG] lines show up in `docker logs` immediately.
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies. CPU-only torch first, so sentence-transformers
# does not pull the multi-GB CUDA build from PyPI.
COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

# Bake the reranker weights into the image at build time (no network at runtime).
ARG RAG_RERANKER_MODEL
ENV HF_HOME=/app/.hf_cache
RUN python -c "from sentence_transformers import CrossEncoder; CrossEncoder('${RAG_RERANKER_MODEL}')"
ENV HF_HUB_OFFLINE=1

COPY . .

EXPOSE 8001

CMD ["uvicorn", "script:app", "--host", "0.0.0.0", "--port", "8001"]