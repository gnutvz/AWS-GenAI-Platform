# Ingest runs here rather than in Lambda: docling downloads layout and OCR model
# weights on first use, which blows past Lambda's package limit and its timeout.
#
#   docker build -f services/ingest/Dockerfile -t aiplat-ingest .
#   docker run --rm \
#     -v ~/.aws:/root/.aws:ro \
#     -v "$PWD/evals/corpus:/data:ro" \
#     -e AWS_REGION -e DOCUMENTS_BUCKET -e KNOWLEDGE_BASE_ID \
#     aiplat-ingest /data --wait
#
# Build context is the repo root, not this directory.

FROM python:3.11-slim

# docling shells out to these for PDF rasterisation and image handling.
RUN apt-get update && apt-get install -y --no-install-recommends \
        poppler-utils \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY aiplat ./aiplat
COPY services ./services

# docling depends on torch, and the default PyPI wheel drags in the whole CUDA
# runtime — torch, triton, and nine nvidia-* packages, several GB of GPU driver
# this container will never touch. Installing the CPU-only build first means the
# docling install below finds torch already satisfied and skips all of it.
RUN pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        torch torchvision

RUN pip install --no-cache-dir '.[ingest]'

# Model weights land here. Mount a volume at this path to keep them across runs —
# otherwise every container start re-downloads several hundred MB.
ENV HF_HOME=/models
VOLUME /models

ENTRYPOINT ["python", "-m", "services.ingest.ingest"]
CMD ["--help"]
