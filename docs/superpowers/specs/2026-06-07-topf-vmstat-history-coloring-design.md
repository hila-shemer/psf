# topf — history-grounded vmstat coloring

Date: 2026-06-07
Status: design (approved; pending spec review)
Builds on: docs/superpowers/specs/2026-06-07-topf-vmstat-and-interactivity-design.md

## Motivation

The vmstat pane tints each cell by how much it deviates from the column's
*currently visible* values (`outlier_level`: a robust MAD z-score over the
shown rows, `topf.py:1103`). Two problems follow from "currently visible" being
the only frame of reference:

1. **Eager on an idle machine.** When a column is near-idle, the visible values
   are all tiny, so the MAD (spread) is tiny too. A small absolute bump then
   divides by a near-zero sigma and explodes into a huge z-score — instant red.
   Almost-idle noise reads as alarming.
2. **The display reshuffles ("disco").** Because a cell's color depends on the
   other rows on screen, scrolling — or simply the newest row pushing the oldest
   out — recolors rows that haven't changed. Colors are not a stable property of
   a row.

There is also a semantic wart: `outlier_level` is two-tailed and unanchored, so
a steady `id` (idle %) of ~100 on an idle box reads as a red outlier — "100% idle
is alarming," which is silly.

This design replaces window-relative coloring with **coloring grounded in
long-term, per-machine history**: a value is red because it is high *for this
machine over time*, not because it differs from the dozen rows that happen to be
on screen. The frame of reference no longer depends on the viewport, which fixes
the eagerness and the disco in one move.

## Scope

- **In:** a per-column, exponentially-decaying, log-scale histogram as the
  coloring baseline; persistence of that baseline to a file across runs;
  high-tail percentile coloring with a per-kind absolute *noise floor* and a
  small absolute *ceiling* table for the few machine-independent columns;
  per-row frozen levels (computed once at sample time) so scrolling never
  recolors; new CLI flags for the half-life and the history file; tests for the
  new pure functions.
- **Out:** two-tailed / directional (red-vs-green) coloring — explicitly
  dropped; the existing `dim → yellow → red` ramp stays. Per-process (tree)
  coloring — unchanged. Cross-host history sharing. A UI to inspect/reset the
  histogram (delete the file by hand).

## Substrate & shape

- Remains one self-contained, **stdlib-only** file (`topf.py`); raw ANSI.
- New pure helpers (histogram update, percentile/level, file (de)serialize) are
  unit-tested the way the existing core is. The only impure additions are the
  history-file read at startup and the periodic atomic write.
- `outlier_level` and `VMSTAT_OUTLIER_ANCHORS` are **removed** wholesale; nothing
  else in the pane layout changes.

## Coloring model

### One baseline per column: a decaying log-scale histogram

For each of the 17 vmstat column keys we keep one histogram of that column's
observed rate-row values. The histogram is the *only* frame of reference for
coloring; it does not depend on what is on screen.

**Buckets.** Per column *kind* (`int`, `bytes`, `bps`, `count`, `pct`) a fixed
log-scale layout of `NBUCKETS` (≈ 40) buckets:

- Bucket 0 is reserved for "zero / below the kind's `floor`" (see noise floor).
- Buckets `1..NBUCKETS-1` are log-spaced from the kind's `lo` to `hi`:
  `idx = 1 + floor((log(v) - log(lo)) / (log(hi) - log(lo)) * (NBUCKETS-1))`,
  clamped to `[1, NBUCKETS-1]`.
- `lo`/`hi` per kind are generous, fixed brackets chosen so the realistic range
  for that kind spans the buckets (e.g. `pct`: 1 … 100; `bps`/`count`: 1 …
  ~1e10; `bytes`: 1 MiB … 1 TiB; `int`: 1 … 4096). Out-of-range values clamp to
  the end buckets — exact tail resolution past `hi` does not matter because the
  ceiling and the p99.9 level already saturate there.

Bucket counts are **floats** (decay and normalization produce fractional mass).

**Decay (per sample, gradual).** When a new rate-row value `v` arrives for a
column, before folding it in we *first* read its level against the current
histogram (so a value is judged against history, then becomes history). Then:

```
b = bucket(v)
hist = [c * d for c in hist]     # exponential decay of all mass
hist[b] += (1 - d)              # the new sample's unit of mass
```

`d = 0.5 ** (1 / H)` where `H` is the **half-life in samples** — the number of
samples after which a sample's contribution halves. Default `H` ≈ 200 samples
(≈ 3 min at the 1 s default interval). This is exactly the "0.7 old / 0.3 new
per 100 samples" recency weighting discussed, applied smoothly per-sample
instead of in a once-per-100 jolt.

Two scalars are tracked per column and are independent:

- **`hist`** — the decaying bucket mass, which tends to total ≈ 1 in steady
  state. We never assume exactly 1; `cdf` always divides by the live `sum(hist)`
  ("normalize on read"). This is the *shape* of the distribution.
- **`count`** — a monotonic cumulative count of samples folded in (not decayed),
  used **only** by the warmup gate. It is *not* used for normalization. Because
  it is persisted, a loaded file is already past warmup — fresh runs ground
  immediately, only a brand-new machine starts cold.

Because decay is per-sample but small, the baseline drifts smoothly. Crucially,
a row's level is **frozen at creation** (below), so baseline drift only affects
*new* rows; already-painted rows never shift.

**Decoupled persistence.** The decay above happens every sample; the *file
write* happens only every `WRITE_EVERY` (≈ 100) samples and once on clean exit.
A crash loses ≲ 100 samples of decay — negligible. Smoothness does not depend on
write cadence.

### From a value to a tint level

The tint level (0 dim, 1 dim-yellow, 2 yellow, 3 bold-red — unchanged
`TINT_SGR`) for value `v` in column `k` of kind `ki` is:

```
final = max(ceiling_level(k, v), relative_level)
```

**`relative_level` — high-tail percentile against history.** With the histogram
normalized to total mass 1, let `cdf(v)` be the fraction of mass at buckets `<
bucket(v)` plus half the mass in `bucket(v)` (mid-bucket, to avoid a step at the
boundary). Then:

- `cdf ≥ 0.90` → 1, `cdf ≥ 0.99` → 2, `cdf ≥ 0.999` → 3, else 0.

`relative_level` is forced to 0 when **either** guard trips:

- *Noise floor:* `v < FLOOR[ki]` — a loose per-kind absolute threshold below
  which we never relative-tint. This is the backstop for an all-idle history,
  whose p99.9 is itself a trivially small number; without it the first non-zero
  blip on a fresh, flat histogram would read as p100. Nominal floors (tunable):
  `pct` 2(%), `int` 1, `bytes` 0 (see limitation below), `bps` ~4 KiB/s, `count`
  ~10/s.
- *Warmup:* the column has seen fewer than `WARMUP` (≈ 100) effective samples.
  Track a per-column observation count; below the threshold only the ceiling can
  color. (A freshly loaded file is already warm.)

**`ceiling_level` — objective extremes, history-independent.** Only the few
columns whose meaning is the same on any machine get a ceiling; every other
column relies purely on the histogram (this is what auto-calibrates the
machine-relative columns whose absolute rates vary ~100× across hosts). The
ceiling forces a *minimum* level:

| col | direction | → 2 (yellow) | → 3 (red) |
|-----|-----------|--------------|-----------|
| `id` | low  | ≤ 10 % | ≤ 3 % |
| `wa` | high | ≥ 20 % | ≥ 40 % |
| `r`  | high | ≥ 1 × cores | ≥ 2 × cores |
| `b`  | high | ≥ 1 | ≥ 3 |

`id` low is the aggregate "CPU pinned" signal (it subsumes "us+sy high"), so
us/sy get no separate ceiling. `cores` comes from `cores_count()`
(`os.cpu_count()`, `topf.py:970`). Swap (`si`/`so`) gets no ceiling — absent on
most target machines, and the histogram covers it where present. Numbers are
starting points, tunable as named constants.

### Frozen per-row levels (no disco)

Today coloring is computed in `format_vmstat_pane` at render time over the
shown window. Instead, the level for each cell is computed **once, when its rate
row is produced**, against the histogram as it stood then, and stored alongside
the row.

- `run_live` keeps a ring of **colored rate rows**: `(rate_row, levels)` pairs,
  where `levels` is `{column_key: 0..3}`. One pair is appended per sample (the
  newest adjacent-sample pair yields exactly one new rate row); the ring is
  bounded to a small scrollback (e.g. `max(args.vmstat_rows, …) * 2`, the "twice
  the buffer" needed so the topmost displayed row keeps its own frozen color).
- `format_vmstat_pane` takes the precomputed `levels` and no longer calls any
  outlier function — it just looks up `levels[k]` for the SGR wrap. `repaint`
  reads from the colored-row ring instead of recomputing `vmstat_rate_rows`.

Scrolling and the newest-row-evicts-oldest churn now leave colors untouched,
because a row's color is a property of the row, not of the viewport.

## Persistence

**Location.** `${XDG_STATE_HOME:-~/.local/state}/topf/vmstat-hist.json`
(history that accrues value over time but is non-critical and self-rebuilding).
Overridable with `--history-file PATH`; `--no-history` disables load+save (the
session still colors from the histogram it builds in memory, ceiling + warmup
as usual).

**Format.** JSON, versioned:

```json
{
  "version": 1,
  "nbuckets": 40,
  "columns": {
    "us": {"count": 8123, "hist": [.. NBUCKETS floats ..]},
    ...
  }
}
```

(The per-kind `(lo, hi)` brackets are compile-time constants, not persisted —
the loader only needs `version` + `nbuckets` to validate shape. `count` is an
integer sample counter; `hist` holds float bucket mass.)

Human-inspectable, tiny (~17 × 40 floats). On load, a version/shape mismatch is
ignored (start fresh) rather than fatal. On clean exit and every `WRITE_EVERY`
samples, write to a temp file and `os.replace` it into place (atomic; never a
torn file).

**Concurrent instances.** Multiple topf processes on one host each load at
start, decay independently, and write every ~100 samples → **last-writer-wins**.
Acceptable: the histograms describe the same machine and converge; the atomic
write guarantees no corruption, only that one instance's recent decay may be
overwritten by another's. (A future refinement could re-read and merge on write;
out of scope here.)

## Known limitations (accepted)

- **High-tail only.** `free`/`buff`/`cache` are byte *levels* whose *meaningful*
  direction is *low* (memory pressure), but we color the high tail only and give
  them no ceiling (RAM-size dependent). They will therefore rarely tint usefully
  — an unusually *high* free is harmless to flag and an unusually *low* free
  won't flag. This is an accepted consequence of dropping two-tailed coloring;
  CPU/scheduler columns carry the load signal. (`bytes` noise floor is 0 since we
  are not trying to suppress them.)
- **Pure-relative on a perpetually-busy host** trends toward white for the
  machine-relative columns (busy is its norm); the ceiling keeps `id`/`wa`/`r`/`b`
  honest, which is the intended "is it objectively pinned" backstop.

## Testing

Pure, table-driven unit tests (no I/O) for:

- `bucket(v, kind)` — zero/floor → 0, monotonic over the log range, clamping at
  `lo`/`hi`.
- histogram decay — mass conservation toward 1, half-life behavior (after `H`
  samples of a new value, the old peak's share ≈ halved), order independence of
  the read-then-fold step.
- `cdf` / `relative_level` — p90/99/99.9 thresholds, mid-bucket interpolation,
  floor and warmup guards force 0.
- `ceiling_level` — each table row's two thresholds, `r` scaling with `cores`,
  un-listed columns → 0, `final = max(ceiling, relative)`.
- (de)serialize round-trip; version/shape-mismatch load → fresh; corrupt JSON →
  fresh, no raise.

Integration-ish (still pure where possible): `format_vmstat_pane` honors
supplied `levels` verbatim; the colored-row ring appends one per sample and is
bounded.

## CLI / constants summary

- `--history-file PATH` (default XDG state path), `--no-history`.
- `--vmstat-halflife N` samples (default ≈ 200) → `d`.
- Named constants: `NBUCKETS`, per-kind `(lo, hi)`, `FLOOR[kind]`, `WARMUP`,
  `WRITE_EVERY`, `CEILING` table, percentile anchors `(0.90, 0.99, 0.999)`.
