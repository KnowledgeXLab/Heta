FROM python:3.10-slim

# System dependencies for ML libs (easyocr, pillow, opencv, docling, mineru)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    libpq-dev \
    wget \
    curl \
    git \
    libreoffice-calc \
    libreoffice-impress \
    libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer cache)
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e .

# Default config (overridden by volume mount at runtime)
COPY config.yaml ./

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "src/main.py"]
