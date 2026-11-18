# The shadow-mode replay proxy image: an eval fixture, not a product image.
# See README.md.
#
# Build from the repository root, because the image carries the install's
# scrubbers from agents/ and the shell reader they load from bench/:
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
COPY hack/replay-proxy/schema.py hack/replay-proxy/normalizer.py hack/replay-proxy/diagnosis.py \
     hack/replay-proxy/request_store.py hack/replay-proxy/spool.py hack/replay-proxy/replay_cache.py hack/replay-proxy/replay_proxy.py /app/
# The scrubbers request_store.py loads by path (REPLAY_REDACTOR_PATH and
# REPLAY_TRANSCRIPT_RULES_PATH default to these).
COPY agents/chat/defaults/plugins/common/redactor.py /app/scrubbers/redactor.py
COPY agents/platform/scripts/worker_transcript.py /app/scrubbers/worker_transcript.py
# The shell reader worker_transcript.py loads from its own folder.
COPY bench/kube_agents_bench/shell_words.py /app/scrubbers/shell_words.py
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# Fail the build when worker_transcript.py does not load the reader from its own folder.
RUN PYTHONPATH=/app/scrubbers python3 -c "import worker_transcript as w; assert w.SHELL_READER_PATH == '/app/scrubbers/shell_words.py', w.SHELL_READER_PATH"
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["python3", "/app/replay_proxy.py"]
