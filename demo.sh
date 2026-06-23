#!/usr/bin/env bash
# Narrated CLI demo of topf. Runs real commands in a fresh terminal.
#
#   ./demo.sh              run from source in place (topf isn't installed)
#   DEMO_PAUSE=0 ./demo.sh no read-pauses (fast replay / self-test)
#
# topf is a solo single-file Python tool, not on PATH and stable-only:
# no --staging/--next channels exist, so this script has none.
#
# Deliberately NO 'set -e': a demo command may exit non-zero on purpose and
# we want the real exit shown on screen, not the script aborted. A fresh shell
# starts here -> use absolute paths.
set -uo pipefail

SRC=/home/hila/proj/topf                # source dir; assumed not to move (stale ok)
PAUSE=${DEMO_PAUSE:-3}

case ${1:-} in
  '') ;;
  *) echo "usage: $0   (stable-only, no channel flags)" >&2; exit 2 ;;
esac

# Not installed -> run the single file straight from source (stale is fine).
TOOL=$(command -v topf 2>/dev/null) || TOOL="python3 $SRC/topf.py"

say() { printf '# %s\n' "$*"; sleep "$(( PAUSE > 0 ? 1 : 0 ))"; }     # explanation line (tight: no leading blank)
run() { printf '\n$ %s\n' "$*"; eval "$*"; sleep "$PAUSE"; }          # show command, run for real, pause
sec() { printf '\n'; }                                                # one blank line at a section boundary

# --- overview: assume the viewer last saw this months ago, name alone won't do
sec
say "topf -- a 'top' that hides the noise: a full-screen live process viewer"
say "that shows only the INTERESTING subtrees -- ones you named (bazel, ssh,"
say "tmux, claude) plus ones that got heavy (windowed CPU / RSS) -- each row"
say "annotated with cmdline, cwd, binary, open ports, and per-window CPU/RSS."
say "Why: plain 'top' buries the few processes you actually care about."
say "Demoing: $TOOL  (single-file Python, run from source -- not installed)"

# --- proof of life: cheap facts that work even if nothing is 'ready'
run "$TOOL --help 2>&1 | head -6"

# --- the headline: take one real frame of THIS machine and show it
# Live (no --once flag) it's an interactive full-screen TUI; piped or with
# --once it prints a single plain frame -- the original 'psf' behaviour.
# --no-cache keeps it side-effect-free (it otherwise caches socket probes
# under ~/.cache/psf); --no-color so the bytes read cleanly off-terminal.
sec
say "Now watch it take one real frame of this machine's process tree:"
run "$TOOL --once --no-color --no-cache --sample-interval 0.2 2>&1 | head -22"

sec
say "That top line counts total/hidden procs: topf surfaces the few that matter."
say "That's topf. Source: $SRC"
