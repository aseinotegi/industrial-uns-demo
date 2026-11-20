# One image, one service per container: the command picks which (see docker-compose.yml).
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 uns \
    && mkdir /data && chown uns /data   # the rules engine keeps its rules here (a volume)

COPY config ./config
COPY dataset ./dataset
ENV DATASET_DIR=/app/dataset
USER uns
