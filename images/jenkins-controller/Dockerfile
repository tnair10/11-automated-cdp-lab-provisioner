FROM jenkins/jenkins:lts-jdk21

USER root

ARG TERRAFORM_VERSION=1.16.1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      unzip \
      jq \
      git \
      python3 \
      python3-venv \
      docker-cli \
      docker-compose && \
    rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/p11-python && \
    /opt/p11-python/bin/pip install \
      --no-cache-dir \
      PyYAML \
      boto3 \
      requests

ENV PATH="/opt/p11-python/bin:${PATH}"

RUN arch="$(dpkg --print-architecture)" && \
    case "$arch" in \
      arm64) tf_arch="arm64" ;; \
      amd64) tf_arch="amd64" ;; \
      *) echo "Unsupported architecture: $arch"; exit 1 ;; \
    esac && \
    curl -fsSL \
      "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${tf_arch}.zip" \
      -o /tmp/terraform.zip && \
    unzip /tmp/terraform.zip \
      -d /usr/local/bin && \
    rm -f /tmp/terraform.zip

RUN terraform version && \
    python --version && \
    docker --version && \
    docker compose version

USER jenkins
