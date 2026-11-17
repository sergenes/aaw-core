# Agents At Work Core shell integration. Loaded by:  eval "$(aaw shell-init)"
#
# Typing claude, codex, gemini, grok, cursor-agent or scoot in a folder starts a
# bridged session (tmux + daemon) and attaches to it, so the phone follows along.
# Runs under bash and zsh alike.

# True only for an interactive launch of the agent itself. A --version or --help
# probe, print mode, output piped into another program, or another tool calling
# the binary must reach the real binary untouched: starting the bridge from such
# a call would create a phantom session and daemon.
_aaw_interactive() {
    [ -t 1 ] || return 1
    local a
    for a in "$@"; do
        case "$a" in
            --version|-v|-V|--help|-h|-p|--print|--output-format|--output-format=*) return 1 ;;
        esac
    done
    return 0
}

# _aaw_run AGENT BINARY ARGS...: start (or find) the bridged session for $PWD and
# attach to it; fall back to the plain binary when the host is off or unlinked.
_aaw_run() {
    local agent="$1" bin="$2"; shift 2
    if ! _aaw_interactive "$@" || [ -n "${TMUX:-}" ]; then
        command "$bin" "$@"
        return
    fi
    local enabled="${AAW_STATE_DIR:-$HOME/.aaw}/enabled"
    if ! command -v aaw >/dev/null 2>&1 || [ ! -f "$enabled" ]; then
        command "$bin" "$@"
        return
    fi
    # The host decides the session id: a second agent on this folder gets its own
    # (proj-codex), so read the id back rather than assuming the folder name.
    local sid
    sid="$(aaw start "$PWD" --agent "$agent" --porcelain 2>/dev/null)"
    if [ -n "$sid" ] && tmux has-session -t "=aaw-$sid" 2>/dev/null; then
        tmux attach -t "=aaw-$sid"
    else
        command "$bin" "$@"
    fi
}

# Each wrapper stands on its own: a tool that copies the shell's functions into a shell
# of its own may leave the helpers above behind (Claude Code's Bash tool drops the names
# that start with an underscore), and the wrapper must then reach the real binary instead
# of failing with "command not found: _aaw_run".
claude() {
    if command -v _aaw_run >/dev/null 2>&1; then _aaw_run claude claude "$@"; else command claude "$@"; fi
}
codex() {
    if command -v _aaw_run >/dev/null 2>&1; then _aaw_run codex codex "$@"; else command codex "$@"; fi
}
gemini() {
    if command -v _aaw_run >/dev/null 2>&1; then _aaw_run gemini gemini "$@"; else command gemini "$@"; fi
}
grok() {
    if command -v _aaw_run >/dev/null 2>&1; then _aaw_run grok grok "$@"; else command grok "$@"; fi
}
# Cursor's CLI is cursor-agent (never bare `agent`: Grok's CLI installs one too).
cursor-agent() {
    if command -v _aaw_run >/dev/null 2>&1; then _aaw_run cursor cursor-agent "$@"; else command cursor-agent "$@"; fi
}
# scoot's non-REPL invocations go straight to the real binary.
scoot() {
    case "${1:-}" in
        models|auth|--version|-v|-V|--help|-h|--json|--headless|-p|--print) command scoot "$@"; return ;;
    esac
    if command -v _aaw_run >/dev/null 2>&1; then _aaw_run scoot scoot "$@"; else command scoot "$@"; fi
}
