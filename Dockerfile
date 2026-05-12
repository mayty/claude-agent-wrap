# This file has been edited with the assistance of an AI tool.
FROM ubuntu:24.04

ARG HOST_UID
ARG HOST_GID

RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    git \
    jq \
    file \
    && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code globally as root
RUN npm install -g @anthropic-ai/claude-code --os=linux --cpu=x64

# Make npm global modules accessible to any user
RUN chmod -R a+rX /usr/lib/node_modules

# Install hadolint (Dockerfile linter) and crane (registry client used by the
# Dockerfile.agent validator to probe base images without a Docker daemon).
RUN curl -fsSL -o /usr/local/bin/hadolint \
        https://github.com/hadolint/hadolint/releases/download/v2.14.0/hadolint-Linux-x86_64 \
    && chmod 0755 /usr/local/bin/hadolint \
    && curl -fsSL -o /tmp/crane.tar.gz \
        https://github.com/google/go-containerregistry/releases/download/v0.21.5/go-containerregistry_Linux_x86_64.tar.gz \
    && tar -xzf /tmp/crane.tar.gz -C /usr/local/bin crane \
    && chmod 0755 /usr/local/bin/crane \
    && rm -f /tmp/crane.tar.gz

WORKDIR /workspace
RUN git config --system --add safe.directory /workspace

ENTRYPOINT ["claude"]
