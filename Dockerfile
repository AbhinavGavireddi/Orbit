# One build recipe, five independently versionable images via SERVICE.
FROM python:3.13.11-slim-bookworm
ARG SERVICE
ENV ORBIT_SERVICE=${SERVICE} ORBIT_BIND_HOST=0.0.0.0 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-common.txt constraints.txt /app/
COPY services/${SERVICE}/requirements.txt /app/requirements-service.txt
RUN pip install --no-cache-dir -c constraints.txt -r requirements-common.txt -r requirements-service.txt \
    && useradd --uid 10001 --create-home orbit \
    && mkdir -p /data/artifacts && chown -R orbit:orbit /data
COPY shared /app/shared
COPY services/${SERVICE} /app/service
COPY scripts/serve.py /app/serve.py
ENV PYTHONPATH=/app/shared:/app/service
USER orbit
CMD ["python", "serve.py"]
