#!/usr/bin/env bash
# Narrated CLI demo of psf/topf. Runs real, read-only commands in a fresh terminal.
#
#   ./demo.sh              run the tools (from source in place)
#   DEMO_PAUSE=0 ./demo.sh   no read-pauses (fast replay / self-test)
#
# Stable-only repo: two single-file tools, no staging/next channels. Nothing is
# installed on PATH, so we run them straight from source with python3 (stale ok).
#
# Deliberately NO 'set -e': a demo command may exit non-zero on purpose and we
# want the real exit shown, not the script aborted. Fresh shell -> absolute paths.
set -uo pipefail

SRC=/home/hila/proj/psf                # source dir; assumed not to move (stale ok)
PAUSE=${DEMO_PAUSE:-3}
PY=$(command -v python3)

# Prefer an installed console script; else run the module from source in place.
PSF=$(command -v psf)   || PSF="$PY $SRC/psf.py"
TOPF=$(command -v topf) || TOPF="$PY $SRC/topf.py"

say()  { printf '# %s\n' "$*"; sleep "$(( PAUSE > 0 ? 1 : 0 ))"; }    # explanation line (tight)
run()  { printf '$ %s\n' "$*"; eval "$*"; sleep "$PAUSE"; }           # show command, run for real, pause
sect() { printf '\n'; }                                              # one blank line between sections/steps

# --- overview: assume the viewer last saw this months ago, name alone won't do
sect
say "psf — two single-file Linux process viewers that read /proc, no deps."
say "  topf: a top-like live TUI that hides the boring 500 processes and shows"
say "        only the INTERESTING subtrees (ssh/tmux/bazel/claude) + whatever's"
say "        actually heavy, each annotated with windowed CPU, RSS, ports."
say "  psf:  a one-shot snapshot of those same subtrees, every proc classified"
say "        [usr]/[bld]/[cmp]/[inf]/... with cwd/exe/venv/ports under a load+mem"
say "        header — plus --path, 'which processes are touching this directory?'"
say "Demoing: from source -> $PSF"

# --- proof of life: cheapest run there is — the tool answers --help
# (the usage block alone surfaces the knobs, incl. the new --path mode)
sect
run "$PSF --help 2>&1 | head -4"

# --- headline 1: psf — one real snapshot. It runs once and exits; no flag for it.
sect
say "psf takes one snapshot and exits: a load/mem header, a badge legend, then the"
say "interesting subtrees with a category badge on every line. (-s 0 skips the CPU"
say "sample; --no-cache leaves nothing behind.)"
run "$PSF -s 0 --no-cache --no-color -w 44 2>/dev/null | head -16"

# --- headline 2: psf --path — 'who is touching this directory right now?'
sect
say "--path flips psf around: instead of the tree it scans every proc's cwd, exe,"
say "open fds and mmaps and lists whoever touches a path. We plant a sleeper in a"
say "temp dir, then ask psf who's in there:"
tmp=$(mktemp -d)
# A backgrounded sleeper whose cwd is the temp dir = one guaranteed, clean hit.
# Detach its fds (>/dev/null </dev/null) so it never holds our output pipe open,
# and use ${holder:-} (NOT :-0, which would 'kill 0' the whole process group).
( cd "$tmp" && exec sleep 30 ) >/dev/null 2>&1 </dev/null &  holder=$!
trap 'kill "${holder:-}" 2>/dev/null; rm -rf "$tmp"' EXIT
sleep 0.3
run "$PSF --path $tmp --no-color -w 50 2>/dev/null | head -8"
kill "$holder" 2>/dev/null; wait "$holder" 2>/dev/null || true; holder=

# --- headline 3: topf — one plain frame. -s 0.2 keeps the CPU sample quick.
sect
say "topf is the live TUI sibling. topf --once prints a single plain frame: a"
say "glossary, then the interesting process tree. (-s 0.2 = sample CPU for 0.2s.)"
run "$TOPF --once --no-color -s 0.2 -w 44 2>&1 | head -14"

sect
say "Both are read-only /proc readers — no network, no writes. Source: $SRC"
