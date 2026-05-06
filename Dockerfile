# This file has been edited with the assistance of an AI tool.
FROM ubuntu:24.04

ARG HOST_UID
ARG HOST_GID

RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    git \
    && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code globally as root
RUN npm install -g @anthropic-ai/claude-code --os=linux --cpu=x64

# Make npm global modules accessible to any user
RUN chmod -R a+rX /usr/lib/node_modules

WORKDIR /workspace
RUN git config --system --add safe.directory /workspace

ENV CLAUDE_CODE_USE_BEDROCK=1
ENV AWS_REGION=us-east-1

ENTRYPOINT ["claude"]
