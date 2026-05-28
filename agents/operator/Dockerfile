# check=skip=InvalidDefaultArgInFrom
ARG HERMES_AGENT_TAG
FROM nousresearch/hermes-agent:${HERMES_AGENT_TAG}

USER root

# Install patch utility
RUN apt-get update && apt-get install -y patch && rm -rf /var/lib/apt/lists/*

# Create defaults directory
RUN mkdir -p /opt/defaults

# Copy identity from templates to defaults
COPY workspace/agents/platform/templates/operator/SOUL.md /opt/defaults/SOUL.md

# Setup defaults
COPY agents/shared/defaults/ /opt/defaults/
COPY agents/operator/defaults/ /opt/defaults/
RUN chmod -R a+rX /opt/defaults

# Copy and apply patch
COPY agents/shared/entrypoint.patch /tmp/entrypoint.patch
RUN patch /opt/hermes/docker/entrypoint.sh < /tmp/entrypoint.patch && rm /tmp/entrypoint.patch

# Expose port
EXPOSE 8642
EXPOSE 9119

# Run gateway
CMD ["hermes", "gateway", "run"]
