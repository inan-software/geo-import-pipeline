FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# unzip (Info-ZIP license) runs as an external program. It reads Deflate64 archives,
# which Python's zipfile module cannot open.
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore -r requirements.txt

COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
COPY migrations ./migrations
COPY config ./config
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore --no-deps .

RUN useradd --uid 10001 --create-home pipeline \
    && mkdir -p /data/cache /data/reports \
    && chown -R pipeline:pipeline /data
USER pipeline

ENTRYPOINT ["geo-import-pipeline"]
CMD ["schedule"]

FROM base AS test
USER root
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore -r requirements-dev.txt
COPY tests ./tests
COPY examples ./examples
COPY scripts ./scripts
USER pipeline
ENTRYPOINT ["python", "-m", "pytest"]
CMD []
