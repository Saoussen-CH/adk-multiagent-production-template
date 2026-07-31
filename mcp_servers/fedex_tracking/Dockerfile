# mcp_servers/fedex_tracking/Dockerfile
# Build context is the REPO ROOT (deploy script passes it), so the
# mcp_servers package tree is copied intact and absolute imports work.
FROM python:3.11-slim
WORKDIR /app
COPY mcp_servers/fedex_tracking/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mcp_servers/ /app/mcp_servers/
ENV PYTHONPATH=/app
EXPOSE 8080
CMD ["python", "-m", "mcp_servers.fedex_tracking.server"]
