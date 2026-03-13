# Renamarr Dockerfile
# Multi-stage build for smaller final image

# Build stage
FROM python:3.11-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Create virtual environment and install dependencies
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir -r requirements.txt

# Production stage
FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies (ffprobe for metadata extraction)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash renamarr

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Version (injected at build time from git tag)
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

# Copy application code
COPY src/ ./src/
COPY config.yaml .

# Change ownership to non-root user
RUN chown -R renamarr:renamarr /app

# Switch to non-root user
USER renamarr

# Set Python to run unbuffered
ENV PYTHONUNBUFFERED=1

# Expose web UI port
EXPOSE 8080

# Default command - web UI mode
CMD ["python", "-m", "src.main", "--web"]
