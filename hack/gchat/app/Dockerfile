FROM nousresearch/hermes-agent:sha-bec2250d2c8349fc85201bcd1aa39bcaa766a555

USER root

# Install required system utilities, gcloud, and kubectl natively
RUN apt-get update && apt-get install -y apt-transport-https ca-certificates gnupg curl && \
    curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | tee -a /etc/apt/sources.list.d/google-cloud-sdk.list && \
    apt-get update && apt-get install -y google-cloud-cli google-cloud-cli-gke-gcloud-auth-plugin kubectl && \
    rm -rf /var/lib/apt/lists/*

# Install Google Chat dependencies in the virtualenv using uv
RUN /usr/local/bin/uv pip install google-cloud-pubsub==2.38.0 --python /opt/hermes/.venv/bin/python

WORKDIR /opt/data
CMD ["hermes", "gateway", "run"]
