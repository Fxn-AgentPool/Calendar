FROM python:3.12-slim

WORKDIR /src
COPY pyproject.toml ./
COPY calapp ./calapp
RUN pip install --no-cache-dir . \
 && useradd --system --uid 10002 --home-dir /data --shell /usr/sbin/nologin calapp \
 && mkdir -p /data && chown calapp:calapp /data \
 && rm -rf /src

WORKDIR /
USER calapp
EXPOSE 8000

# The command is set per service in docker-compose.yml.
CMD ["uvicorn", "calapp.ingest.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
