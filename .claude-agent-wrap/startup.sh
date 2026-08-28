#!/usr/bin/env bash
# This file has been created with the assistance of an AI tool.
#
# Rebuild this project's image when it has fallen behind the base image.
#
# `agent rebuild --full` builds the base and then the project image; a --full that
# stopped halfway, or a base rebuilt from some other project's directory, leaves
# claude-agent-agent-wrap carrying an older Claude Code than claude-agent does, with
# nothing announcing it. Compare the two and rebuild before the container starts.
#
# The images carry no version of their own, so the comparable is the Claude Code
# release installed inside each one, which is what `agent inspect` reports.
set -euo pipefail

# --lite drops the npm-registry check and the logs-tree walk -- the two slow steps,
# and this runs holding the host-global startup lock. `|| true`: inspect exits 1 when
# the Docker daemon is unreachable, and still prints a parseable document of nulls.
report=$("$AGENT_BINARY" inspect --lite --json) || true

# has_project distinguishes "this project declares no image" from "its image is not
# built yet" -- both leave .project.claude_version null, and only the second rebuilds.
read -r has_project present base project <<<"$(
    printf '%s' "$report" | jq -r '
        [.project != null,
         .project.present // false,
         .environment.base_image_version // "unknown",
         .project.claude_version // "unknown"] | @tsv
    ' 2>/dev/null
)" || true

if [ "${has_project:-false}" != "true" ] || [ "${base:-unknown}" = "unknown" ]; then
    # Docker down, base image absent, or this project declares no image of its own.
    # A check that cannot read its inputs must not block the launch.
    echo "startup: could not compare image versions -- skipping the rebuild check."
    exit 0
fi

if [ "$present" != "true" ]; then
    echo "startup: project image is not built -- rebuilding."
elif [ "$project" = "unknown" ]; then
    echo "startup: could not read the project image's Claude Code version -- skipping."
    exit 0
elif [ "$project" = "$base" ]; then
    echo "startup: project image is current with the base (Claude Code v$base)."
    exit 0
else
    echo "startup: project image has Claude Code v$project, base has v$base -- rebuilding."
fi

# exec: the rebuild's exit code becomes this script's, with no shell in between to
# translate it. The wrapper's timeout signals this script's whole process group, so the
# rebuild -- and the `docker build` under it -- are torn down either way.
exec "$AGENT_BINARY" rebuild
