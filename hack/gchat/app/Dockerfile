FROM nousresearch/hermes-agent:latest

USER root

# Install required system dependencies
RUN apt-get update && apt-get install -y patch && rm -rf /var/lib/apt/lists/*

# Create the data directory and assign ownership to the non-root user (UID 10000)
RUN mkdir -p /opt/data && chown -R 10000:10000 /opt/data

# Use uv to install GCP-specific Python packages into the existing virtual environment
RUN uv pip install --python /opt/hermes/.venv/bin/python \
    google-cloud-pubsub google-api-python-client google-auth google-auth-oauthlib && \
    chown -R 10000:10000 /opt/hermes/.venv

# Set the working directory to the persistent volume mount point
WORKDIR /opt/data
