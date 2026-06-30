# Parakeet ASR + diarization service for GB10 (aarch64 / DGX Spark).
# Built on NVIDIA's NeMo container which ships nemo_toolkit + CUDA for arm64.
FROM nvcr.io/nvidia/nemo:25.09

WORKDIR /app

# ffmpeg is needed to decode mp3/m4a/etc. into 16kHz mono WAV.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Service deps (NeMo + torch already in base image). wsproto is an alternate
# WebSocket implementation; the default "websockets" impl wedges after heavy
# in-process GPU inference, rejecting subsequent handshakes with HTTP 400.
RUN pip install --no-cache-dir \
    "fastapi>=0.115" \
    "uvicorn[standard]>=0.32" \
    "wsproto>=1.2" \
    "python-multipart>=0.0.9" \
    "loguru>=0.7"

COPY app.py /app/app.py

ENV ASR_MODEL=nvidia/parakeet-tdt-0.6b-v3 \
    DIAR_MODEL=nvidia/diar_sortformer_4spk-v1 \
    HF_HOME=/models/hf \
    NEMO_CACHE_DIR=/models/nemo

EXPOSE 8770

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8770", "--ws", "wsproto"]
