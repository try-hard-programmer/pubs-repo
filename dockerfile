# Use pre-built base image with all dependencies
ARG REGISTRY_IMAGE=registry.gitlab.com/streamtify/sapa/filemanager-api
FROM ${REGISTRY_IMAGE}:base

WORKDIR /app

# Copy application code only
COPY --chown=appuser:appuser . .

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/ || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]