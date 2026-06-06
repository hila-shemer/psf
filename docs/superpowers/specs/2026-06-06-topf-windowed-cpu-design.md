# topf — windowed-CPU live process viewer (psf → topf)

Date: 2026-06-06
Status: design approved, pending spec review

## Motivation

`psf` prints a one-shot focused tree of *interesting* subtrees (matched by
comm/cmdline: bazel, sshd, tmux, claude). Two gaps drove this redesign:

1. **Resource hogs are invisible.** A `qemu` pegged at 400% CPU never appears
   unless it happens to be a descendant of a matched process. We want to also
   surface processes that are *interesting because they are heavy* — a lot of
   CPU or memory — even a busy `kworker`.
2. **One snapshot can't measure load.** Live CPU% is the diff of two samples;
   one-shot `psf` can only ever take the one pair, and a lifetime average is
   "CPU ÷ uptime" — for a long-lived process that just spiked, that reads ~0%
   and is useless. Meaningful load needs *many* samples over time.

So `psf` becomes `topf`: a `top`-like, full-screen, continuously-sampling
viewer that keeps psf's value-add — a focused, deduped, collapsed **tree** —
instead of `top`'s flat list.

## Scope

- **In (v1):** full-screen live viewer; per-process windowed CPU; resource
  promotion reusing the tint anchors; RSS-only-promotion gate; heavy kernel
  threads; top-like ordering; header/clipping; read-only keys (quit, freeze,
  cycle sort window); piped/`--once` single-frame fallback.
- **Out (v2, separate design):** interactive row cursor (up/down navigation)
  and process actions (signal/kill/renice). Stable selection over a
  re-sorting / deduping / collapsing tree is its own sub-problem.

## Substrate & shape

- `git mv psf.py topf.py`; remains one self-contained, **stdlib-only** file.
- Full-screen rendering uses **raw ANSI**, not curses: alternate screen
  (`\x1b[?1049h` / `…l` on exit), cursor-home + clear-to-end-of-line per row
  each frame. This reuses the existing SGR/tint machinery and keeps `render()`
  a pure list-of-lines function.
- Keypresses: `termios` raw (cbreak) mode on stdin + `select()` with the
  refresh interval as timeout. Terminal state restored on exit (try/finally,
  including on signal). `q` or Ctrl-C quits.
- **Fallback:** when stdout is not a TTY (piped) or `--once` is given, take a
  single two-sample frame and print it plainly (no alt screen) — preserves
  pipeability and keeps the existing one-shot behaviour available.

## The sampling loop

State carried across frames:
- `prev`: the previous full `{pid: Proc}` snapshot (for the per-frame CPU diff).
- `history`: per-`(pid, starttime)` ring of `(timestamp, cpu_ticks)` samples
  spanning the longest window (for windowed rates).
- `promoted_since` / cooling state derived from the windows (see below).

Each frame:
1. `scan()` → `cur`; record `t = monotonic()`.
2. For every pid in `cur`, compute `cpu_current` by diffing `cur` vs `prev`
   over the **actual** elapsed wall time. First frame has no `prev` → primes
   only (no current CPU yet).
3. Append `(t, utime+stime)` to each proc's `history` ring; evict samples older
   than the longest window; drop rings for pids no longer present.
4. `build_tree` → `select` (matchers + resource promotion) → `collapse` →
   `collect_printed` → `probe` (cwd/exe/sockets, cached as today).
5. Render lines, clip to terminal, draw.
6. `prev = cur`. Wait up to one interval for a keypress; handle it; repeat.

Sample cadence == redraw cadence == `-s/--sample-interval` (default ~1s). All
windows derive from `history`, so a small interval (e.g. `-s 0.2`) yields a
~200 ms shortest window; a larger interval yields coarser windows.

## Windowed CPU

- Windows default to **2s / 10s / 60s** (`--windows 2,10,60` to override).
- Rate over window *W* = `Δticks / (clk_tck · actual_elapsed)`, where `Δticks`
  is `cpu_ticks_now − cpu_ticks_at_sample_nearest(now − W)` and
  `actual_elapsed` is the real time between those two samples (not the nominal
  *W*, since frames can be late). Result is in cores (1.0 == one full core),
  consistent with the existing `cpu_fraction`.
- A window with no old-enough sample yet (process younger than *W*, or warming
  up) reports over whatever span exists, or `None` if < 2 samples.
- **Lifetime-average is removed from the headline** (it is ÷ uptime). It may be
  retained only as an explicitly-labelled trailing bit if useful; not required.
- Headline format, e.g.: `cpu 398% 210% 55%` (2s 10s 60s). The 2s figure is
  the live one; tint level = `max` across windows against `CPU_TINT_ANCHORS`.

## Resource promotion

`select()` keeps the existing always-interesting matchers (bazel/sshd/tmux/
claude) and adds **resource promotion**:

- A process is promoted when it clears tint-anchor **level ≥ N** (default 2,
  `--promote-level`) on either:
  - any CPU window (`CPU_TINT_ANCHORS = 0.10, 1.0, 4.0` cores) — level 2 ≈
    ≥ 1.0 core; or
  - RSS (`RSS_TINT_ANCHORS = 100M, 1G, 5G`) — level 2 ≈ ≥ 1G.
- **RSS-only gate:** a process promoted *solely* by RSS must also clear a low
  60s-CPU floor (anchor level ≥ 1 ≈ 10%) — it has to be doing something, not
  just holding a large heap (idle JVM/browser/DB). CPU-promoted processes
  promote regardless of RSS. Controlled by `--rss-needs-cpu` (default **on**);
  `--no-rss-needs-cpu` disables the gate. Tunable once observed under load.
- Promoted processes keep ancestors-to-root + descendants, exactly like matched
  ones, so `qemu` shows with its context.
- **Kernel threads** (under `kthreadd`, pid 2), currently always excluded, are
  **promotable when heavy** (clear the CPU anchor) — your `kworker` case. They
  have no cmdline/cwd/rss, so they render as `[kworker/3:1]` with cpu/up only.
  They are still **never** shown merely for matching.

### Persistence / decay (subsumed by the long window)

No separate decay timer. The 60s window *is* the persistence mechanism: a brief
spike promotes via the 2s window, then stays promoted via the 10s/60s windows
until it ages out. **"Cooling"** = the short window has dropped below threshold
but a longer window is still elevated; such rows render **dimmed** to signal the
spike is over but recent. (This satisfies the earlier "sticky with decay"
requirement without an ad-hoc timer.)

## Ordering (top-like)

- Visible **top-level** subtrees are sorted by aggregate **2s-window** CPU,
  descending, with a stable tiebreak by pid — hot stuff floats to the top each
  frame.
- **Children keep pid order** (reordering every child every frame is too jittery
  to read).
- Matched subtrees (bazel etc.) sort by their own load alongside promoted ones —
  there is no separate "resource" section.

## Groups & lifecycle

- `×N` deduped group detail shows windowed CPU as **ranges** across members; the
  **sum** drives the group's tint. Reuses the existing group machinery.
- The born/died **lifecycle** section is kept. It is now naturally per-frame over
  the interval, which is more useful live than it was one-shot.

## Header & clipping

- Top-style status line:
  `topf — {frame}s, {cores} cores, {N} procs ({hidden} hidden)   every {interval}s   [q]uit  [space]freeze  [w]indow`
  (`{frame}` = actual elapsed of the frame just measured).
- Output is clipped to the terminal's rows and columns via an **ANSI-aware
  truncate** that counts *visible* characters and never splits an SGR escape.
  Overflow rows collapse into a `… +K more` footer.
- Frozen state (`space`) shows a `FROZEN` marker in the header and stops
  re-sampling/redrawing until unfrozen.

## CLI

`topf.py` (argparse `prog="topf"`). New/changed flags:

- `-s/--sample-interval` — now the **refresh cadence** (default ~1s).
- `--once` — single plain frame then exit (auto-selected when stdout is piped).
- `--windows 2,10,60` — CPU window seconds (comma list; longest sets ring span).
- `--promote-level N` — tint-anchor level required to promote (default 2).
- `--rss-needs-cpu` / `--no-rss-needs-cpu` — RSS-only-promotion gate (default on).

Carried over unchanged: `-w/--width`, `-t/--threshold`, `--no-cache`,
`--no-color`, `--no-glossary`, `--no-dedup`, `--dedup-min`, `--no-lifecycle`.

Keys (read-only v1): `q`/Ctrl-C quit, `space` freeze/unfreeze, `w` cycle the
sort window (2s → 10s → 60s → back).

## Testing

The repo has no tests today. Add a focused test file covering the new **pure**
logic (the TUI loop stays a thin shell over `render()`):

- windowed-rate computation from a synthetic sample ring (incl. late frames,
  too-young process, < 2 samples → None);
- history append / eviction by longest window / dead-pid drop;
- promotion-by-anchor-level for CPU and RSS, incl. the RSS-only 60s-CPU gate
  (on and off) and the kernel-thread override;
- ordering by 2s window with pid tiebreak;
- ANSI-aware truncate (visible-width count, never splits an escape).

## Backward compatibility

- `--once` / piped output reproduces the substance of today's psf single frame
  (now with windowed rather than lifetime CPU, and with resource-promoted procs
  included). The cache, socket summaries, dedup, collapse, and lifecycle all
  carry over.
