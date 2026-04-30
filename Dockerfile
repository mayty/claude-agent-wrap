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

WORKDIR /workspace

# Install Claude Code globally as root
RUN npm install -g @anthropic-ai/claude-code --os=linux --cpu=x64

RUN mkdir -p /tmp/claude-home/.claude/backups \    
    /tmp/claude-home/.claude/projects \   
    /tmp/claude-home/.claude/sessions && \
    chmod -R 777 /tmp/claude-home

# Make npm global modules accessible to any user
RUN chmod -R a+rX /usr/lib/node_modules

WORKDIR /workspace

ENV CLAUDE_CODE_USE_BEDROCK=1
ENV AWS_REGION=us-east-1

ENTRYPOINT ["claude"]
