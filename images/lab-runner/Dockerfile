FROM python:3.11-slim

ARG TERRAFORM_VERSION=1.16.1

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      unzip \
      jq \
      git \
      openssh-client \
      less \
      groff && \
    rm -rf /var/lib/apt/lists/*

RUN arch="$(dpkg --print-architecture)" && \
    case "$arch" in \
      arm64) tf_arch="arm64" ;; \
      amd64) tf_arch="amd64" ;; \
      *) echo "Unsupported architecture: $arch"; exit 1 ;; \
    esac && \
    curl -fsSL \
      "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${tf_arch}.zip" \
      -o /tmp/terraform.zip && \
    unzip /tmp/terraform.zip -d /usr/local/bin && \
    rm -f /tmp/terraform.zip

RUN pip install --no-cache-dir \
      awscli \
      boto3 \
      requests \
      PyYAML

WORKDIR /workspace

CMD ["bash"]
