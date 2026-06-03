FROM python:3.11-slim

WORKDIR /app

# Install system dependencies needed for cryptography compilation
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libssl-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Install python-cryptography
RUN pip install --no-cache-dir cryptography

# Copy application script
COPY broker.py .

EXPOSE 8080

ENV PORT=8080

ENTRYPOINT ["python3", "broker.py"]
