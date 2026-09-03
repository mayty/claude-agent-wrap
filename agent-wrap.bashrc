# This file has been edited with the assistance of an AI tool.
#
# agent-wrap: Docker-based wrapper for running Claude Code CLI.
#
# The `agent` command itself is the bin/agent executable; all logic lives in
# agent_wrap/ (via __main__.py). Sourcing this file does two things: it puts
# bin/ on PATH so `agent` resolves (including for child/subprocess callers),
# and it registers the bash completion below.

_agent_wrap_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Prepend bin/ to PATH if absent (idempotent — re-sourcing won't duplicate it).
case ":$PATH:" in
    *":$_agent_wrap_dir/bin:"*) ;;
    *) PATH="$_agent_wrap_dir/bin:$PATH" ;;
esac
export PATH

# Bash completion for `agent`. This is click's own generated completion function,
# inlined verbatim rather than sourced via `eval "$(_AGENT_COMPLETE=bash_source agent)"`:
# that form forks a Python interpreter on every shell startup, and on a checkout that is
# not provisioned yet bin/agent exits 0 immediately, so the eval would register nothing
# at all until the next new shell. Regenerate with
# `_AGENT_COMPLETE=bash_source agent` if click's template changes.
#
# Registered only under bash, where `complete`/COMPREPLY exist; other shells just get
# the PATH setup above. `complete -o nosort` needs bash >= 4.4.
if [ -n "$BASH_VERSION" ]; then
    _agent_completion() {
        local IFS=$'\n'
        local response

        response=$(env COMP_WORDS="${COMP_WORDS[*]}" COMP_CWORD=$COMP_CWORD _AGENT_COMPLETE=bash_complete $1)

        for completion in $response; do
            IFS=',' read type value <<< "$completion"

            if [[ $type == 'dir' ]]; then
                COMPREPLY=()
                compopt -o dirnames
            elif [[ $type == 'file' ]]; then
                COMPREPLY=()
                compopt -o default
            elif [[ $type == 'plain' ]]; then
                COMPREPLY+=($value)
            fi
        done

        return 0
    }

    _agent_completion_setup() {
        complete -o nosort -F _agent_completion agent
    }

    _agent_completion_setup;
fi
