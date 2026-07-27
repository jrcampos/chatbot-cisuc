echo "Running full ingestion pipeline..."

# Step 1: Reset the database
echo "[1/4] Resetting the database..."
./delete_db.sh

# Step 2: Run the scraper pipeline
echo "[2/4] Running the scraper pipeline..."
python3 run_scraper.py --config config/scraper_config.yaml

# Step 3: Run the enhancement pipeline
echo "[3/4] Running the enhancement pipeline..."
python3 2_enhancement/enhancement.py

# Step 4: Run the embedding/ population pipeline
echo "[4/4] Running the embedding/ population pipeline..."
python3 3_embeddings/populate.py

echo "Full ingestion pipeline completed."