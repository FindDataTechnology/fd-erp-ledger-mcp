FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY fd_erp_ledger_mcp ./fd_erp_ledger_mcp
# 线自描述端点 /.well-known/line-manifest.json（change converge-line-identity-and-keys 1.4）：
# 清单在仓根，pip 装的包里没有它，端点按 CWD 候选链取这一份（WORKDIR=/app）。不在镜像里
# = 端点只能答 404，而 404 与"没实现"在外部无法区分。构建期断言：缺了就让构建红。
COPY line-manifest.json /app/line-manifest.json
RUN test -s /app/line-manifest.json && ls -l /app/line-manifest.json
RUN pip install --no-cache-dir .

# 非 root 运行；缓存目录可写（mcp-service-deploy 标配件）
RUN useradd --create-home --uid 10001 appuser && \
    mkdir -p /app/.cache && chown -R appuser:appuser /app
USER appuser

ENV ERP_LEDGER_CACHE_DIR=/app/.cache
EXPOSE 8000
CMD ["fd-erp-ledger-mcp", "serve", "--host", "0.0.0.0", "--port", "8000"]
