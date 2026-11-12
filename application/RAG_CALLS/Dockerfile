# One image for the EU funding calls: the retrieval API (default command) and the scheduled
# loader (ingest.sh). The external_sources package comes from the `external_sources` build
# context (see application/docker-compose.yaml).

FROM python:3.12-slim

WORKDIR /app

# pdftotext (poppler-utils) converts call documents; the API does not need it but shares the image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# CPU-only torch first, so sentence-transformers does not pull the CUDA build (as in RAG_CISUC).
COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

# Bake the reranker weights into the image at build time (no network at runtime), as in RAG_CISUC.
ARG CALLS_RERANKER_MODEL
ENV HF_HOME=/app/.hf_cache
RUN python -c "from sentence_transformers import CrossEncoder; CrossEncoder('${CALLS_RERANKER_MODEL}')" \
    && chmod -R a+rX /app/.hf_cache
ENV HF_HUB_OFFLINE=1

COPY --from=external_sources . /app/external_sources
COPY retrieval.py script.py ingest.sh ./
RUN chmod +x ingest.sh && useradd --create-home --uid 10001 calls \
    && mkdir -p /data && chown calls /data

USER calls
ENV PYTHONPATH=/app EXTSRC_WORK_DIR=/data PYTHONUNBUFFERED=1

EXPOSE 8003

CMD ["uvicorn", "script:app", "--host", "0.0.0.0", "--port", "8003"]
