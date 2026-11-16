# KTRU card service: local hybrid search + snapshot/index builders.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd -g 1500 app && useradd -m -u 1500 -g app app \
    && mkdir -p /app /data/snapshots /data/vector /data/semantic && chown -R app:app /app /data
WORKDIR /app

COPY --chown=app:app pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY --chown=app:app src ./src
COPY --chown=app:app scripts ./scripts

USER app
ENV KTRU_SNAPSHOT_DIR=/data/snapshots/current
EXPOSE 8010
HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=10 \
    CMD python -c "import json,sys,urllib.request as u; sys.exit(0 if json.load(u.urlopen('http://127.0.0.1:8010/api/ktru/health'))['ready'] else 1)"
CMD ["uvicorn", "src.api.ktru.app:app", "--host", "0.0.0.0", "--port", "8010", "--proxy-headers", "--forwarded-allow-ips=*"]
