# The shadow-mode replay proxy image: an eval fixture, not a product image.
# See README.md.
#
# Build from the repository root, because the image carries the install's
# scrubbers from agents/:
#   docker build --platform linux/amd64 -f hack/replay-proxy/Dockerfile -t <registry>/replay-proxy-shadow:<tag> .
#
# The base comes from images.json, spelled as
# examples/inference-replay/replay-proxy/Dockerfile spells it, so
# `make images-check` covers it.
ARG PYTHON_IMAGE=python
ARG PYTHON_VERSION=3.14-slim
FROM ${PYTHON_IMAGE}:${PYTHON_VERSION}
WORKDIR /app
# Stdlib only: nothing to install.
COPY hack/replay-proxy/normalizer.py hack/replay-proxy/request_store.py hack/replay-proxy/replay_proxy.py /app/
# The scrubbers request_store.py loads by path (REPLAY_REDACTOR_PATH and
# REPLAY_TRANSCRIPT_RULES_PATH default to these).
COPY agents/chat/defaults/plugins/common/redactor.py /app/scrubbers/redactor.py
COPY agents/platform/scripts/worker_transcript.py /app/scrubbers/worker_transcript.py
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["python3", "/app/replay_proxy.py"]
