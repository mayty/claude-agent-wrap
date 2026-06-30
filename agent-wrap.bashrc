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

# Bash completion for `agent`. All verb/flag data lives in the git-tracked
# agent-wrap-completion.bash, compiled from each command module's USAGE string
# by scripts/gen-bash-completion.py (run `make gen-completion` to refresh it).
# Completion is registered only under bash, where `complete`/COMPREPLY exist;
# other shells just get the PATH setup above.
if [ -n "$BASH_VERSION" ] && [ -f "$_agent_wrap_dir/agent-wrap-completion.bash" ]; then
    . "$_agent_wrap_dir/agent-wrap-completion.bash"

    _agent_complete() {
        local cur="${COMP_WORDS[COMP_CWORD]}"
        local verb="${COMP_WORDS[1]}"

        if [[ "$COMP_CWORD" -eq 1 ]]; then
            mapfile -t COMPREPLY < <(compgen -W "$_agent_complete_verbs" -- "$cur")
            return 0
        fi

        # Resolve this verb's flag list from the sourced data via indirect
        # expansion (e.g. _agent_complete_flags_rebuild="--full").
        local flags_var="_agent_complete_flags_${verb}"
        local flags="${!flags_var}"

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

        # Passthrough verbs (e.g. `agent run`) forward remaining args to claude
        # (file paths). Only fall through to file completion once the user has
        # typed a non-flag prefix — bare TAB stays flags-only to avoid dumping
        # the cwd into the menu.
        if [[ " $_agent_complete_passthrough_verbs " == *" $verb "* && -n "$cur" && "$cur" != -* ]]; then
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
fi
