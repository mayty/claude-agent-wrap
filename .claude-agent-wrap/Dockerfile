# agent-name: agent-wrap
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
#
# `venv-current` is what puts the pinned interpreter on PATH below, so that a bare
# `python`/`python3` is it and not the base image's apt python3.12. The venv's own
# directory name embeds the pin AND the python-build-standalone target triple, and
# ENV cannot be computed from a RUN -- so the slug is resolved once here, the same
# way `current` itself is the tree's answer to "which slug".
USER ubuntu
RUN /tmp/agent-wrap/bin/agent-bootstrap --dev --prefix /workspace/.python \
    && ln -s "venv-$(cat /workspace/.python/current)" /workspace/.python/venv-current \
    && chmod -R a+rX /workspace/.python

# Through the symlinked directory, pyvenv.cfg still sits one level above the
# interpreter, so this is the venv the Makefile's $(PYTHON) is -- dev dependencies
# importable, and pytest/ruff/pyrefly/coverage on PATH as commands too.
ENV PATH=/workspace/.python/venv-current/bin:$PATH

USER root
RUN rm -rf /tmp/agent-wrap

# The wrapper always launches with an explicit --user, so this is only the image's
# default; ubuntu is what ops/validate-dockerfile-agent expects to find.
USER ubuntu
