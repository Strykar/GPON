FROM python:3.13-slim

# OCI labels. image.source is the one that matters for ghcr -- it's
# what links the published package to this repo (and inherits the
# repo's visibility/permissions in the GitHub UI). The rest is
# nice-to-have metadata visible in `docker inspect`.
LABEL org.opencontainers.image.source="https://github.com/Strykar/GPON"
LABEL org.opencontainers.image.description="Prometheus exporter for the HSGQ / ODI (Realtek RTL960x) GPON SFP"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gpon_exporter.py .

USER nobody
EXPOSE 8114

# In-container health check: pull /metrics over HTTP. python's stdlib is
# already on PATH so we don't need wget/curl in the image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8114/metrics', timeout=3).read()" \
      || exit 1

ENTRYPOINT ["python3", "/app/gpon_exporter.py"]
