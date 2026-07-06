# ---------------------------------------------------------------------------
# Global build args (available to all stages; consumed by the LABEL below)
# Supply at build time: docker compose build --build-arg GIT_COMMIT=$(git rev-parse --short HEAD)
# ---------------------------------------------------------------------------
ARG GIT_COMMIT=unknown
ARG BUILD_DATE=unknown

# ---------------------------------------------------------------------------
# Stage 1: Build the React/Vite frontend
# ---------------------------------------------------------------------------
FROM node:20-alpine AS frontend-builder

WORKDIR /build

# Copy only what npm ci needs first (better layer caching)
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --ignore-scripts

# Now copy the rest of the frontend source and build
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: Python runtime
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# Re-declare global ARGs so their values are visible inside this stage
# (ARGs declared before the first FROM are otherwise only usable in FROM lines)
ARG GIT_COMMIT
ARG BUILD_DATE

# OCI image labels (must be on the final stage to appear on the published image)
LABEL org.opencontainers.image.title="api-zhuanzhuan" \
      org.opencontainers.image.source="https://github.com/your-org/api-zhuanzhuan" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.created="${BUILD_DATE}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENV=production

# System deps: sqlite3 CLI is useful for debugging; curl for health checks
RUN apt-get update && \
    apt-get install -y --no-install-recommends sqlite3 curl && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid 1000 --create-home appuser

WORKDIR /app

# Install Python dependencies (cache-friendly: copy only requirements first)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source
COPY backend/ ./backend/

# Copy built frontend from stage 1
COPY --from=frontend-builder /build/dist ./frontend/dist/

# Create data directory for SQLite DB
RUN mkdir -p /app/data && chown -R appuser:appuser /app

# Default database location inside the container
ENV DATABASE_PATH=/app/data/data.db

# Expose the application port
EXPOSE 8000

# Health check: lightweight GET on /health (no auth required)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Switch to non-root user
USER appuser

# Single worker: SQLite WAL mode does not support multi-process writes well
ENTRYPOINT ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
