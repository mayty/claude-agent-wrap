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

# uv is what `make install` syncs with, and it backs `make constraints-check` and
# `make dump-prod-constraints`. A developer tool only -- bin/agent-bootstrap without
# --dev uses pip against the exported constraints, so the end-user path never needs it.
# Pinned and fetched the same way ops/Dockerfile fetches hadolint and crane.
ARG UV_VERSION=0.12.9
RUN curl -fsSL -o /tmp/uv.tar.gz \
        "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz" \
    && tar -xzf /tmp/uv.tar.gz -C /usr/local/bin --strip-components=1 \
        uv-x86_64-unknown-linux-gnu/uv \
    && chmod 0755 /usr/local/bin/uv \
    && rm -f /tmp/uv.tar.gz

# Staged at /workspace, not a scratch directory: the bootstrap derives everything it reads
# -- and where it provisions -- from its own path, so putting it at the path a real
# checkout uses is what makes `make install` work here with no arguments. `WORKDIR
# /workspace` in the base image is already the cwd. These are the four files --dev reads;
# bin/requirements.txt is not among them, because --dev neither installs nor hashes it.
# All four are removed again further down, so the image carries no phantom half-checkout.
COPY python-pin.env pyproject.toml uv.lock Makefile /workspace/
COPY bin/agent-bootstrap /workspace/bin/

# WORKDIR in the base image leaves /workspace root-owned, so the agent user cannot
# create the mountpoint itself. Doing it here also means the anonymous volume is
# seeded from a non-root-owned directory -- seeded from a root-owned one, the volume
# comes out unwritable.
RUN mkdir -p /workspace/.python && chown ubuntu:ubuntu /workspace/.python

# One step, and the same one contributors and CI run: `make install` is
# `bin/agent-bootstrap --dev`, which provisions the pinned interpreter and then hands the
# whole dependency question to `uv sync --locked` -- prod dependencies and the dev group
# together, out of one lock, into one venv. They must share a sys.path; `make test`
# imports both halves.
#
# The chmod covers a host uid that is not 1000: nothing writes into the tree at
# runtime, so read+execute for everyone is enough. It runs last, after the install.
#
# `venv-current` is what puts the pinned interpreter on PATH below, so that a bare
# `python`/`python3` is it and not the base image's apt python3.12. The venv's own
# directory name embeds the pin and the python-build-standalone target triple, and ENV
# cannot be computed from a RUN -- so the name is resolved once here, out of the pointer
# file that is the tree's own answer to "which venv".
USER ubuntu
RUN make install \
    && uv cache clean \
    && ln -s "$(cat /workspace/.python/current-venv)" /workspace/.python/venv-current \
    && chmod -R a+rX /workspace/.python

# Through the symlinked directory, pyvenv.cfg still sits one level above the
# interpreter, so this is the venv the Makefile's $(PYTHON) is -- dev dependencies
# importable, and pytest/ruff/pyrefly/coverage on PATH as commands too.
ENV PATH=/workspace/.python/venv-current/bin:$PATH

# Put /workspace back to being nothing but the mountpoint. Named explicitly rather than
# a blanket wildcard: `rm -rf /workspace/*` would take .python/ -- the whole point of the
# build -- with it.
USER root
RUN rm -rf /workspace/bin \
    && rm -f /workspace/python-pin.env /workspace/pyproject.toml \
        /workspace/uv.lock /workspace/Makefile

# The wrapper always launches with an explicit --user, so this is only the image's
# default; ubuntu is what ops/validate-dockerfile-agent expects to find.
USER ubuntu
