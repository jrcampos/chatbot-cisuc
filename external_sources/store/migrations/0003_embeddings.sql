-- Optional semantic layer: pgvector columns for chunk and record embeddings.
-- Skipped (with a NOTICE) when the pgvector extension is not installed on the server; full-text
-- search never depends on it. 768 dimensions = MODEL_EMBEDDINGS
-- (paraphrase-multilingual mpnet base), the model already used for cisuc_rag. A model with a
-- different size needs a new migration.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
        CREATE EXTENSION IF NOT EXISTS vector;
        ALTER TABLE extsrc.document_chunk ADD COLUMN IF NOT EXISTS embedding vector(768);
        ALTER TABLE extsrc.document_chunk ADD COLUMN IF NOT EXISTS embedding_model text;
        CREATE TABLE IF NOT EXISTS extsrc.record_embedding (
            text_sha256  text NOT NULL,
            model        text NOT NULL,
            embedding    vector(768) NOT NULL,
            created_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (text_sha256, model)
        );
        CREATE INDEX IF NOT EXISTS document_chunk_embedding_idx
            ON extsrc.document_chunk USING hnsw (embedding vector_cosine_ops);
        CREATE INDEX IF NOT EXISTS record_embedding_idx
            ON extsrc.record_embedding USING hnsw (embedding vector_cosine_ops);
        CREATE OR REPLACE VIEW current.chunk_embeddings AS
            SELECT ch.doc_id, ch.content_sha256, ch.chunk_no, ch.embedding, ch.embedding_model
            FROM extsrc.document_chunk ch
            JOIN current.document_chunks c USING (doc_id, content_sha256, chunk_no)
            WHERE ch.embedding IS NOT NULL;
        CREATE OR REPLACE VIEW current.record_embeddings AS
            SELECT r.snapshot_id, r.natural_key, e.model, e.embedding
            FROM current.records r
            JOIN extsrc.record_embedding e ON e.text_sha256 = r.embed_text_sha256;
        GRANT SELECT ON current.chunk_embeddings, current.record_embeddings TO extsrc_reader;
    ELSE
        RAISE NOTICE 'pgvector is not available: embedding columns not created (full-text search only)';
    END IF;
END
$$;
