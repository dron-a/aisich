# syntax=docker/dockerfile:1.7

# ---------------------------------------------------------------------------
# Stage 1: Builder
# Resolves and installs dependencies into a standalone virtual environment.
# This stage is 100% cached unless uv.lock or pyproject.toml changes.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

# Copy official standalone uv binary
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Mount package manifests and install ONLY dependencies into /build/.venv
# (Keeps application code out of the builder for persistent layer caching)
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv venv /build/.venv && \
    uv sync --locked --no-dev --no-install-project

# Strip heavy vendor tests inside dependencies to save disk space
# RUN find /build/.venv -type d -name 'tests' -exec rm -rf {} + 2>/dev/null || true

# ---------------------------------------------------------------------------
# Stage 2: Lean Runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm

# Thread affinity limits: Essential for 1-2 vCPU VMs to prevent OpenMP/BLAS
# from thrashing CPU cores and starving the asyncio event loop.
ENV OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    HF_HOME="/home/app/.cache/huggingface" \
    FASTEMBED_CACHE_PATH="/home/app/.cache/fastembed"

# Install runtime shared libraries required by faiss-cpu and outbound HTTPS
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Non-root user setup
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /home/app/.cache/huggingface /home/app/.cache/fastembed \
 && chown -R app:app /home/app/.cache

WORKDIR /app

# 1. Copy the compiled, clean venv from builder
# COPY --from=builder --chown=app:app /build/.venv /app/.venv
COPY --from=builder --chown=app:app /app/.venv /app/.venv

# 2. Copy application source code (changing code only rebuilds this single layer)
COPY --chown=app:app . /app

USER app

EXPOSE 8000

# 'exec' ensures Uvicorn replaces the shell as PID 1 to receive SIGTERM properly
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --no-use-colors"]