FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
 && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin mcp

# Nothing here needs root; a numeric user also satisfies runAsNonRoot checks.
USER 10001

ENV MCP_TRANSPORT=http \
    HOST=0.0.0.0 \
    PORT=8080
EXPOSE 8080

# OXYLABS_WEB_API_KEY must be supplied at runtime, never baked into the image.
CMD ["oxylabs-web-api-mcp"]
