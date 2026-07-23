# check=skip=InvalidDefaultArgInFrom
ARG HERMES_AGENT_TAG
ARG ENVOY_VERSION=v1.38.0
FROM envoyproxy/envoy:${ENVOY_VERSION} AS envoy-bin

FROM golang:1.25-alpine AS watcher-builder
WORKDIR /workspace
COPY k8s-operator/ /workspace/k8s-operator/
WORKDIR /workspace/k8s-operator
RUN CGO_ENABLED=0 go build -ldflags="-w -s" -o /workspace/k8s-event-watcher ./cmd/k8s-event-watcher/

FROM nousresearch/hermes-agent:${HERMES_AGENT_TAG} AS agent-base

USER root

# 1. Install all common system utilities and dependencies
RUN apt-get update && apt-get install -y \
    apt-transport-https \
    ca-certificates \
    curl \
    dnsutils \
    gnupg \
    iputils-ping \
    jq \
    nano \
    patch \
    vim \
    wget \
    && rm -rf /var/lib/apt/lists/*

# 2. Install credential-free utility CLIs used by the sandbox.
RUN ARCH=$(dpkg --print-architecture) && \
    curl -sSLf -o /usr/local/bin/yq "https://github.com/mikefarah/yq/releases/download/v4.53.3/yq_linux_${ARCH}" && \
    chmod +x /usr/local/bin/yq && \
    curl -sSLf -o /tmp/k9s.tar.gz "https://github.com/derailed/k9s/releases/download/v0.51.0/k9s_Linux_${ARCH}.tar.gz" && \
    tar -xzf /tmp/k9s.tar.gz -C /usr/local/bin k9s && \
    rm /tmp/k9s.tar.gz && \
    curl -sSLf -o /tmp/helm.tar.gz "https://get.helm.sh/helm-v3.15.2-linux-${ARCH}.tar.gz" && \
    tar -xzf /tmp/helm.tar.gz -C /tmp && \
    mv /tmp/linux-${ARCH}/helm /usr/local/bin/helm && \
    rm -rf /tmp/helm.tar.gz /tmp/linux-${ARCH}

RUN /usr/local/bin/uv pip install --python /opt/hermes/.venv/bin/python \
    ddgs \
    google-api-python-client \
    google-auth \
    google-cloud-pubsub \
    "hermes-agent[google_chat]" \
    "hermes-agent[slack]" \
    kubernetes \
    opentelemetry-api \
    opentelemetry-sdk \
    opentelemetry-exporter-otlp-proto-http

# Apply upstream PR #51567 patch to google_chat adapter to fix nested inline formatting leaks (GC0, GC1, etc.) and standalone thread creation bugs
RUN sed -i 's/for key, value in placeholders.items():/for key, value in reversed(list(placeholders.items())):/' /opt/hermes/plugins/platforms/google_chat/adapter.py && \
    /opt/hermes/.venv/bin/python3 -c "import pathlib; p = pathlib.Path('/opt/hermes/plugins/platforms/google_chat/adapter.py'); c = p.read_text(); t = 'url = f\"https://chat.googleapis.com/v1/{chat_id}/messages\"'; r = 'url = f\"https://chat.googleapis.com/v1/{chat_id}/messages\"\n    if thread_id:\n        url += \"?messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD\"'; p.write_text(c.replace(t, r)) if t in c else exit(1)" && \
    grep -q "reversed(list(placeholders.items()))" /opt/hermes/plugins/platforms/google_chat/adapter.py && \
    grep -q "messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD" /opt/hermes/plugins/platforms/google_chat/adapter.py

# Hotfix: Allow explicit target routing for google_chat spaces/threads in send_message_tool
RUN /opt/hermes/.venv/bin/python3 -c "import pathlib; p = pathlib.Path('/opt/hermes/tools/send_message_tool.py'); c = p.read_text(); t = 'def _parse_target_ref(platform_name: str, target_ref: str):\n    \"\"\"Parse a tool target into chat_id/thread_id and whether it is explicit.\"\"\"'; r = t + '\n    if platform_name == \"google_chat\":\n        parts = target_ref.split(\":\", 1)\n        if len(parts) == 2 and parts[0].startswith(\"spaces/\"):\n            return parts[0].strip(), parts[1].strip(), True\n        elif target_ref.startswith(\"spaces/\"):\n            return target_ref.strip(), None, True'; p.write_text(c.replace(t, r)) if t in c else exit(1)"


# 3. Clone and build mcp-remote proxy. Git is a build-only dependency here;
# the sandbox runtime exposes only the credential-proxy Git wrapper.
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    git clone https://github.com/bradhoekstra/mcp-remote.git /opt/mcp-remote && \
    cd /opt/mcp-remote && \
    git checkout 2ff8f16753d8c744e6982b99e6ed5ab62f44f311 && \
    npm install && \
    npm run build && \
    npm prune --production && \
    npm cache clean --force && \
    rm -rf .git

# 5. Create base defaults directory and copy shared defaults
RUN mkdir -p /opt/defaults && chown hermes:hermes /opt/defaults
COPY --chown=hermes:hermes deploy/shared/defaults/ /opt/defaults/
COPY --chown=hermes:hermes docs/glossary.md /opt/defaults/docs/


# 5.5. Install and enable hermes-otel plugin in /opt/defaults
RUN HOME=/opt/defaults HERMES_HOME=/opt/defaults /opt/hermes/.venv/bin/hermes plugins install briancaffey/hermes-otel && \
    HOME=/opt/defaults HERMES_HOME=/opt/defaults /opt/hermes/.venv/bin/hermes plugins enable hermes_otel && \
    /opt/hermes/.venv/bin/python3 -c "import yaml, pathlib; p = pathlib.Path('/opt/defaults/plugins/hermes_otel/config.yaml'); c = yaml.safe_load(p.read_text()) or {} if p.exists() else {}; c['backends'] = [{'name': 'gke-managed-otel', 'type': 'otlp', 'endpoint': 'http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318/v1/traces'}]; p.write_text(yaml.safe_dump(c))"

# Remove the build-only clone dependency after all plugin installation is done.
RUN apt-get purge -y git && apt-get autoremove -y && \
	rm -rf /var/lib/apt/lists/* && \
	for binary in gcloud kubectl gh git; do \
		if command -v "${binary}" >/dev/null 2>&1; then \
			echo "unexpected credential-aware CLI in sandbox image: ${binary}" >&2; \
			exit 1; \
		fi; \
	done

# 5.8. Copy custom agent startup script
COPY deploy/shared/docker-entrypoint.sh /usr/local/bin/agent-entrypoint
COPY deploy/shared/credential-proxy-path.sh /etc/profile.d/credential-proxy-path.sh
COPY agents/platform/scripts/credential_proxy_client.py /usr/local/bin/credential-proxy-exec
RUN chmod 755 /usr/local/bin/agent-entrypoint /usr/local/bin/credential-proxy-exec /etc/profile.d/credential-proxy-path.sh && \
    mkdir -p /opt/credential-proxy/bin && \
    ln -s /usr/local/bin/credential-proxy-exec /opt/credential-proxy/bin/kubectl && \
    ln -s /usr/local/bin/credential-proxy-exec /opt/credential-proxy/bin/gcloud && \
    ln -s /usr/local/bin/credential-proxy-exec /opt/credential-proxy/bin/gh && \
    ln -s /usr/local/bin/credential-proxy-exec /opt/credential-proxy/bin/git

# Copy event-watcher binary from builder stage
COPY --from=watcher-builder /workspace/k8s-event-watcher /usr/local/bin/k8s-event-watcher
RUN chmod 755 /usr/local/bin/k8s-event-watcher

# 6. Expose ports shared by all agent gateways
EXPOSE 8642
EXPOSE 9119

# 7. Execution lifecycle
ENTRYPOINT ["/usr/local/bin/agent-entrypoint"]
CMD ["hermes", "gateway", "run"]



# --- PLATFORM AGENT TARGET ---
FROM agent-base AS platform

# Setup platform specific defaults from consolidated configs
COPY agents/platform/cron/ /opt/defaults/cron/
COPY --chown=hermes:hermes agents/platform/defaults/ /opt/defaults/
COPY agents/platform/scripts/ /opt/defaults/scripts/
COPY agents/platform/governance/ /opt/defaults/governance/
COPY agents/platform/SOUL.md /opt/defaults/SOUL.md
COPY agents/platform/AGENTS.md /opt/defaults/AGENTS.md

# Copy platform skills
COPY agents/platform/skills/ /opt/hermes/skills/

# Copy platform plugins
COPY agents/platform/plugins/ /opt/hermes/plugins/



# Copy platform defaults configuration as temp file
COPY agents/platform/config.yaml /tmp/platform-config.yaml

# Copy config merge helper script
COPY deploy/docker/merge_configs.py /tmp/merge_configs.py

# Dynamically merge config.yamls using helper script
RUN /opt/hermes/.venv/bin/python3 /tmp/merge_configs.py /opt/defaults/config.yaml /tmp/platform-config.yaml && \
    rm -f /tmp/merge_configs.py /tmp/platform-config.yaml

# Normalize ownership and permissions at the end of build
RUN chown -R hermes:hermes /opt/defaults /opt/hermes/skills /opt/hermes/plugins && \
    find /opt/defaults -type d -exec chmod 755 {} + && \
    find /opt/defaults -type f -exec chmod 644 {} + && \
    find /opt/hermes/skills -type d -exec chmod 755 {} + && \
    find /opt/hermes/skills -type f -exec chmod 644 {} + && \
    find /opt/hermes/plugins -type d -exec chmod 755 {} + && \
    find /opt/hermes/plugins -type f -exec chmod 644 {} + && \
    find /opt/defaults/scripts -type f -exec chmod 755 {} + 2>/dev/null && \
    find /opt/hermes/skills -path '*/scripts/*' -type f -exec chmod 755 {} + 2>/dev/null || true


# --- CREDENTIAL PROXY TARGET ---
# Only this image contains the real credential-aware CLIs. The platform target
# above contains wrappers with the same names and cannot execute locally.
FROM platform AS credential-proxy

USER root

COPY --from=envoy-bin /usr/local/bin/envoy /usr/local/bin/envoy
COPY --chmod=0644 deploy/shared/envoy-credential-proxy.yaml /etc/envoy/envoy-credential-proxy.yaml
COPY deploy/shared/envoy-credential-sidecar.sh /usr/local/bin/envoy-credential-sidecar

RUN curl -sSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor --yes -o /usr/share/keyrings/cloud.google.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" > /etc/apt/sources.list.d/google-cloud-sdk.list && \
    apt-get update && apt-get install -y \
      git \
      google-cloud-cli \
      google-cloud-cli-gke-gcloud-auth-plugin \
      kubectl && \
    ARCH=$(dpkg --print-architecture) && \
    curl -fL -o /tmp/gh.deb "https://github.com/cli/cli/releases/download/v2.92.0/gh_2.92.0_linux_${ARCH}.deb" && \
    dpkg -i /tmp/gh.deb && \
    rm -f /tmp/gh.deb && \
    rm -rf /var/lib/apt/lists/* && \
    chmod 755 /usr/local/bin/envoy /usr/local/bin/envoy-credential-sidecar
