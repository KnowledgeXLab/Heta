#!/usr/bin/env bash

set -euo pipefail

BUILD_LOCAL=0
OPEN_BROWSER=1
PULL_RETRIES="${HETA_PULL_RETRIES:-3}"

usage() {
  cat <<'EOF'
Usage: ./scripts/bootstrap.sh [--build] [--no-open]

Start Heta with Docker Compose.

Before running this script, create and edit config.yaml:
  cp config.example.yaml config.yaml
  # or
  cp config.example.zh.yaml config.yaml

Options:
  --build     Skip GHCR pull and build backend/frontend locally.
  --no-open   Do not try to open the browser after startup.
  -h, --help  Show this help message.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build)
      BUILD_LOCAL=1
      ;;
    --no-open)
      OPEN_BROWSER=0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
  shift
done

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD=(docker-compose)
else
  echo "Docker Compose is not available. Install Docker Desktop or docker compose first." >&2
  exit 1
fi

if [[ ! -f config.yaml ]]; then
  cat >&2 <<'EOF'
config.yaml not found.

Create it first:
  cp config.example.yaml config.yaml
  # or
  cp config.example.zh.yaml config.yaml

Then edit config.yaml and fill your provider API keys.
EOF
  exit 1
fi

open_frontend() {
  if [[ "$OPEN_BROWSER" -eq 0 ]]; then
    return
  fi
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://localhost" >/dev/null 2>&1 || true
  elif command -v open >/dev/null 2>&1; then
    open "http://localhost" >/dev/null 2>&1 || true
  fi
}

start_local_build() {
  "${COMPOSE_CMD[@]}" -f docker-compose.yml -f docker-compose.dev.yml up -d --build
}

pull_published_images() {
  local attempt
  for attempt in $(seq 1 "$PULL_RETRIES"); do
    echo "Pulling published Heta images (attempt $attempt/$PULL_RETRIES)..."
    if "${COMPOSE_CMD[@]}" -f docker-compose.yml pull backend frontend; then
      return 0
    fi
    sleep 2
  done
  return 1
}

if [[ "$BUILD_LOCAL" -eq 1 ]]; then
  echo "Starting Heta from local source build..."
  start_local_build
  MODE="local source build"
else
  if pull_published_images; then
    echo "Starting Heta from published GHCR images..."
    "${COMPOSE_CMD[@]}" -f docker-compose.yml up -d
    MODE="published GHCR images"
  else
    echo "GHCR images are unavailable. Falling back to local source build..."
    start_local_build
    MODE="local source build fallback"
  fi
fi

open_frontend

echo "Config: $ROOT_DIR/config.yaml"
echo "Mode: $MODE"
echo "Frontend: http://localhost"
echo "API: http://localhost:8000"
echo "Logs: ${COMPOSE_CMD[*]} logs -f backend"
