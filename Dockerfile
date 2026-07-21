# engram server — one-command stack via `docker compose up` (see docker-compose.yml).
#
# Embeddings run in-process on CPU inside the container (fine for the
# hybrid-search workload; the model is small). The HuggingFace model
# (~270MB) downloads on FIRST start and is cached in the hf-cache volume —
# first boot takes a few minutes, every later boot is seconds.
FROM python:3.12-slim

WORKDIR /app

# Install deps first for layer caching; torch CPU wheels keep the image lean(er).
COPY pyproject.toml README.md ./
COPY server ./server
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu .

# Container binds all interfaces (loopback inside a container is unreachable
# from the host); compose maps the port to 127.0.0.1 on the HOST, so the
# secure-by-default posture is preserved end to end.
ENV ENGRAM_HOST=0.0.0.0 \
    ENGRAM_ALLOW_INSECURE_BIND=true \
    ENGRAM_PORT=8920

EXPOSE 8920

# Healthcheck without curl: stdlib only.
HEALTHCHECK --interval=15s --timeout=5s --start-period=300s --retries=20 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if b'ok' in urllib.request.urlopen('http://127.0.0.1:8920/health', timeout=4).read() else 1)"

CMD ["python", "-m", "uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8920"]
