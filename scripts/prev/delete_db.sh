echo "Deleting database..."

docker compose down -v

rm -rf chroma_data

docker compose up -d
echo "Database deleted."

