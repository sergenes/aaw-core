# The relay, self-hosted. Build from the repository root:
#   docker build -f relay/Dockerfile -t aaw-relay .
# or use relay/docker-compose.yml. Put it behind a TLS-terminating reverse proxy
# (Caddy, nginx, Traefik) so devices connect over wss://.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY aaw_core ./aaw_core
RUN pip install --no-cache-dir ".[relay]"

# The SQLite store (events for 90 days, commands for 30, device tokens) lives here.
VOLUME ["/data"]
EXPOSE 8765

CMD ["python", "-m", "aaw_core.relay", "--host", "0.0.0.0", "--port", "8765", "--db", "/data/relay.sqlite"]
