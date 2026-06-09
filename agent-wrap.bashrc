# This file has been edited with the assistance of an AI tool.
#
# agent-wrap: Docker-based wrapper for running Claude Code CLI.
#
# Thin dispatcher — all logic lives in agent_wrap/ (via __main__.py). This
# function just forwards to the Python implementation.

_agent_wrap_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

agent() {
    PYTHONPATH="$_agent_wrap_dir" python3 -m agent_wrap "$@"
}

# Bash completion for `agent`. Verbs are discovered dynamically by globbing
# agent_wrap/commands/, mirroring _discover_commands() in __main__.py. Flag
# lists are hardcoded per verb; tests/test_bash_completion.py guards drift
# between these flags and each command module's USAGE string.
_agent_complete() {
    local cur="${COMP_WORDS[COMP_CWORD]}"
    local verb="${COMP_WORDS[1]}"

    if [[ "$COMP_CWORD" -eq 1 ]]; then
        local verbs=()
        local f name
        for f in "$_agent_wrap_dir"/agent_wrap/commands/*.py; do
            name="${f##*/}"
            name="${name%.py}"
            [[ "$name" == _* ]] && continue
            verbs+=("$name")
        done
        mapfile -t COMPREPLY < <(compgen -W "${verbs[*]}" -- "$cur")
        return 0
    fi

    local flags=""
    case "$verb" in
        rebuild) flags="--full" ;;
        run)     flags="--base" ;;
        stats)         flags="--days" ;;
        legacy_stats)  flags="--days" ;;
        logs)          flags="--port" ;;
        create|update) flags="" ;;
    esac

    # Drop flags already present on the command line so repeated TAB on
    # `agent rebuild --full ` doesn't keep re-inserting --full.
    local -a available=()
    local f i seen
    for f in $flags; do
        seen=0
        for ((i = 2; i < COMP_CWORD; i++)); do
            if [[ "${COMP_WORDS[i]}" == "$f" ]]; then
                seen=1
                break
            fi
        done
        [[ "$seen" -eq 0 ]] && available+=("$f")
    done

    # `agent run` passes remaining args to claude (file paths). Only fall
    # through to file completion once the user has typed a non-flag prefix —
    # bare TAB stays flags-only to avoid dumping the cwd into the menu.
    if [[ "$verb" == "run" && -n "$cur" && "$cur" != -* ]]; then
        mapfile -t COMPREPLY < <(compgen -f -- "$cur")
        return 0
    fi

    mapfile -t COMPREPLY < <(compgen -W "${available[*]}" -- "$cur")

    # On bare TAB, suppress bash's auto-insertion of single matches and the
    # longest common prefix (e.g. `--` for stats's three `--*` flags). Append
    # a no-prefix sentinel so the menu is shown verbatim — user then types
    # the leading char and TAB to actually pull a flag in.
    if [[ -z "$cur" && ${#COMPREPLY[@]} -ge 1 ]]; then
        COMPREPLY+=(" ")
    fi
    return 0
}

complete -F _agent_complete agent
