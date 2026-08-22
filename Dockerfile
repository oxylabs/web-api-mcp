FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV MCP_TRANSPORT=streamable-http \
    HOST=0.0.0.0 \
    PORT=8080
EXPOSE 8080

# OXYLABS_API_KEY must be supplied at runtime, never baked into the image.
CMD ["oxylabs-web-api-mcp"]
