# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TMPDIR=/data/tmp

WORKDIR /app

# Install dependencies from the project metadata (no Node build pipeline).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Non-root runtime user. The entrypoint starts as root only to fix appdata
# ownership (PUID/PGID) and then drops privileges before running the app; the
# application process itself is always non-root.
RUN groupadd --gid 1000 app \
 && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /config /data /data/tmp \
 && chown -R app:app /config /data /app

COPY --chown=root:root docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Unraid/LinuxServer-compatible ownership mapping; override with PUID/PGID.
ENV PUID=1000 \
    PGID=1000

USER root

# /config holds config + secrets; /data holds SQLite state, snapshots, temp.
VOLUME ["/config", "/data"]
EXPOSE 8080

# Health reflects *application* health only; backend outages must not restart us.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
