# psf — focused process-snapshot tool

**Date:** 2026-06-06
**Status:** Approved design, pre-implementation

## Problem

`ps wauxf` dumps every process; the interesting ones (bazel builds, ssh
sessions, tmux/claude) are buried under hundreds of background daemons and
kernel threads. Today the user hand-greps `/proc` to find PIDs of interest and
inspect their `cwd` / `exe` / open sockets — slow and repetitive. A full
`sudo lsof -n` to get sockets is too slow to run routinely.

`psf` prints a compact tree of only the interesting process subtrees, each
annotated with the start of its command line, a summarized `cwd`, the executing
binary, and any open ports/sockets — fast enough to run constantly, by deep-
probing only the processes it will actually print and caching the expensive
work across runs.

## Goals

- Show only interesting subtrees; hide the ~95% of daemons/kthreads.
- Per printed process: first ~50 chars of cmdline, summarized `cwd`, `exe`
  path, listening ports / open sockets, thread count when >1.
- No `ps`, no `lsof`, no `ss`, no subprocess forks — read `/proc` directly.
- Collapse large subtrees (big bazel builds) into a count instead of probing
  every node.
- Cache the expensive per-process analysis across invocations, re-probing only
  processes whose fd table actually changed since the last run.
- Run fully under `sudo`/root (to read other users' processes); degrade
  gracefully (show `?`) when run unprivileged.

## Non-goals

- Live/continuous TUI (one-shot snapshot only).
- Per-thread display (ps -L style). Thread *count* only.
- Cross-host / remote process inspection.
- A separate "drill into one PID" subtool (YAGNI for now).

## Language & dependencies

Single executable Python 3 script, standard library only. Config block at the
top of the file, in the spirit of `backup.sh`.

## Architecture

Pipeline of small, independently testable functions operating on plain dicts:

```
scan() -> build_tree() -> select() -> collapse() -> probe() -> render()
```

### 1. `scan()` — cheap full pass

`os.listdir('/proc')`; for each numeric PID read only cheap files:

- `/proc/PID/stat` → ppid, state, `num_threads`, `starttime` (field 22),
  `comm`.
- `/proc/PID/cmdline` → full argv (NULs → spaces).
- uid from `/proc/PID/status` (`Uid:` line).

No `cwd`/`exe`/`fd` reads here — those expensive readlinks are deferred to
`probe()`. Output: `{pid: ProcInfo}`.

### 2. `build_tree()`

Build PPID → children mapping. Roots: PID 1, and PID 2 (`kthreadd`) for kernel
threads.

### 3. `select()` — the smart filter

Config `INTERESTING` is a list of matchers (regex against `comm` and/or full
`cmdline`). Default matchers:

- **bazel** — `comm` matches `^bazel` OR cmdline contains `bazel(` (the Java
  server shows `comm=java`, `cmdline=bazel(<workspace>)`).
- **sshd** — session leaders (`sshd: user@pts/...`) and everything beneath.
- **tmux** — `tmux` server.
- **claude** — `comm`/cmdline matches `claude`.

A process is **kept** if it *is* an interesting root, is a **descendant** of
one, or is an **ancestor** of one (ancestors keep the tree rooted, e.g.
`sshd → bash → claude`). All other processes are dropped. Kernel threads
(descendants of PID 2) are always dropped unless explicitly matched.

Optional footer summarizing what was hidden:
`(hidden: 312 procs, 89 kernel threads)`.

### 4. `collapse()` — bound big subtrees

For each kept node with more than `COLLAPSE_THRESHOLD` (default 20)
descendants: mark collapsed, keep a few representative children (distinct
`comm`s, plus any interesting ones), and emit
`… (+N more descendants)` in place of the rest. Collapsed-away leaves are
**not** deep-probed — the fast path for big builds.

### 5. `probe()` — deep info for printed nodes only

For each node that will actually be printed:

- `readlink /proc/PID/cwd` — always re-read (one syscall; avoids stale cwd on
  shells that `cd` around). Summarized: `$HOME` → `~`, long paths shortened.
- `readlink /proc/PID/exe` — always re-read (one syscall).
- **Sockets** — cached (see §6). On a miss/stale: walk `/proc/PID/fd` for
  `socket:[inode]` entries, resolve inodes against `/proc/net/{tcp,tcp6,udp,
  udp6,unix}`. `/proc/net` is read **once per network namespace** — group PIDs
  by `readlink /proc/PID/ns/net`, read one representative's `/proc/PID/net/...`
  per namespace — so bazel sandboxes / containers resolve correctly.

Socket presentation: listening TCP/UDP ports shown prominently
(`:8080 LISTEN`); established connections as a count (`+3 est`); named unix
socket paths where present.

### 6. Cross-run cache

**Location:** `${XDG_CACHE_HOME:-~/.cache}/psf/cache.json`. Under `sudo`,
resolve to the invoking user's cache dir via `$SUDO_USER` when set.

**Top-level validity:** cache file stores `boot_id`
(`/proc/sys/kernel/random/boot_id`); a reboot invalidates the whole file.

**Per-process identity key:** `(pid, starttime)`. `starttime` is already read in
`scan()` and is immune to PID reuse — a recycled PID has a different starttime,
so a stale entry can never be mistaken for the new process.

**Cached payload:** the resolved socket summaries only (the expensive part).
`cwd`/`exe` are not cached (re-read every run — they're free).

**Freshness signal:** the originally-planned `/proc/PID/fd` directory mtime
trick was validated against this kernel (6.17.0-1017-aws) and **rejected** —
neither `st_mtime_ns` nor `st_ctime_ns` of the fd directory changes when fds
open/close. The only cheap signal that reliably moves is the **fd count**
(`len(os.listdir('/proc/PID/fd'))`) — one readdir, zero readlinks, far cheaper
than the full fd-walk + `/proc/net` parse it gates.

A cached socket entry is reused iff **both**:
- the fd count matches the cached fingerprint, **and**
- the entry's age is under `CACHE_TTL` (default 30s).

Otherwise the process is re-probed (fd-walk + targeted `/proc/net` parse for
its netns). So:

- stable daemon (bazel server, sshd, tmux) → fd count unchanged, within TTL →
  reuse cached sockets; no fd-walk and no `/proc/net` parse for its namespace.
- fds opened/closed → count changes → re-probe.
- a same-count fd swap (close one socket, open another) is missed by the count
  but corrected within `CACHE_TTL`.
- short-lived actions (clang) → never stable long enough to benefit; they age
  out automatically.

**Self-cleaning:** on write, drop entries whose `(pid, starttime)` is absent
from the current scan, so the file only ever holds live processes.

### 7. `render()`

`pstree`/`ps auxf`-style tree with `├─ └─` connectors. Per node:

- Primary line: connector + PID + first `CMD_WIDTH` (default 50) chars of
  cmdline.
- Dim indented detail line (only fields present): `cwd:` , `exe:`,
  sockets, `[N threads]` when >1.
- Collapsed marker line: `… (+N more descendants)`.

Color/dim only when stdout is a TTY.

## Config block (top of file)

- `CMD_WIDTH = 50`
- `COLLAPSE_THRESHOLD = 20`
- `CACHE_TTL = 30` (max age before a cached socket entry is re-probed)
- `INTERESTING = [...]` — list of matchers (label, regex, target=comm|cmdline)
- HOME-summarization on/off

## Performance characteristics

- One `/proc` pass, zero forks.
- Deep readlinks bounded to the small set of printed nodes.
- `/proc/net` parsed at most once per network namespace, and skipped entirely
  for processes whose fd count is unchanged and cache entry is within TTL.
- Effectively instant even with a large build running.
- `sudo` needed only to read other users' `fd`/`exe`/`cwd` links.

## Testing strategy

- Unit-test each stage on synthetic `ProcInfo` dicts (tree building,
  selection, collapse, cwd/exe summarization, socket-line formatting).
- Parse fixtures for `/proc/net/{tcp,tcp6,unix}` → socket summary.
- Cache: identity-key reuse, boot_id invalidation, fd-count hit/miss,
  TTL expiry, self-cleaning of dead entries.
```
