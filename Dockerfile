FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8005

WORKDIR /srv/app

COPY pyproject.toml .
COPY clusterx_os ./clusterx_os
RUN pip install --no-cache-dir .

RUN mkdir -p data/uploads

EXPOSE 8005

CMD ["sh", "-c", "uvicorn --factory clusterx_os.web.app:create_app --host 0.0.0.0 --port ${PORT}"]
