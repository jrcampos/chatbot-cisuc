# Stop Chroma so its database is flushed
docker compose \
  -f preprocessing/docker-compose.yaml \
  stop chromadb

# Compose builds the populated image
docker compose \
  -f preprocessing/docker-compose.yaml \
  build chromadb-final