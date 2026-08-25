# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY migrations ./migrations
COPY modeller ./modeller
COPY schemas ./schemas
COPY scripts ./scripts
RUN python -m pip install --no-cache-dir hatchling \
    && python -m pip wheel --no-cache-dir ".[api,db]" --wheel-dir /wheelhouse \
    && python -m pip install --no-cache-dir --no-index --find-links=/wheelhouse "zekam[api,db]" \
    && PYTHONPATH=src python scripts/protocol_generate.py --check \
    && PYTHONPATH=src python scripts/generate_package_manifest.py --check

FROM python:3.12-slim AS runtime
ARG VCS_REF=unknown
ARG PROTOCOL_SCHEMA_DIGEST=unknown
LABEL org.opencontainers.image.title="Zekam" \
      org.opencontainers.image.revision="${VCS_REF}" \
      io.zekam.protocol.schema-digest="${PROTOCOL_SCHEMA_DIGEST}"
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ZEKAM_HOME=/var/lib/zekam
RUN groupadd --system zekam && useradd --system --gid zekam --home-dir /var/lib/zekam zekam
COPY --from=builder /wheelhouse /wheelhouse
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheelhouse "zekam[api,db]" \
    && rm -rf /wheelhouse \
    && mkdir -p /var/lib/zekam \
    && chown zekam:zekam /var/lib/zekam
USER zekam
WORKDIR /var/lib/zekam
EXPOSE 8769
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8769/healthz',timeout=3)"
CMD ["python", "-m", "uvicorn", "zekam.interfaces.api.health:create_health_app", "--factory", "--host", "0.0.0.0", "--port", "8769", "--no-access-log"]
