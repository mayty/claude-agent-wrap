# This file has been edited with the assistance of an AI tool.
#
# Two stages on purpose. The wrapper builds this file with docker's layer cache ON -- its
# own recipe is the only one it trusts that far -- so everything expensive and stable
# lives in `scaffold` and is reused between builds, while the Claude Code CLI, which
# ships a release most days, lives in the final stage and is reinstalled every time.
# See BuildService._docker_build.
FROM ubuntu:24.04 AS scaffold

ARG HOST_UID
ARG HOST_GID

# The wrapper's DOCKER_BUILD_ITERATION, *referenced* rather than merely declared: on
# BuildKit an unused ARG invalidates nothing, so the reference is what makes a bump reach
# these layers on both builders. Being the first RUN of the stage, it invalidates the
# whole scaffold -- which is exactly what a bump has always meant. The file it writes also
# answers "which recipe built this image?" from inside a container, alongside the
# agent-wrap.build-iteration label docker carries outside it.
ARG BUILD_ITERATION=0
RUN printf '%s\n' "$BUILD_ITERATION" > /etc/agent-wrap-build-iteration

RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    git \
    jq \
    file \
    wl-clipboard \
    xclip \
    imagemagick \
    hunspell \
    && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Dictionaries for Claude Code's prompt spell checking, which shells out to
# `hunspell -a -i utf-8 -d <language>`. The list is comma-separated and a word found in
# any of its dictionaries counts as correct, which is what makes mixed-language prompts
# work. SPELLCHECK_LANG carries the wrapper's AGENT_SPELLCHECK_LANG, and the same value
# is written into the global settings.json: every entry must resolve to an installed
# dictionary here, or hunspell fails to start and spell checking stays off entirely.
#
# Upstream package names are inconsistent -- hunspell-en-us and hunspell-de-de are
# region-qualified, hunspell-ru and hunspell-fr are not -- so both spellings are tried
# before the build is failed. A silent miss would leave the feature dead with no signal.
ARG SPELLCHECK_LANG=en_US,ru_RU
RUN set -eu; \
    apt-get update; \
    IFS=,; \
    for lang in $SPELLCHECK_LANG; do \
        [ -n "$lang" ] || continue; \
        base=$(printf '%s' "${lang%%_*}" | tr '[:upper:]' '[:lower:]'); \
        region=$(printf '%s' "${lang#*_}" | tr '[:upper:]' '[:lower:]'); \
        apt-get install -y "hunspell-$base-$region" \
        || apt-get install -y "hunspell-$base" \
        || { echo "ERROR: no hunspell dictionary package for '$lang' (tried hunspell-$base-$region, hunspell-$base)" >&2; exit 1; }; \
    done; \
    rm -rf /var/lib/apt/lists/*

# Install hadolint (Dockerfile linter) and crane (registry client used by the
# project Dockerfile validator to probe base images without a Docker daemon).
RUN curl -fsSL -o /usr/local/bin/hadolint \
        https://github.com/hadolint/hadolint/releases/download/v2.14.0/hadolint-Linux-x86_64 \
    && chmod 0755 /usr/local/bin/hadolint \
    && curl -fsSL -o /tmp/crane.tar.gz \
        https://github.com/google/go-containerregistry/releases/download/v0.21.5/go-containerregistry_Linux_x86_64.tar.gz \
    && tar -xzf /tmp/crane.tar.gz -C /usr/local/bin crane \
    && chmod 0755 /usr/local/bin/crane \
    && rm -f /tmp/crane.tar.gz

# Pre-create the parents of the MCP log bind mount. Docker materializes missing
# mount parents as root:root, which would leave the agent user unable to write
# anything else under ~/.cache (e.g. the version-check cache).
RUN mkdir -p /home/ubuntu/.cache/claude-cli-nodejs \
    && chown -R ubuntu:ubuntu /home/ubuntu/.cache

WORKDIR /workspace
RUN git config --system --add safe.directory /workspace


# The Claude Code CLI, reinstalled on every build. The wrapper passes a
# CLAUDE_CACHE_BUST whose value differs each time and the RUN below references it, so
# this layer never hits the cache and every base build lands the current release.
#
# Declared in THIS stage only: on the classic builder every valued ARG in a stage
# participates in that stage's cache keys, so declaring it above would rotate the
# scaffold's keys on every build and defeat the split. For the same reason the reference
# shares the `npm install` RUN -- referenced from a later one, the marker would re-run
# while the install cached, which is the exact opposite of the point.
#
# Nothing secret may ever be routed through this arg: its value is visible in
# `docker history` and in the image itself.
FROM scaffold AS agent

ARG CLAUDE_CACHE_BUST=unset
RUN set -eu; \
    printf '%s\n' "$CLAUDE_CACHE_BUST" > /etc/agent-wrap-cache-bust; \
    npm install -g @anthropic-ai/claude-code --os=linux --cpu=x64; \
    chmod -R a+rX /usr/lib/node_modules

ENTRYPOINT ["claude"]
