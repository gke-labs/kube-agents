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

# Hotfix: stop the kanban notifier from severing URLs in a card's completion line.
# Upstream hard-slices the handoff at a fixed width, which turned a published
# ledger link into ".../is" in chat. Swap both slices for a whitespace-boundary
# clip that also stops discarding every line after the first. See the module
# docstring in deploy/docker/patches/kanban_handoff_clip.py.
COPY deploy/docker/patches/kanban_handoff_clip.py /opt/hermes/gateway/kanban_handoff_clip.py
RUN /opt/hermes/.venv/bin/python3 -c "import pathlib; p = pathlib.Path('/opt/hermes/gateway/kanban_watchers.py'); c = p.read_text(); t1 = 'h = lines[0][:200] if lines else payload_summary[:200]'; t2 = 'r = lines[0][:160] if lines else task.result[:160]'; exit(1) if (t1 not in c or t2 not in c) else None; c = c.replace(t1, 'h = _clip_handoff(payload_summary)').replace(t2, 'r = _clip_handoff(task.result)'); p.write_text(c + '\n\n# kube-agents patch: see gateway/kanban_handoff_clip.py\nfrom gateway.kanban_handoff_clip import clip_handoff as _clip_handoff\n')" && \
    grep -q "h = _clip_handoff(payload_summary)" /opt/hermes/gateway/kanban_watchers.py && \
    grep -q "r = _clip_handoff(task.result)" /opt/hermes/gateway/kanban_watchers.py && \
    /opt/hermes/.venv/bin/python3 -c "import ast, pathlib; ast.parse(pathlib.Path('/opt/hermes/gateway/kanban_watchers.py').read_text())"

# Hotfix: make a dispatched cron run report back, and stop it closing the
# kanban card of the caller that dispatched it. Upstream discards the run's
# final response and lets the run inherit HERMES_KANBAN_TASK, which is how
# three fleet-audit streams published wrong ledgers on 2026-08-04. Nine
# anchored edits across three files, so the surgery lives in an applier script
# rather than inline; it fails the build on any anchor that no longer matches.
# verify_cron_run_scope.py then exercises the patched code. The applier only
# proves the anchors matched, and the first regression here was a runtime type
# error no anchor could have caught: save_job_output returns a pathlib.Path,
# the run action json.dumps it, and the caller got a TypeError in place of the
# report it had waited six minutes for.
# See the module docstring in deploy/docker/patches/cron_run_scope.py.
COPY deploy/docker/patches/cron_run_scope.py /opt/hermes/tools/cron_run_scope.py
COPY deploy/docker/patches/apply_cron_run_scope.py /tmp/apply_cron_run_scope.py
COPY deploy/docker/patches/verify_cron_run_scope.py /tmp/verify_cron_run_scope.py
RUN /opt/hermes/.venv/bin/python3 /tmp/apply_cron_run_scope.py /opt/hermes && \
    grep -q "outcome=None) -> bool:" /opt/hermes/cron/scheduler.py && \
    grep -q "with cron_run_scope(job_id):" /opt/hermes/tools/cronjob_tools.py && \
    grep -q "cron_ownership_violation(tid)" /opt/hermes/tools/kanban_tools.py && \
    grep -q 'outcome\["output_file"\] = str(output_file)' /opt/hermes/cron/scheduler.py && \
    cd /opt/hermes && /opt/hermes/.venv/bin/python3 -c "\
from gateway.kanban_handoff_clip import clip_handoff; \
from tools.cron_run_scope import clip_cron_response, cron_run_scope; \
clipped = clip_cron_response(('word ' * 2000) + 'https://example.invalid/issues/27'); \
exit(1) if ('https://' in clipped or not 3000 < len(clipped) <= 4000) else None" && \
    /opt/hermes/.venv/bin/python3 /tmp/verify_cron_run_scope.py && \
    rm /tmp/apply_cron_run_scope.py /tmp/verify_cron_run_scope.py


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
COPY --chown=hermes:hermes agents/platform/docs/glossary.md /opt/defaults/docs/
COPY --chown=hermes:hermes agents/platform/docs/gcp-console-links.md /opt/defaults/docs/


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



# --- AGENT GATEWAY TARGET ---
# One image, one pod, multiple co-located Hermes profiles sharing one PVC:
#   - `default`   = Chat Agent (the front door; receives chat ingress) — synced
#                   from /opt/defaults into $HERMES_HOME at startup.
#   - `platform`  = Platform Agent specialist — scaffolded at startup from
#                   /opt/platform-template (see docker-entrypoint.sh).
#   - `cluster-*` = per-cluster read-only agents — scaffolded at runtime from
#                   /opt/cluster-template (see scripts/cluster_agent_profile.py).
FROM agent-base AS platform

# --- Chat Agent (the `default` profile) ---
# Chat ingress plugins/hooks (session_store, session_otel_bridge, tool_call_audit,
# chat_message_audit) live with the front door that handles ingress.
COPY --chown=hermes:hermes agents/chat/defaults/ /opt/defaults/
COPY agents/chat/SOUL.md /opt/defaults/SOUL.md
COPY agents/chat/AGENTS.md /opt/defaults/AGENTS.md
# Self-contained Chat Agent config: overwrite (NOT merge) the shared-defaults
# config so the front door stays minimal (router tools only, no cloud/write tools).
COPY agents/chat/config.yaml /opt/defaults/config.yaml

# --- Shared scripts (reachable by every profile at $HERMES_HOME/scripts) ---
# Executable scripts are shared, not per-profile: the router (Chat Agent) and the
# platform/cluster tooling all resolve under $HERMES_HOME/scripts.
COPY agents/chat/scripts/ /opt/defaults/scripts/
COPY agents/platform/scripts/ /opt/defaults/scripts/
# Merges the operator's per-profile config overlays at startup (entrypoint step 2.7).
# Shipped as a file rather than inlined in the entrypoint so it can be unit tested —
# see tests/test_profile_overlay.py.
COPY deploy/shared/profile_overlay.py /opt/defaults/scripts/profile_overlay.py
# Links profile-targeted plugin image volumes into their profile homes (entrypoint step
# 2.65, and cluster_agent_profile.py for profiles scaffolded later) — see
# tests/test_profile_plugins.py.
COPY deploy/shared/profile_plugins.py /opt/defaults/scripts/profile_plugins.py

# --- Platform Agent specialist profile TEMPLATE (scaffolded at startup) ---
COPY --chown=hermes:hermes agents/platform/SOUL.md /opt/platform-template/SOUL.md
COPY --chown=hermes:hermes agents/platform/AGENTS.md /opt/platform-template/AGENTS.md
COPY --chown=hermes:hermes agents/platform/CAPABILITIES.md /opt/platform-template/CAPABILITIES.md
COPY --chown=hermes:hermes agents/platform/skills/ /opt/platform-template/skills/
COPY --chown=hermes:hermes agents/platform/cron/ /opt/platform-template/cron/
COPY --chown=hermes:hermes agents/platform/governance/ /opt/platform-template/governance/

# Platform config is self-contained (scaffold does not merge again): build it by
# merging the shared defaults base with the platform overrides and bake it into
# the template. This reproduces the exact config the platform ran with as the
# former default profile.
COPY deploy/shared/defaults/config.yaml /tmp/platform-base-config.yaml
COPY agents/platform/config.yaml /tmp/platform-overlay-config.yaml
COPY deploy/docker/merge_configs.py /tmp/merge_configs.py
RUN /opt/hermes/.venv/bin/python3 /tmp/merge_configs.py /tmp/platform-base-config.yaml /tmp/platform-overlay-config.yaml && \
    cp /tmp/platform-base-config.yaml /opt/platform-template/config.yaml && \
    rm -f /tmp/merge_configs.py /tmp/platform-base-config.yaml /tmp/platform-overlay-config.yaml

# Copy the Cluster Agent profile TEMPLATE (persona, config, skills), scaffolded
# into per-cluster Hermes profiles at runtime by scripts/cluster_agent_profile.py.
COPY --chown=hermes:hermes agents/cluster/ /opt/cluster-template/

# Shared plugins available to all profiles (memory backend used by the Chat Agent).
COPY agents/platform/plugins/ /opt/hermes/plugins/

# NOTE: the platform overlay is no longer merged into /opt/defaults/config.yaml.
# /opt/defaults now belongs to the Chat Agent (`default` profile) and its config
# is copied verbatim above; the platform overlay is merged into
# /opt/platform-template/config.yaml instead (see the RUN block above).

# Normalize ownership and permissions at the end of build.
#
# `set -eu` rather than an `&&` chain ending in `|| true`: that trailing `|| true`
# bound to the WHOLE chain, so a failure in any early command short-circuited
# every later chmod and still exited 0 — a build that silently shipped
# unnormalized permissions. Each command must now fail the build loudly.
# Every path below is guaranteed to exist at this point: /opt/hermes/skills and
# /opt/hermes/plugins come from the base image, the rest are COPYed above.
RUN set -eu; \
    chown -R hermes:hermes /opt/defaults /opt/hermes/skills /opt/hermes/plugins /opt/cluster-template /opt/platform-template; \
    for d in /opt/defaults /opt/hermes/skills /opt/hermes/plugins /opt/cluster-template /opt/platform-template; do \
        find "$d" -type d -exec chmod 755 {} + ; \
        find "$d" -type f -exec chmod 644 {} + ; \
    done; \
    find /opt/defaults/scripts -type f -exec chmod 755 {} + ; \
    for d in /opt/hermes/skills /opt/platform-template /opt/cluster-template; do \
        find "$d" -path '*/scripts/*' -type f -exec chmod 755 {} + ; \
    done


# --- CREDENTIAL PROXY TARGET ---
# Only this image contains the real credential-aware CLIs. The platform target
# above contains wrappers with the same names and cannot execute locally.
FROM platform AS credential-proxy

USER root

COPY --from=envoy-bin /usr/local/bin/envoy /usr/local/bin/envoy
RUN mkdir -p /etc/envoy
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
    chmod 755 /etc/envoy /usr/local/bin/envoy /usr/local/bin/envoy-credential-sidecar
