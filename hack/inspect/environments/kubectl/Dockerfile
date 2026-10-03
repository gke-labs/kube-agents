FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL "https://dl.k8s.io/release/v1.33.1/bin/linux/$(dpkg --print-architecture)/kubectl" -o /usr/local/bin/kubectl && chmod +x /usr/local/bin/kubectl
