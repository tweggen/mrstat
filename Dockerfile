FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    MRSTAT_CONFIG=/config/mrstat.toml MRSTAT_DB=/data/mrstat.sqlite

WORKDIR /app
COPY pyproject.toml README.md ./
COPY mrstat ./mrstat
RUN pip install --no-cache-dir '.[web]' && useradd --uid 1000 mrstat && mkdir /data && chown mrstat /data

# Internal CA: mount it and set REQUESTS_CA_BUNDLE, or put verify_ssl = "/config/ca.pem" in the config.
USER mrstat
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
# Incremental sync every 6h; the very first run starts at MRSTAT_INITIAL_SINCE.
CMD ["sh", "-c", "mrstat -v serve --host 0.0.0.0 --port 8080 --sync-every ${MRSTAT_SYNC_EVERY:-21600} ${MRSTAT_INITIAL_SINCE:+--initial-since $MRSTAT_INITIAL_SINCE}"]
