FROM nousresearch/hermes-agent

USER root

# Install patch utility
RUN apt-get update && apt-get install -y patch && rm -rf /var/lib/apt/lists/*

# Create defaults directory
RUN mkdir -p /opt/defaults

# Copy identity from templates to defaults
COPY workspace/agents/platform/templates/operator/SOUL.md /opt/defaults/SOUL.md

# Setup .env for API server in defaults
RUN echo "API_SERVER_ENABLED=true" > /opt/defaults/.env
RUN echo "HERMES_DASHBOARD=1" >> /opt/defaults/.env

# Copy and apply patch
COPY agents/entrypoint.patch /tmp/entrypoint.patch
RUN patch /opt/hermes/docker/entrypoint.sh < /tmp/entrypoint.patch && rm /tmp/entrypoint.patch

# Expose port
EXPOSE 8642
EXPOSE 9119

# Run gateway
CMD ["hermes", "gateway", "run"]
