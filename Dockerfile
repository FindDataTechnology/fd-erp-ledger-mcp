FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY fd_erp_ledger_mcp ./fd_erp_ledger_mcp
RUN pip install --no-cache-dir .

# 非 root 运行；缓存目录可写（mcp-service-deploy 标配件）
RUN useradd --create-home --uid 10001 appuser && \
    mkdir -p /app/.cache && chown -R appuser:appuser /app
USER appuser

ENV ERP_LEDGER_CACHE_DIR=/app/.cache
EXPOSE 8000
CMD ["fd-erp-ledger-mcp", "serve", "--host", "0.0.0.0", "--port", "8000"]
