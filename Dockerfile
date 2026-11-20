# Servidor MCP de Cosnautas. Un solo proceso/worker (la sesión de Cosnautas es un
# singleton en memoria y la suscripción admite UNA conexión): no escalar réplicas.
FROM python:3.12-slim

# curl: lo usa el healthcheck de Coolify (la imagen slim no lo trae)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY server.py .

RUN useradd --create-home app
USER app

EXPOSE 8787
CMD ["python", "server.py"]
