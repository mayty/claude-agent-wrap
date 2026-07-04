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

# Bash completion for `agent`. Delegates to the Python completion module
# via AGENT_COMPLETE=1 agent. Registered only under bash,
# where `complete`/COMPREPLY exist; other shells just get the PATH setup.
if [ -n "$BASH_VERSION" ]; then
    _agent_complete() {
        local cur="${COMP_WORDS[COMP_CWORD]}"
        local prev="${COMP_WORDS[COMP_CWORD-1]}"

        # `=` in flag values (e.g. --from=-14d): don't complete
        if [[ "$prev" == *= ]]; then
            COMPREPLY=()
            return 0
        fi

        local result
        result=$(AGENT_COMPLETE=1 agent "$COMP_CWORD" "${COMP_WORDS[@]}" 2>/dev/null) || true

        if [[ -n "$result" ]]; then
            mapfile -t COMPREPLY < <(compgen -W "$result" -- "$cur")
        else
            compopt -o default 2>/dev/null
            COMPREPLY=()
        fi
        return 0
    }

    complete -F _agent_complete agent
fi
