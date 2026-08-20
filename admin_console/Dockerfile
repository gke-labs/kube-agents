FROM python:3.11-slim

# Install system dependencies including curl and ca-certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    git \
    apt-transport-https \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install Google Cloud SDK and kubectl
RUN curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | tee -a /etc/apt/sources.list.d/google-cloud-sdk.list && \
    apt-get update && apt-get install -y --no-install-recommends \
    google-cloud-cli \
    google-cloud-cli-gke-gcloud-auth-plugin \
    kubectl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install
COPY admin_console/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy application source
COPY admin_console /app/admin_console

ENV PYTHONPATH="/app:${PYTHONPATH}"
ENV HOST="0.0.0.0"
ENV PORT="8501"
ENV ADMIN_PORTAL_STREAMLIT_PORT="8502"
ENV KUBE_AGENTS_ADMIN_USER="fcurrie@google.com"
ENV KUBE_AGENTS_PORTAL_API_URL="http://127.0.0.1:8501/api/v1"

EXPOSE 8501 8502

CMD ["python3", "-m", "uvicorn", "admin_console.api.main:app", "--host", "0.0.0.0", "--port", "8501", "--workers", "1", "--no-access-log"]
