# topf — pinned vmstat pane + interactive scroll/expand

Date: 2026-06-07
Status: design (approved; pending spec review)
Builds on: docs/superpowers/specs/2026-06-06-topf-windowed-cpu-design.md

## Motivation

topf surfaces interesting & heavy process subtrees, but the live loop has two
gaps the user hit in practice:

1. **System-wide load is invisible.** topf shows per-process CPU/RSS but nothing
   about overall CPU split, memory pressure, or I/O. A pinned `vmstat`-style
   pane gives that context without leaving topf — and we can fix vmstat's
   long-standing readability problems (raw KB numbers, no color, ragged
   columns) while we own the numbers.
2. **Overflow is a dead end.** When the kept tree is taller than the terminal,
   `clip_frame` drops the tail behind a `… +K more` footer. A batch of 20+
   `clang` workers that merges into a single `×24` group at the bottom is
   exactly the thing you want to *look into*, and today you can't — you can't
   scroll to it, and you can't expand it to see the members.

This design adds, in one combined change: a **pinned bottom vmstat pane**
(reimplemented from `/proc`, no deps, no subprocess) and an **interactive
scroll + cursor + expand/collapse** model for the tree.

## Scope

- **In:** split-pane live layout (pinned header, scrolling tree+lifecycle,
  pinned vmstat); a logical-identity selection cursor; viewport scrolling with
  edge markers; Space/Enter expand-collapse of the group/collapsed-subtree under
  the cursor; the vmstat pane with human units, outlier coloring, fixed columns,
  swap-aware and network columns; new keys and CLI flags; tests for the new pure
  functions.
- **Out:** process actions (signal/kill/renice) — still a later design. Mouse
  support. Horizontal scrolling. Persisting expansion/scroll state across runs.

## Substrate & shape

- Remains one self-contained, **stdlib-only** file (`topf.py`); raw ANSI, no
  curses. New pure helpers are unit-tested the way the existing core is.
- The functional core (`scan → build_tree → select → collapse → probe →
  render`) is preserved. The two structural changes are: (a) `render()` emits
  *row records* instead of bare strings so a presenter can apply cursor/viewport;
  (b) `run_live` holds a small persistent **UI-state** object across frames.

## Layout

The frame is three regions instead of one flat clipped list:

```
topf — 1.0s, 32 cores, 412 procs (380 hidden)  every 1s  [q]uit [f]reeze [w]in [␣]expand   ← pinned top
                                                                                   ▲ 3 more above
  4521 bazel build //...                                                           ┐
      cpu 380% 210% 90%  rss:2.1G  up:5m                                           │
▸ ×24 clang -cc1 ...                          ◀ selected (reverse video), ▸=closed │ scroll region
      cwd:~/src/..  pids:1201 1202 … +16  cpu 1800–2400%  rss:120M–340M            │ (tree+lifecycle)
  9930 ssh -N tunnel ...                                                           ┘
                                                                                   ▼ 41 more below
────────────────────────────────────────────────────────────────────────────────  separator
 vmstat   r  b    free   buff   cache     bi     bo     ni     no     in     cs  us sy id wa   ┐ pinned
         12  0    3.2G   210M    8.1G      0   4.2M   1.1M   880k   9.1k   44k  71  6 21  2     │ bottom
         14  0    3.1G   210M    8.1G      0   1.1M   2.0M   1.3M   8.8k   41k  68  7 24  1     ┘ pane
```

- **Pinned top:** the header line (always; it already carries the key hints).
- **Scroll region:** the process tree, then the lifecycle section, as one
  scrollable block. The glossary, when enabled, is the first thing in this block
  (so it scrolls away). Edge markers `▲ N more above` / `▼ M more below` are
  drawn (dim) on the first/last region row when content extends past it,
  replacing the old `… +K more` truncation footer.
- **Pinned bottom:** the vmstat pane, shown only when the terminal is large
  enough (see below).

### Region sizing

Let `rows`,`cols` = terminal size. Reserve 1 row for the header. The vmstat pane
is shown when `rows >= MIN_ROWS_FOR_VMSTAT` (default 18) and
`cols >= MIN_COLS_FOR_VMSTAT` (default 60); otherwise it is hidden and the tree
gets the whole body. When shown it occupies `1 (separator) + 1 (vmstat header) +
k` rows, where `k` (sample rows) = `clamp(rows_left_for_pane, 3, --vmstat-rows)`
and `--vmstat-rows` defaults to a cap (e.g. 12). The tree scroll region gets
whatever rows remain. `v` toggles the pane at runtime; `--no-vmstat` starts it
hidden.

## Part A — interactive scroll / cursor / expand

### UI state (persists across frames in `run_live`)

A small object (e.g. a dataclass `UIState`) holding:

- `expanded: set` — item ids the user has toggled open.
- `cursor: id | None` — the logical item currently selected (an **id**, not a
  row index, so it survives re-sorting and pid churn).
- `scroll_top: int` — index (into the current frame's selectable rows) of the
  first visible row.
- existing `frozen: bool`, `sort_idx: int`.

### Row records & the presenter

`render()` returns a list of **row records** instead of strings. A row record
carries: the rendered `text` (already colored/tinted as today), the owning
**item id**, and `expandable: bool` (True for a `×N` group or a collapsed
subtree node). Detail/annotation lines (the dim second line, collapse notes)
share their parent row's id and are non-selectable continuation lines.

A new presenter step (in `run_live`) takes the row records + UI state +
region height and produces the final lines:

1. Resolve the cursor: if `cursor` id is absent this frame, snap to the nearest
   surviving selectable row (by previous index); on first frame, the first
   selectable row.
2. Adjust `scroll_top` so the cursor's row is within the viewport (scroll just
   enough; cursor never leaves the visible region).
3. Slice selectable rows (with their continuation lines) to the region height.
4. Apply the cursor highlight (reverse video, SGR `7`) to the selected row, and
   a leading `▸`/`▾` glyph to expandable rows (closed/open).
5. Add edge markers when sliced content extends beyond the region.

The presenter is a pure function `(row_records, ui_state, height) -> (lines,
new_scroll_top, resolved_cursor)` so it is unit-testable without a terminal.

### Identity (stable across re-sort & pid churn)

- **Proc row** → `("p", pid, starttime)`.
- **Group row** → `("g", parent_id, comm, exe)` — the bucket key
  `group_siblings` already forms, qualified by the parent so the same
  `(comm,exe)` under different parents are distinct. Top-level groups use a
  root sentinel as `parent_id`.
- **Collapsed-subtree node** → its Proc id `("p", pid, starttime)` (the node
  that owns the `… +K descendants` note).

### Expansion threads into the data model (not the presenter)

Expansion changes *what rows exist*, so it is applied in the existing core, not
bolted onto rendering:

- `group_siblings(..., expanded)` — a bucket whose group id is in `expanded`
  emits its members **individually** (the `×N` merge is skipped). So expanding
  the `×24 clang` group yields 24 ordinary Proc rows (and their subtrees).
- `collapse(..., expanded)` — a node whose id is in `expanded` is **not**
  collapsed; its suppressed descendants become visible again.

Because both are id-driven, Space works uniformly: toggle the id under the
cursor in `expanded`; the next frame's tree reflects it. Expansion is
**location-specific** — toggling one `×N` group does not expand an identical
group elsewhere.

Newly-revealed rows can themselves be groups/collapsed (e.g. an expanded clang
member with its own heavy subtree), so expansion composes naturally.

### Keys (live mode)

| key | action |
|-----|--------|
| `↑` / `k`, `↓` / `j` | move cursor (scrolls the viewport at the edges) |
| `PgUp` / `PgDn` | page the viewport |
| `g` / `G` | cursor to top / bottom |
| `Space` / `Enter` | expand/collapse the selected group or collapsed subtree |
| `f` | freeze (moved off Space) |
| `w` | cycle sort window (unchanged) |
| `v` | toggle the vmstat pane |
| `q` / `Ctrl-C` | quit |

Arrow keys arrive as escape sequences (`\x1b[A` etc.); the input read in
`run_live` grows a tiny decoder for the cursor/paging keys. Unknown sequences
are ignored. `--once`/piped output is unchanged (no cursor, no pane unless
explicitly requested — see flags).

## Part B — vmstat pane (reimplemented from /proc)

A new self-contained block of **pure functions**, mirroring the windowed-CPU
ring pattern (`update_history`/`windowed_rate`): parse raw `/proc` text →
counters; a ring holds the last N samples; rates are per-second deltas between
consecutive 1 Hz samples; a formatter produces the pane lines.

### Sources & columns

All rate columns are deltas between the two most recent samples ÷ elapsed
seconds. Sampling rides topf's existing redraw cadence — no extra timer, no
subprocess.

| group | cols | source | format |
|-------|------|--------|--------|
| procs | `r b` | `/proc/stat` `procs_running`, `procs_blocked` | int |
| memory | `free buff cache` | `/proc/meminfo` MemFree, Buffers, Cached | human bytes |
| swap† | `si so` | `/proc/vmstat` `pswpin`/`pswpout` (pages→bytes) /s | human bytes/s |
| io (block) | `bi bo` | `/proc/vmstat` `pgpgin`/`pgpgout` (KB→bytes) /s | human bytes/s |
| net | `ni no` | `/proc/net/dev` summed rx/tx bytes (excl. `lo`) /s | human bytes/s |
| system | `in cs` | `/proc/stat` `intr`, `ctxt` /s | human count (`9.1k`) |
| cpu | `us sy id wa` | `/proc/stat` `cpu` aggregate deltas | percent |

† The swap group (`si so`) is shown **only when `SwapTotal > 0`** (read from
`/proc/meminfo`); on a swapless host the columns are omitted entirely.

**CPU columns are exactly four:** `us sy id wa`. The "rest" (nice, irq, softirq,
steal, guest = `100 − us − sy − id − wa`) is dropped, not shown — it is usually
0 and not of interest.

### The three readability fixes (the point of reimplementing)

1. **Human units.** Memory and io/net byte columns go through the existing
   `fmt_bytes` (`3.2G`, `4.2M`), not raw KB. `in`/`cs` use a compact count
   format (`9.1k`, `44k`). CPU stays integer percent.
2. **Outlier coloring.** Each cell is tinted by how much the latest value
   deviates from its **own rolling window** — robustly, via median + MAD:
   `level = number of MAD-thresholds the deviation clears`, mapped 0–3 onto the
   existing `TINT_SGR` ramp (dim → warm → color → bold-red). So a `wa` or `cs`
   spike pops without any hardcoded per-column thresholds, and a steadily-busy
   column stays calm. Columns with ~zero spread (e.g. idle `si/so`) never tint.
   A small new pure helper `outlier_level(value, window_values) -> 0..3`.
3. **Proper widths.** Columns are right-aligned and sized to the max formatted
   width across the visible sample rows (min the header width), with single
   spaces between groups widened to a clear gap. A `vmstat ` gutter labels the
   pane on the left of the header row.

### Pane assembly

`format_vmstat_pane(sample_ring, swap_on, width, height, color) -> [lines]`:
build the visible column set (swap in/out), format the last `height-1` samples
into a right-aligned grid under a header row, color each cell by
`outlier_level` against the full ring. Pure; testable with a synthetic ring.

## Config / flags

New CLI flags (all with sensible defaults so bare `topf` "just works"):

- `--no-vmstat` — start with the pane hidden (still toggleable with `v`).
- `--vmstat-rows N` — cap the pane's sample rows (default ~12).
- `--vmstat` — in `--once`/piped mode, include a single current vmstat row in
  the plain frame (off by default there, since one-shot has no ring/trend).

Existing flags unchanged. `space` is no longer documented as freeze in the
header/help — it is `f` now, and `[␣]expand` is shown.

## Error handling & edge cases

- Missing `/proc` fields (older kernels, no `pgpgin`, container without
  `procs_blocked`): the parser yields `None` for that cell, formatted as `—`,
  never crashes.
- `/proc/net/dev` absent or unreadable → `ni/no` show `—`.
- First live frame has < 2 vmstat samples → all rate columns `—`; the pane still
  draws its header (so layout is stable from frame 1).
- Tiny terminals (below the vmstat thresholds) → pane hidden, tree gets the body;
  the cursor/scroll still work.
- Cursor's item disappears (process died, group dissolved below `dedup_min`,
  parent collapsed) → snap to nearest surviving row; never leave a dangling id.
- Freeze (`f`) stops sampling as today; the cursor/scroll/expand keys still work
  on the frozen frame (navigation is a view operation, not a data operation).

## Testing

New pure functions get unit tests alongside the existing ones:

- `parse_proc_stat_counters`, `parse_meminfo`, `parse_vmstat_counters`,
  `parse_net_dev` — fixture text → counter dicts (incl. missing-field paths).
- vmstat rate computation over a synthetic ring (delta ÷ elapsed; `—` when < 2
  samples; swap columns present iff `SwapTotal>0`).
- `outlier_level` — flat window → 0; a clear spike → high level; zero-spread
  window never tints.
- `format_vmstat_pane` — column count tracks swap on/off; right-alignment;
  network columns present.
- The presenter `(row_records, ui_state, height) -> (lines, scroll_top,
  cursor)` — cursor stays visible while scrolling; edge markers appear only when
  content overflows; cursor snaps to nearest on id loss; highlight applied to the
  right row.
- `group_siblings`/`collapse` honoring `expanded` — an expanded group id yields
  individual members; an expanded collapsed-node id reveals descendants;
  location-specificity (identical group elsewhere stays merged).

## Out of scope (explicit)

- Process actions (kill/signal/renice) — future design.
- Mouse, horizontal scroll, persisted UI state.
- Configurable vmstat column sets / reordering — the column set is fixed here.
