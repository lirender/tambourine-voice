#!/usr/bin/env bash
# Build and run the Parakeet ASR + diarization service on GB10.
# Models are cached in a named volume so they survive container restarts.
set -euo pipefail

NAME=parakeet-service
PORT=8770

cd "$(dirname "$0")"

echo "==> Building $NAME image (this pulls the NeMo base image on first run)..."
docker build -t "$NAME:latest" .

echo "==> (Re)starting container..."
docker rm -f "$NAME" 2>/dev/null || true
docker run -d \
    --name "$NAME" \
    --gpus all \
    --restart unless-stopped \
    -p "${PORT}:8770" \
    -v parakeet-models:/models \
    "$NAME:latest"

echo "==> Waiting for health (model download can take several minutes on first run)..."
for i in $(seq 1 120); do
    if curl -sf "http://localhost:${PORT}/health" | grep -q '"status":"ok"'; then
        echo "==> Service healthy on :${PORT}"
        curl -s "http://localhost:${PORT}/health"
        exit 0
    fi
    sleep 5
done
echo "==> Still loading after timeout; check: docker logs -f $NAME"
