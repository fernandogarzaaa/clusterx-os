# syntax=docker/dockerfile:1
# ---- build: produce the wheel ----
FROM python:3.12-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir build==1.6.1
COPY pyproject.toml README.md LICENSE ./
COPY clusterx_os ./clusterx_os
RUN python -m build --wheel --outdir /dist

# ---- runtime ----
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8005 \
    DATABASE_URL=sqlite:////data/app.db
WORKDIR /srv/app

COPY requirements.lock .
RUN pip install -r requirements.lock
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-deps /tmp/*.whl && rm /tmp/*.whl

RUN useradd --system --uid 10001 --home /srv/app clusterx \
    && mkdir -p /data && chown clusterx /data
USER clusterx
VOLUME ["/data"]
EXPOSE 8005

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8005'), timeout=4)"

# `clusterx-os serve` runs migrations on startup (AUTO_MIGRATE=1 by default).
CMD ["sh", "-c", "clusterx-os serve --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY:-2} --forwarded-allow-ips ${FORWARDED_ALLOW_IPS:-127.0.0.1}"]
