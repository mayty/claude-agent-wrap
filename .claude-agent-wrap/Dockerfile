# agent-name: agent-wrap
# agent-enable-startup: 600
# The container gets its OWN provisioned interpreter at the SAME path the host
# uses. This project's directory is the wrapper's own tool directory, so the
# host's .python/ is bind-mounted in at /workspace/.python — and on a macOS host
# that is a Mach-O interpreter inside a Linux container. An anonymous volume
# shadows it with the tree baked in below, which is what lets bin/agent and the
# Makefile use one relative path everywhere with no override variable.
# agent-run-args: -v /workspace/.python
# This file has been edited with the assistance of an AI tool.

FROM claude-agent

RUN apt-get update \
    && apt-get install -y \
    make \
    && rm -rf /var/lib/apt/lists/*

COPY python-pin.env /tmp/agent-wrap/
COPY pyproject.toml /tmp/agent-wrap/
COPY bin/agent-bootstrap /tmp/agent-wrap/bin/

# WORKDIR in the base image leaves /workspace root-owned, so the agent user cannot
# create the mountpoint itself. Doing it here also means the anonymous volume is
# seeded from a non-root-owned directory -- seeded from a root-owned one, the volume
# comes out unwritable.
RUN mkdir -p /workspace/.python && chown ubuntu:ubuntu /workspace/.python

# The chmod covers a host uid that is not 1000: nothing writes into the tree at
# runtime, so read+execute for everyone is enough.
USER ubuntu
RUN /tmp/agent-wrap/bin/agent-bootstrap --dev --prefix /workspace/.python \
    && chmod -R a+rX /workspace/.python
USER root
RUN rm -rf /tmp/agent-wrap

# The wrapper always launches with an explicit --user, so this is only the image's
# default; ubuntu is what ops/validate-dockerfile-agent expects to find.
USER ubuntu
