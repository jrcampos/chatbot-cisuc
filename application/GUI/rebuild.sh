#!/usr/bin/env bash

set -euo pipefail

IMAGE_NAME="chatbot_cisuc_ui_jrc"
CONTAINER_NAME="chatbot_cisuc_ui_jrc"

echo "Stopping existing container..."
sudo docker stop "$CONTAINER_NAME" 2>/dev/null || true

echo "Removing existing container..."
sudo docker rm "$CONTAINER_NAME" 2>/dev/null || true

echo "Rebuilding Docker image..."
sudo docker build --no-cache -t "$IMAGE_NAME" .

echo "Starting new container..."
sudo docker run \
  --detach \
  --restart unless-stopped \
  --name "$CONTAINER_NAME" \
  --publish 80:80 \
  "$IMAGE_NAME"

echo
echo "Container started successfully."
sudo docker ps --filter "name=$CONTAINER_NAME"