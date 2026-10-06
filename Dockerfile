# One build recipe, five independently versionable images via SERVICE.
# Install surface is uv sync from the locked pyproject (not pip -r).
FROM python:3.13.11-slim-bookworm
ARG SERVICE
ENV ORBIT_SERVICE=${SERVICE} \
    ORBIT_BIND_HOST=0.0.0.0 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app
# Bootstrap uv only; project deps come from uv.lock via uv sync.
RUN pip install --no-cache-dir uv==0.11.6
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project \
    && useradd --uid 10001 --create-home orbit \
    && mkdir -p /data/artifacts /ms-playwright \
    && if [ "$SERVICE" = "task" ]; then \
         /app/.venv/bin/playwright install --with-deps chromium \
         && chown -R orbit:orbit /ms-playwright; \
       fi \
    && chown -R orbit:orbit /data /app
COPY shared /app/shared
COPY services/${SERVICE} /app/service
COPY config /app/config
COPY skills /app/skills
COPY scripts/serve.py /app/serve.py
ENV PYTHONPATH=/app/shared:/app/service \
    PATH=/app/.venv/bin:$PATH
USER orbit
CMD ["python", "serve.py"]
