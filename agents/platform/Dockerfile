FROM nousresearch/hermes-agent:sha-bec2250d2c8349fc85201bcd1aa39bcaa766a555

USER root

# Install system utilities, gcloud CLI, GKE auth plugin, patch, and kubectl
# Re-added patch utility as it is required by the entrypoint.patch step
RUN apt-get update && apt-get install -y apt-transport-https ca-certificates gnupg curl patch && \
    curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | tee -a /etc/apt/sources.list.d/google-cloud-sdk.list && \
    apt-get update && apt-get install -y google-cloud-cli google-cloud-cli-gke-gcloud-auth-plugin && \
    rm -rf /var/lib/apt/lists/*

# Download and install kubectl binary directly to bypass OpenPGP v3 signature deprecation
RUN curl -fsSL -o /usr/local/bin/kubectl "https://dl.k8s.io/release/v1.30.0/bin/linux/amd64/kubectl" && \
    chmod +x /usr/local/bin/kubectl

# Install Google Chat dependencies
RUN uv pip install google-cloud-pubsub google-api-python-client google-auth google-auth-oauthlib

# Create defaults directory
RUN mkdir -p /opt/defaults

# Copy identity from templates to defaults
COPY agents/platform/SOUL.md /opt/defaults/SOUL.md

# Setup defaults
COPY agents/shared/defaults/ /opt/defaults/
COPY agents/platform/defaults/ /opt/defaults/
RUN chmod -R a+rX /opt/defaults

# Copy and apply patch
COPY agents/shared/entrypoint.patch /tmp/entrypoint.patch
RUN patch /opt/hermes/docker/entrypoint.sh < /tmp/entrypoint.patch && rm /tmp/entrypoint.patch

# Expose ports
EXPOSE 8642
EXPOSE 9119

# Run gateway
CMD ["hermes", "gateway", "run"]
