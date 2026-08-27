# agent-name: agent-wrap
# agent-enable-startup: 120
# This file has been created with the assistance of an AI tool.

FROM claude-agent

RUN apt-get update \
    && apt-get install -y \
    python3 \
    python3-pip \
    make \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /tmp/agent-wrap/

RUN pip install --no-cache-dir --break-system-packages '/tmp/agent-wrap[dev]' \
    && rm -rf /tmp/agent-wrap
