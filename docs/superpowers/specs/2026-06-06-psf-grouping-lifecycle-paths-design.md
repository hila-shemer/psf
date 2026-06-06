# psf — sibling dedup, lifecycle section, path compression

**Date:** 2026-06-06
**Status:** Approved design, pre-implementation
**Builds on:** `2026-06-06-psf-design.md` (the base tool) and the RSS / elapsed /
CPU-rate addition already merged (`feat(psf): per-process RSS, elapsed time, and
CPU rate`).

## Problem

Three rough edges on the current output:

1. **All-or-nothing grouping.** A subtree is either fully expanded or, past
   `COLLAPSE_THRESHOLD`, fully collapsed into a `… (+N descendants)` note.
   Near-identical *sibling* subtrees (e.g. six `sshd: shemer [priv] → sshd@pts/N
   → tmux attach-session` chains under one `sshd -D`) each print in full — ~6
   lines apiece — even though they differ only in pts number, pid, and session
   name. The repetition drowns the signal.

2. **The 0.2s sample window is invisible.** psf now sleeps ~0.2s to measure
   current CPU, but says nothing about processes that were *born or died* during
   that window — exactly the short-lived churn (compiler invocations under a
   bazel build, transient shells) that is otherwise impossible to catch in a
   one-shot snapshot.

3. **Over-long detail lines.** `cwd:` and `exe:` print in full, so a line like
   `cwd:~/mss/.claude/worktrees/multimlamp (deleted)  exe:~/.local/share/claude/
   versions/2.1.165  cpu 0% (0.1% avg)  rss:271.9M  up:10h48m  20 threads`
   wraps and is hard to scan.

## Goals

- Merge near-identical sibling subtrees into one `×N` entry that preserves the
  distinguishing information (pids, cwd/cmdline variants, stat ranges).
- Add a separate, system-wide section listing processes born/died during the
  measured sample window.
- Compress `cwd`/`exe` to a head/`..`/basename form, and compress an over-long
  basename the same way.

## Non-goals

- Tree-wide (cross-parent) merging — sibling-only, structure preserved.
- A live/continuous TUI — still a one-shot snapshot.
- Detecting processes that are born *and* die entirely within the window (in
  neither snapshot) — out of reach for a two-point sample; documented as a known
  limitation.

## Config additions (top of file)

- `DEDUP_MIN = 3` — minimum identical siblings to form a group (`--dedup-min`).
- `NEVER_MERGE = {"claude"}` — comms never grouped, even when identical.
- `LIFECYCLE_MAX = 40` — max born (and max died) entries listed before `+M more`.
- `PATH_HEAD = 2` — leading path components kept when compressing.
- `PATH_MAX_BASENAME = 30` — basename length above which it is itself compressed.
- `PATH_BASENAME_KEEP = 6` — chars kept each side of `...` in a compressed basename.

CLI flags: `--no-dedup`, `--dedup-min N`, `--no-lifecycle`. `-s 0` (existing)
already disables the sleep and therefore both current-CPU and the lifecycle
section.

---

## Part A — sibling dedup

### Model

A **render-time** grouping, orthogonal to `select`/`collapse`. The selection and
collapse passes are unchanged; grouping happens while rendering a node's visible
children.

**Group key:** `(comm, exe)`. Deliberately loose — args, cwd, and pts numbers
may differ. A comm in `NEVER_MERGE` is never grouped (each such proc renders
individually regardless of identical siblings).

**Grouping is recursive over the union of children.** At a node, its visible
(kept, non-suppressed) children are partitioned by key. A partition with
`>= DEDUP_MIN` members renders as one `×N` group header; the group's children are
the *concatenation of all members' visible children*, which are then grouped by
the same rule one level down. Partitions with `< DEDUP_MIN` members render as
individual procs as today.

Because exe is needed for the key, the dedup transform runs **after** `probe()`
(which fills `exe`). Members within a group may have heterogeneous subtrees;
pooling their children and re-grouping naturally yields sub-groups (e.g. 5 with a
`tmux` child + 1 with a `bash` child → child groups `×5 tmux` and a lone
`bash`).

### Pure function

```
group_siblings(procs, dedup_min, never_merge) -> list[RenderItem]
```
`RenderItem` is either a single `Proc` or a `Group(key, members: list[Proc])`.
Order: groups and singletons sorted by the smallest member pid, to keep output
stable. Procs whose comm is in `never_merge`, and any partition smaller than
`dedup_min`, come back as singletons.

`render()` consumes these: a singleton renders exactly as today; a `Group`
renders one header + one detail line, then recurses with
`group_siblings(union_of_members_children, …)`.

### Group header + detail

```
└─ ×6  sshd: shemer [priv]
      pids: 364810 364812 364814 364816 364832 1050962   rss:10.1–10.3M  up:16h27m–20h33m
   └─ ×6  sshd: shemer@pts/{0,7,8,9,10}
         rss:7.1–8.9M  cpu 0% (≤0.021% avg)  up:~20h
      └─ ×6  tmux attach-session -t {monitoring,general,task-1,task-2,task-3,...}
            cwd:~  exe:~/../tmux  rss:4.7–4.8M  up:~20h
```

- **Header label** = `×N ` + a merged cmdline: the common token prefix of the
  members' cmdlines, with the differing tails brace-summarized
  (`{a,b,c,...}`, truncated). When tails don't share a clean prefix, fall back to
  the representative (lowest-pid) cmdline truncated to width.
- **Detail line** aggregates, omitting any field that is empty for all members:
  - `pids:` the member pids (capped, `+M` suffix when long).
  - `rss:` min–max via `fmt_bytes` (single value when all equal).
  - `cpu:` current and avg as ranges; collapses to `~0%`/`≤X% avg` when tiny.
  - `up:` min–max via `fmt_duration` (single value / `~` when close).
  - `cwd:`/`exe:` shown when shared across members; when they vary, a brace
    summary (`cwd:{~/backup,~/mss,...}`); each compressed per Part C.
  - thread count when any member has `>1`.

### Helper functions (pure, tested)

- `brace_summary(values, max_items)` → `"{a,b,c,...}"` or common-prefix + brace.
- `range_str(lo_str, hi_str)` → `"lo–hi"` or single when equal.

---

## Part B — system-wide births/deaths

### Two-scan restructure

`main()` brackets the sample sleep with two full scans, so current-CPU and the
lifecycle diff share one measured window:

```
S_A = scan()                      # t_A  (monotonic) → build_tree, select, collapse
printed = collect_printed(...)    #       choose nodes to deep-probe
sleep(interval)                   #       the window
S_B = scan()                      # t_B
dt = t_B - t_A                    #       reported in the section header
# current CPU for printed nodes, over the SAME window:
for n in printed: n.cpu_current = cpu_fraction(cpu(S_B[n]) - cpu(S_A[n]), dt, CLK_TCK)
born, died = diff_snapshots(S_A, S_B)
probe(printed)                    # cwd/exe/sockets, after the window
```

This **replaces** the standalone `sample_current_cpu` printed-only sampler: the
first CPU sample now comes from `S_A`, the second from `S_B`, so the current-CPU
window and the lifecycle window are identical and honestly measured (`dt`, not
the nominal interval — it includes the few ms of build time between the scans).
The rendered tree is "as of `t_A`"; the lifecycle section reports what changed in
the next `dt`.

When `interval <= 0`: no sleep, no `S_B`, no current-CPU, no lifecycle section.

### Diff

```
diff_snapshots(S_A, S_B) -> (born, died)
```
Keyed by `(pid, starttime)` (PID-reuse safe, consistent with the cache). `born`
= keys in `S_B` not in `S_A`; `died` = keys in `S_A` not in `S_B`. Each returns
the corresponding `Proc` list (born from `S_B`, died from `S_A`, so we have
comm/cmdline/ppid for each).

### Display

Grouped by comm to stay compact under a busy build; ppid annotated with the
parent's comm, and flagged when the parent is an interesting/kept proc:

```
lifecycle — system-wide, 0.21s window:
  born:  ×23 cc1plus (← 998877 bazel build //...)   ×4 ld   +1 sh (← 365095 tmux)
  died:  ×21 cc1plus   ×3 ld   -1 git (lived 0.1s)
```

- Group by comm; `×N comm` for groups, individual `+pid cmdline` / `-pid cmdline`
  for singletons. `(← ppid pcomm)` shows the (most common) parent.
- `died` singletons show `lived <fmt_duration(age at t_A)>` from
  `lifetime_secs(starttime, uptime_A, clk_tck)`.
- Each list capped at `LIFECYCLE_MAX`; overflow shown as `+M more` (never
  silently dropped).

### Pure function

`format_lifecycle(born, died, parents, dt) -> list[str]` where `parents` maps
pid→comm (built from `S_A`/`S_B`). Tested on synthetic `Proc` lists.

### Known limitation (documented)

A process born *and* reaped entirely between `t_A` and `t_B` appears in neither
snapshot and cannot be reported.

---

## Part C — path compression (cwd / exe)

`_summarize_path` is extended into `compress_path`, applied wherever `cwd`/`exe`
render (plain detail lines and dedup group lines).

**Rule** (after `$HOME → ~`):

1. Detect and strip a trailing ` (deleted)` marker; re-append at the end.
2. Split on `/`. If there are more than `PATH_HEAD + 1` components, keep the
   first `PATH_HEAD` (=2) as head, the basename as tail, and replace the middle
   with a single `..`. Fewer components → unchanged. (Middle marker is `..`,
   so the result reads like a path: `~/mss/../multimlamp`.)
3. If the basename length > `PATH_MAX_BASENAME` (=30), compress it to
   `prefix...suffix` keeping `PATH_BASENAME_KEEP` (=6) chars each side around a
   `...` ellipsis (distinct from the `..` path-middle marker).

Examples:

| input | output |
|-------|--------|
| `~/mss/.claude/worktrees/multimlamp (deleted)` | `~/mss/../multimlamp (deleted)` |
| `~/.local/share/claude/versions/2.1.165` | `~/.local/../2.1.165` |
| `~/mss/foo` | `~/mss/foo` (no middle) |
| `/usr/lib/a/b/c` | `/usr/../c` |
| basename `verylongnamethatgoesonandonandon_final` (38 ch) | `verylo..._final` |

---

## Rendering order (final output)

1. Glossary (unless `--no-glossary`) — extend it with one line for the `×N`
   group form and the lifecycle section.
2. The grouped tree.
3. The lifecycle section (unless `--no-lifecycle` / `-s 0`).
4. The `(hidden: …)` footer on stderr (unchanged).

## Testing strategy

Pure functions, TDD (write failing test → implement):

- `compress_path`: home-summary, middle elision, no-middle passthrough, absolute
  paths, ` (deleted)` preservation, long-basename compression.
- `brace_summary`, `range_str`.
- `group_siblings`: forms groups at `>= DEDUP_MIN`, leaves pairs when min=3,
  honors `NEVER_MERGE`, recurses over the union of members' children, yields
  heterogeneous sub-groups, sorts stably.
- `diff_snapshots`: born/died by `(pid, starttime)`, PID-reuse case.
- `format_lifecycle`: comm grouping, parent annotation, `lived` for died,
  `LIFECYCLE_MAX` cap + `+M more`.
- Render integration: a synthetic tree renders `×N` headers, aggregated detail,
  and compressed paths.

The two-scan `main()` restructure is covered by the existing live smoke run
(plus a manual check that the lifecycle section appears under a real bazel
build).

## Performance

- One extra full `scan()` (`S_B`) per run — a few ms for a few hundred procs;
  negligible beside the existing 0.2s sleep.
- Dedup and path compression are O(visible nodes) string work at render time.
