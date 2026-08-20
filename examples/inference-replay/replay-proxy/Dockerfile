# Build args so this can be rebuilt from a mirrored base; see
# deploy/docker/Dockerfile for the reasoning. Defaults are upstream.
ARG PYTHON_IMAGE=python
ARG PYTHON_VERSION=3.11-slim
FROM ${PYTHON_IMAGE}:${PYTHON_VERSION}
WORKDIR /app
RUN pip install fastapi uvicorn httpx
COPY replay_proxy.py .
EXPOSE 8080
CMD ["uvicorn", "replay_proxy:app", "--host", "0.0.0.0", "--port", "8080"]

