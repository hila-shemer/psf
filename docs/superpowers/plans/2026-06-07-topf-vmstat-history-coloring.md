# topf history-grounded vmstat coloring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the vmstat pane's window-relative cell coloring with coloring grounded in a persisted, per-column, exponentially-decaying log-scale histogram, so tints reflect "high for this machine over time" instead of "differs from the rows on screen."

**Architecture:** A set of pure functions operate on a plain-dict histogram state (`{col_key: {"hist": [floats], "count": int}}`): bucketing, per-sample decay/fold, CDF→relative level, an absolute ceiling for machine-independent columns, and JSON (de)serialize. `run_live` loads the state at startup, computes each rate row's tint levels **once** (frozen) as the sample arrives, folds the sample into the histogram, keeps a bounded ring of `(rate_row, levels)` pairs for the pane to replay, and writes the state every ~100 samples and on exit. `format_vmstat_pane` becomes a dumb renderer of precomputed levels. The old `outlier_level`/`VMSTAT_OUTLIER_ANCHORS` are deleted.

**Tech Stack:** Python 3 stdlib only (`math`, `json`, `os`), single file `topf.py`, `pytest` tests in `tests/test_topf.py`. All already imported (topf.py:17-28).

**Spec:** `docs/superpowers/specs/2026-06-07-topf-vmstat-history-coloring-design.md`

---

## File structure

- **Modify `topf.py`:**
  - Constants block near the other tint anchors (after `CPU_TINT_ANCHORS`, topf.py:52) — new histogram/ceiling constants.
  - New pure helpers near the existing vmstat core (after `_fmt_vmstat_cell`, topf.py:1137): `vmstat_bucket`, `vmstat_hist_new`, `vmstat_hist_fold`, `vmstat_cdf`, `vmstat_relative_level`, `vmstat_ceiling_level`, `vmstat_cell_level`, `vmstat_hist_to_json`, `vmstat_hist_from_json`, `vmstat_hist_path`, `vmstat_hist_load`, `vmstat_hist_save`.
  - Rewrite `format_vmstat_pane` (topf.py:1140) to take `colored_rows` (`[(row, levels)]`) and render precomputed levels.
  - Delete `outlier_level` (topf.py:1103) and `VMSTAT_OUTLIER_ANCHORS` (topf.py:71).
  - Wire into `run_live` (topf.py:1644+): load state, per-sample fold + frozen levels + colored ring, periodic + exit save; `repaint` uses the ring.
  - New CLI flags in `_parse_args` (topf.py:1885+) and attrs in the test defaults namespace (topf.py:1842).
- **Modify `tests/test_topf.py`:** delete the four `test_outlier_level_*` tests (topf.py tests at 384-401), update the four `test_format_vmstat_pane_*` calls to the new signature, add tests for every new pure function.

---

## Task 1: Histogram constants and bucketing

**Files:**
- Modify: `topf.py` (constants after line 52; `vmstat_bucket` after `_fmt_vmstat_cell`, ~line 1137)
- Test: `tests/test_topf.py`

- [ ] **Step 1: Write the failing test**

Add near the existing vmstat tests in `tests/test_topf.py`:

```python
# --- vmstat history-grounded coloring ---------------------------------------


def test_vmstat_bucket_zero_and_negative():
    assert topf.vmstat_bucket(0, "bps") == 0
    assert topf.vmstat_bucket(-5, "count") == 0
    assert topf.vmstat_bucket(None, "pct") == 0


def test_vmstat_bucket_monotonic_and_clamped():
    # within range, larger value -> same-or-higher bucket, never out of [1, N-1]
    last = 0
    for v in (1, 5, 50, 100):
        b = topf.vmstat_bucket(v, "pct")
        assert 1 <= b <= topf.VMSTAT_NBUCKETS - 1
        assert b >= last
        last = b
    # above hi clamps to the top bucket
    assert topf.vmstat_bucket(10 ** 12, "bps") == topf.VMSTAT_NBUCKETS - 1
    # at/below lo lands in bucket 1
    assert topf.vmstat_bucket(1, "bps") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_topf.py -k vmstat_bucket -q`
Expected: FAIL — `AttributeError: module 'topf' has no attribute 'vmstat_bucket'`.

- [ ] **Step 3: Write the constants and the function**

Insert the constants in `topf.py` immediately after `CPU_TINT_ANCHORS = ...` (line 52):

```python
# --- vmstat history-grounded coloring ---------------------------------------
# A per-column decaying log-scale histogram is the sole frame of reference for
# tinting a vmstat cell: a value is red because it is high *for this machine
# over time*, not because it differs from the rows currently on screen. The
# histogram decays per sample (exponential, by half-life) and is persisted so
# coloring is grounded from the first line of the next run.
VMSTAT_NBUCKETS = 40
# Per-kind log brackets [lo, hi] for buckets 1..NBUCKETS-1 (bucket 0 = zero).
VMSTAT_KIND_RANGE = {
    "pct": (1.0, 100.0),
    "int": (1.0, 4096.0),
    "bytes": (1024.0 ** 2, 1024.0 ** 4),     # 1 MiB .. 1 TiB
    "bps": (1.0, 1e10),
    "count": (1.0, 1e10),
}
# Loose absolute noise floors: below these we never *relative*-tint (a backstop
# for an all-idle history whose own p99.9 is a trivially small number). bytes=0
# because memory-level columns are not suppressed (design: high-tail only).
VMSTAT_FLOOR = {"pct": 2.0, "int": 1.0, "bytes": 0.0, "bps": 4096.0,
                "count": 10.0}
VMSTAT_PCT_ANCHORS = (0.90, 0.99, 0.999)    # cdf thresholds -> tint level 1..3
VMSTAT_WARMUP = 100         # per-column samples before relative coloring engages
VMSTAT_WRITE_EVERY = 100    # samples between history-file writes
VMSTAT_HALFLIFE_DEFAULT = 200   # samples to halve a sample's weight (~3min @1s)
# Absolute ceiling: objectively-extreme, machine-independent columns forced to a
# minimum tint regardless of history. (mode, lvl2, lvl3); "r" scales by cores.
# Every other column relies purely on the histogram.
VMSTAT_CEILING = {
    "id": ("low", 10.0, 3.0),
    "wa": ("high", 20.0, 40.0),
    "r":  ("high_cores", 1.0, 2.0),
    "b":  ("high", 1.0, 3.0),
}
```

Insert the function after `_fmt_vmstat_cell` (after topf.py:1137):

```python
def vmstat_bucket(value, kind):
    """Log-scale bucket index 0..NBUCKETS-1 for `value` of column `kind`.
    Bucket 0 is zero/non-positive; 1..NBUCKETS-1 are log-spaced over the kind's
    [lo, hi] range, clamped at both ends."""
    if value is None or value <= 0:
        return 0
    lo, hi = VMSTAT_KIND_RANGE[kind]
    frac = (math.log(value) - math.log(lo)) / (math.log(hi) - math.log(lo))
    idx = 1 + int(frac * (VMSTAT_NBUCKETS - 1))
    return max(1, min(VMSTAT_NBUCKETS - 1, idx))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_topf.py -k vmstat_bucket -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: vmstat coloring histogram constants + log bucketing"
```

---

## Task 2: Per-sample decay/fold

**Files:**
- Modify: `topf.py` (`vmstat_hist_new`, `vmstat_hist_fold` after `vmstat_bucket`)
- Test: `tests/test_topf.py`

- [ ] **Step 1: Write the failing test**

```python
def test_vmstat_hist_new_shape():
    state = topf.vmstat_hist_new()
    assert set(state) == {k for k, _h, _ki in topf.VMSTAT_COLS}
    assert state["us"]["count"] == 0
    assert state["us"]["hist"] == [0.0] * topf.VMSTAT_NBUCKETS


def test_vmstat_hist_fold_counts_and_skips_none():
    col = {"hist": [0.0] * topf.VMSTAT_NBUCKETS, "count": 0}
    topf.vmstat_hist_fold(col, 50.0, "pct", 0.5)
    assert col["count"] == 1
    assert sum(col["hist"]) > 0
    topf.vmstat_hist_fold(col, None, "pct", 0.5)   # None ignored
    assert col["count"] == 1


def test_vmstat_hist_fold_halflife_decays_old_mass():
    # Saturate bucket for value A, then fold value B for one half-life worth of
    # samples; A's bucket mass should be ~halved (decayed by d ** H == 0.5).
    H = 8
    d = 0.5 ** (1.0 / H)
    col = {"hist": [0.0] * topf.VMSTAT_NBUCKETS, "count": 0}
    a_bucket = topf.vmstat_bucket(50.0, "pct")
    for _ in range(500):                 # saturate toward (1-d) steady mass
        topf.vmstat_hist_fold(col, 50.0, "pct", d)
    peak = col["hist"][a_bucket]
    for _ in range(H):                   # one half-life of a different value
        topf.vmstat_hist_fold(col, 5.0, "pct", d)
    assert abs(col["hist"][a_bucket] - peak * 0.5) < peak * 0.02
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_topf.py -k vmstat_hist_fold -q`
Expected: FAIL — `AttributeError: ... 'vmstat_hist_new'` / `'vmstat_hist_fold'`.

- [ ] **Step 3: Write the implementation**

Add after `vmstat_bucket`:

```python
def vmstat_hist_new():
    """Fresh per-column histogram state: {col_key: {hist: [floats], count}}."""
    return {k: {"hist": [0.0] * VMSTAT_NBUCKETS, "count": 0}
            for k, _h, _ki in VMSTAT_COLS}


def vmstat_hist_fold(col, value, kind, d):
    """Decay all of `col`'s bucket mass by `d` and add the new sample's (1-d)
    unit to its bucket; bump the warmup count. None values are ignored (no
    decay, no count). Mutates `col` in place."""
    if value is None:
        return
    h = col["hist"]
    b = vmstat_bucket(value, kind)
    for i in range(len(h)):
        h[i] *= d
    h[b] += (1.0 - d)
    col["count"] += 1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_topf.py -k "vmstat_hist_new or vmstat_hist_fold" -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: vmstat histogram state + per-sample exponential fold"
```

---

## Task 3: CDF and relative (percentile) level

**Files:**
- Modify: `topf.py` (`vmstat_cdf`, `vmstat_relative_level` after `vmstat_hist_fold`)
- Test: `tests/test_topf.py`

- [ ] **Step 1: Write the failing test**

```python
def _warm_col(kind, *values, d=0.99):
    col = {"hist": [0.0] * topf.VMSTAT_NBUCKETS, "count": 0}
    for v in values:
        topf.vmstat_hist_fold(col, v, kind, d)
    return col


def test_vmstat_cdf_empty_is_zero():
    col = {"hist": [0.0] * topf.VMSTAT_NBUCKETS, "count": 0}
    assert topf.vmstat_cdf(col, 10.0, "pct") == 0.0


def test_vmstat_cdf_high_value_near_one():
    # mass concentrated low; a far-higher value sits near the top of the cdf
    col = _warm_col("count", *([20.0] * 200))
    assert topf.vmstat_cdf(col, 20.0, "count") < 0.6     # mid-bucket of the mass
    assert topf.vmstat_cdf(col, 10 ** 8, "count") > 0.99


def test_vmstat_relative_level_floor_and_warmup():
    col = _warm_col("count", *([20.0] * 200))            # warm (count >= WARMUP)
    assert topf.vmstat_relative_level(col, 10 ** 8, "count") == 3
    # below the kind's floor -> never tints
    assert topf.vmstat_relative_level(col, 0.0, "count") == 0
    # cold column (count < WARMUP) -> never tints
    cold = _warm_col("count", *([20.0] * 10))
    assert topf.vmstat_relative_level(cold, 10 ** 8, "count") == 0
    # None -> 0
    assert topf.vmstat_relative_level(col, None, "count") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_topf.py -k "vmstat_cdf or vmstat_relative_level" -q`
Expected: FAIL — `AttributeError: ... 'vmstat_cdf'`.

- [ ] **Step 3: Write the implementation**

Add after `vmstat_hist_fold`:

```python
def vmstat_cdf(col, value, kind):
    """Fraction of `col`'s histogram mass below `value`'s bucket, plus half that
    bucket's own mass (mid-bucket interpolation smooths the boundary). 0.0 when
    there is no mass yet."""
    h = col["hist"]
    total = sum(h)
    if total <= 0:
        return 0.0
    b = vmstat_bucket(value, kind)
    return (sum(h[:b]) + 0.5 * h[b]) / total


def vmstat_relative_level(col, value, kind):
    """High-tail percentile tint 0..3, gated by the kind's noise floor and the
    per-column warmup count. Levels come from VMSTAT_PCT_ANCHORS."""
    if value is None or value < VMSTAT_FLOOR[kind]:
        return 0
    if col["count"] < VMSTAT_WARMUP:
        return 0
    c = vmstat_cdf(col, value, kind)
    return sum(1 for a in VMSTAT_PCT_ANCHORS if c >= a)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_topf.py -k "vmstat_cdf or vmstat_relative_level" -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: vmstat cdf + floor/warmup-gated relative tint level"
```

---

## Task 4: Absolute ceiling and combined cell level

**Files:**
- Modify: `topf.py` (`vmstat_ceiling_level`, `vmstat_cell_level` after `vmstat_relative_level`)
- Test: `tests/test_topf.py`

- [ ] **Step 1: Write the failing test**

```python
def test_vmstat_ceiling_low_idle():
    assert topf.vmstat_ceiling_level("id", 2.0, cores=4) == 3
    assert topf.vmstat_ceiling_level("id", 8.0, cores=4) == 2
    assert topf.vmstat_ceiling_level("id", 50.0, cores=4) == 0


def test_vmstat_ceiling_high_wait_and_blocked():
    assert topf.vmstat_ceiling_level("wa", 45.0, cores=4) == 3
    assert topf.vmstat_ceiling_level("wa", 25.0, cores=4) == 2
    assert topf.vmstat_ceiling_level("b", 5, cores=4) == 3
    assert topf.vmstat_ceiling_level("b", 0, cores=4) == 0


def test_vmstat_ceiling_runqueue_scales_with_cores():
    assert topf.vmstat_ceiling_level("r", 8, cores=4) == 3      # >= 2*cores
    assert topf.vmstat_ceiling_level("r", 4, cores=4) == 2      # >= 1*cores
    assert topf.vmstat_ceiling_level("r", 1, cores=4) == 0


def test_vmstat_ceiling_unlisted_and_none():
    assert topf.vmstat_ceiling_level("free", 10 ** 12, cores=4) == 0
    assert topf.vmstat_ceiling_level("id", None, cores=4) == 0


def test_vmstat_cell_level_is_max_of_ceiling_and_relative():
    # relative says 0 (cold), ceiling says 3 -> final 3
    cold = {"hist": [0.0] * topf.VMSTAT_NBUCKETS, "count": 0}
    assert topf.vmstat_cell_level("id", "pct", 2.0, cold, cores=4) == 3
    # neither fires -> 0
    assert topf.vmstat_cell_level("us", "pct", 1.0, cold, cores=4) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_topf.py -k "vmstat_ceiling or vmstat_cell_level" -q`
Expected: FAIL — `AttributeError: ... 'vmstat_ceiling_level'`.

- [ ] **Step 3: Write the implementation**

Add after `vmstat_relative_level`:

```python
def vmstat_ceiling_level(key, value, cores):
    """Objective-extreme minimum tint for the machine-independent columns
    (VMSTAT_CEILING); 0 for any other column or a None value. 'low' columns tint
    as the value drops; 'high'/'high_cores' as it rises ('high_cores' scales the
    thresholds by the core count)."""
    spec = VMSTAT_CEILING.get(key)
    if spec is None or value is None:
        return 0
    mode, t2, t3 = spec
    if mode == "low":
        if value <= t3:
            return 3
        return 2 if value <= t2 else 0
    if mode == "high_cores":
        t2, t3 = t2 * cores, t3 * cores
    if value >= t3:
        return 3
    return 2 if value >= t2 else 0


def vmstat_cell_level(key, kind, value, col, cores):
    """Final tint 0..3 for a cell: the stronger of the absolute ceiling and the
    history-relative percentile level."""
    return max(vmstat_ceiling_level(key, value, cores),
               vmstat_relative_level(col, value, kind))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_topf.py -k "vmstat_ceiling or vmstat_cell_level" -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: vmstat absolute ceiling + combined cell tint level"
```

---

## Task 5: JSON (de)serialize

**Files:**
- Modify: `topf.py` (`vmstat_hist_to_json`, `vmstat_hist_from_json` after `vmstat_cell_level`)
- Test: `tests/test_topf.py`

- [ ] **Step 1: Write the failing test**

```python
def test_vmstat_hist_json_roundtrip():
    state = topf.vmstat_hist_new()
    topf.vmstat_hist_fold(state["us"], 50.0, "pct", 0.99)
    state["us"]["count"] = 7
    back = topf.vmstat_hist_from_json(topf.vmstat_hist_to_json(state))
    assert back["us"]["count"] == 7
    assert back["us"]["hist"] == state["us"]["hist"]


def test_vmstat_hist_from_json_bad_input_is_fresh():
    assert topf.vmstat_hist_from_json("not json")["us"]["count"] == 0
    assert topf.vmstat_hist_from_json('{"version": 99}')["us"]["count"] == 0
    # right version, wrong bucket count -> fresh
    bad = '{"version": 1, "nbuckets": 3, "columns": {}}'
    assert topf.vmstat_hist_from_json(bad)["us"]["count"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_topf.py -k vmstat_hist_json -q`
Expected: FAIL — `AttributeError: ... 'vmstat_hist_to_json'`.

- [ ] **Step 3: Write the implementation**

Add after `vmstat_cell_level`:

```python
def vmstat_hist_to_json(state):
    """Serialize histogram state to a versioned JSON string."""
    return json.dumps({
        "version": 1,
        "nbuckets": VMSTAT_NBUCKETS,
        "columns": {k: {"hist": v["hist"], "count": v["count"]}
                    for k, v in state.items()},
    })


def vmstat_hist_from_json(text):
    """Parse a history file back to state. Any parse error, version mismatch, or
    shape mismatch yields a fresh state — never raises. Unknown/old columns are
    ignored; missing columns start empty."""
    try:
        d = json.loads(text)
        if d.get("version") != 1 or d.get("nbuckets") != VMSTAT_NBUCKETS:
            return vmstat_hist_new()
        cols = d["columns"]
        state = vmstat_hist_new()
        for k in state:
            c = cols.get(k)
            if c and len(c["hist"]) == VMSTAT_NBUCKETS:
                state[k]["hist"] = [float(x) for x in c["hist"]]
                state[k]["count"] = int(c["count"])
        return state
    except (ValueError, KeyError, TypeError):
        return vmstat_hist_new()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_topf.py -k vmstat_hist_json -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: vmstat history JSON (de)serialize, fresh on mismatch"
```

---

## Task 6: History file path + atomic load/save

**Files:**
- Modify: `topf.py` (`vmstat_hist_path`, `vmstat_hist_load`, `vmstat_hist_save` after `vmstat_hist_from_json`)
- Test: `tests/test_topf.py`

- [ ] **Step 1: Write the failing test**

```python
import types as _types


def test_vmstat_hist_path_explicit_and_xdg(monkeypatch):
    args = _types.SimpleNamespace(history_file="/tmp/explicit.json")
    assert topf.vmstat_hist_path(args) == "/tmp/explicit.json"
    args = _types.SimpleNamespace(history_file=None)
    monkeypatch.setenv("XDG_STATE_HOME", "/xdg/state")
    assert topf.vmstat_hist_path(args) == "/xdg/state/topf/vmstat-hist.json"


def test_vmstat_hist_save_then_load_roundtrip(tmp_path):
    path = str(tmp_path / "sub" / "hist.json")     # parent dir created on save
    state = topf.vmstat_hist_new()
    state["cs"]["count"] = 42
    topf.vmstat_hist_save(path, state)
    assert topf.vmstat_hist_load(path)["cs"]["count"] == 42


def test_vmstat_hist_load_missing_file_is_fresh(tmp_path):
    path = str(tmp_path / "nope.json")
    assert topf.vmstat_hist_load(path)["cs"]["count"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_topf.py -k vmstat_hist_path -q`
Expected: FAIL — `AttributeError: ... 'vmstat_hist_path'`.

- [ ] **Step 3: Write the implementation**

Add after `vmstat_hist_from_json`:

```python
def vmstat_hist_path(args):
    """Resolve the history-file path: --history-file if given, else
    $XDG_STATE_HOME/topf/vmstat-hist.json (default ~/.local/state)."""
    if args.history_file:
        return args.history_file
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser(
        "~/.local/state")
    return os.path.join(base, "topf", "vmstat-hist.json")


def vmstat_hist_load(path):
    """Load histogram state from `path`; a missing/unreadable file -> fresh."""
    try:
        with open(path) as f:
            return vmstat_hist_from_json(f.read())
    except OSError:
        return vmstat_hist_new()


def vmstat_hist_save(path, state):
    """Atomically write state to `path` (temp file + os.replace). Best-effort:
    any OSError is swallowed so a read-only state dir never crashes topf."""
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(vmstat_hist_to_json(state))
        os.replace(tmp, path)
    except OSError:
        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_topf.py -k "vmstat_hist_path or vmstat_hist_save or vmstat_hist_load" -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: vmstat history file path + atomic load/save"
```

---

## Task 7: Render precomputed levels; delete window-relative coloring

**Files:**
- Modify: `topf.py` (`format_vmstat_pane`, topf.py:1140-1178; delete `outlier_level` topf.py:1103-1123 and `VMSTAT_OUTLIER_ANCHORS` topf.py:71)
- Test: `tests/test_topf.py` (delete `test_outlier_level_*`; update `test_format_vmstat_pane_*`)

- [ ] **Step 1: Update the existing pane tests to the new signature**

`format_vmstat_pane` will take `colored_rows` — a list of `(rate_row, levels)` pairs — instead of bare rate rows. Edit the four `test_format_vmstat_pane_*` tests in `tests/test_topf.py` to wrap each row as `(row, {})` and add one test that a supplied level tints the cell. Replace the existing four tests with:

```python
def test_format_vmstat_pane_header_and_swap_off():
    row = _rate_row(free=3 * 1024**3, bi=0, ni=1024**2)
    row["in"] = 9100
    lines = topf.format_vmstat_pane([(row, {})], swap_on=False, width=200,
                                    height=4, color=False)
    header = lines[0]
    assert header.startswith(topf.VMSTAT_GUTTER)
    assert " si " not in header and " so " not in header
    assert " ni " in header and " no " in header
    assert " us " in header and " id " in header


def test_format_vmstat_pane_swap_on_includes_si_so():
    lines = topf.format_vmstat_pane([(_rate_row(), {})], swap_on=True, width=200,
                                    height=3, color=False)
    assert " si " in lines[0] and " so " in lines[0]


def test_format_vmstat_pane_uses_human_units():
    row = _rate_row(free=2 * 1024**3, ni=4 * 1024**2)
    lines = topf.format_vmstat_pane([(row, {})], swap_on=False, width=200,
                                    height=3, color=False)
    body = lines[-1]
    assert "2.0G" in body and "4.0M" in body


def test_format_vmstat_pane_dashes_when_empty():
    lines = topf.format_vmstat_pane([], swap_on=False, width=200, height=3,
                                    color=False)
    assert lines and lines[0].startswith(topf.VMSTAT_GUTTER)
    assert len(lines) == 1


def test_format_vmstat_pane_tints_from_supplied_levels():
    row = _rate_row(us=99)
    lines = topf.format_vmstat_pane([(row, {"us": 3})], swap_on=False,
                                    width=200, height=3, color=True)
    assert "\x1b[%sm" % topf.TINT_SGR[3] in lines[-1]   # bold-red wrap present
```

Also delete the four `test_outlier_level_*` tests and the `# --- vmstat outlier coloring ---` banner comment above them.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_topf.py -k format_vmstat_pane -q`
Expected: FAIL — current `format_vmstat_pane` iterates rows expecting dicts, not `(row, levels)` pairs (e.g. `AttributeError`/`TypeError`), and `test_..._tints_from_supplied_levels` has no level path yet.

- [ ] **Step 3: Rewrite `format_vmstat_pane` and delete dead code**

Replace `format_vmstat_pane` (topf.py:1140-1178) with:

```python
def format_vmstat_pane(colored_rows, swap_on, width, height, color):
    """Render the pinned vmstat pane: a header row of column names plus up to
    height-1 data rows (oldest..newest, top..bottom), columns right-aligned to
    their content, each data cell tinted by its *precomputed* level. colored_rows
    is a list of (rate_row dict, levels dict) — levels[k] is 0..3 indexing
    TINT_SGR and was frozen when the row was sampled, so scrolling never recolors.
    swap_on=False drops si/so. No data rows -> header only (stable layout)."""
    cols = [(k, h, ki) for (k, h, ki) in VMSTAT_COLS
            if swap_on or k not in SWAP_KEYS]
    shown = colored_rows[-(height - 1):] if height > 1 else []

    formatted = {k: [_fmt_vmstat_cell(r.get(k), ki) for r, _lv in shown]
                 for (k, _h, ki) in cols}
    colw = {k: max(len(h), max((len(c) for c in formatted[k]), default=0))
            for (k, h, _ki) in cols}

    gutter = VMSTAT_GUTTER
    pad = " " * len(gutter)

    def join_cells(cell_strs):
        return "  ".join(s.rjust(colw[k]) for (k, _h, _ki), s in
                         zip(cols, cell_strs))

    lines = [gutter + "  " + join_cells([h for (_k, h, _ki) in cols])]

    for ri, (_r, lv) in enumerate(shown):
        cells = []
        for (k, _h, _ki) in cols:
            cell = formatted[k][ri]
            lpad = " " * (colw[k] - len(cell))       # right-align padding
            if color:
                level = lv.get(k, 0)
                if level:
                    cell = "\x1b[%sm%s\x1b[0m" % (TINT_SGR[level], cell)
            cells.append(lpad + cell)                # pad OUTSIDE the SGR wrap
        lines.append(pad + "  " + "  ".join(cells))
    return lines
```

Delete `outlier_level` entirely (topf.py:1103-1123) and the `VMSTAT_OUTLIER_ANCHORS = ...` line (topf.py:71).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_topf.py -k "format_vmstat_pane or outlier" -q`
Expected: PASS for the 5 pane tests; 0 outlier tests collected (deleted).

- [ ] **Step 5: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: vmstat pane renders frozen per-row levels; drop outlier_level"
```

---

## Task 8: Wire history coloring into the live loop + CLI

**Files:**
- Modify: `topf.py` (`run_live` topf.py:1644-1706+; `_parse_args` topf.py:1885+; test defaults namespace topf.py:1842)
- Test: `tests/test_topf.py` (defaults-namespace attrs assertion)

- [ ] **Step 1: Add the CLI flags and defaults-namespace attributes (and a guard test)**

In `_parse_args`, after the `--vmstat-rows` argument (topf.py:1896-1898), add:

```python
    ap.add_argument("--history-file", default=None,
                    help="vmstat coloring history file (default: XDG state dir)")
    ap.add_argument("--no-history", action="store_true",
                    help="do not load or save vmstat coloring history")
    ap.add_argument("--vmstat-halflife", type=int,
                    default=VMSTAT_HALFLIFE_DEFAULT,
                    help="samples for a vmstat coloring weight to halve "
                         "(default %d)" % VMSTAT_HALFLIFE_DEFAULT)
```

In the test defaults namespace (`_defaults_ns`/`render_once` helper, topf.py:1842), extend the `SimpleNamespace(...)` with:

```python
        history_file=None, no_history=True,
        vmstat_halflife=VMSTAT_HALFLIFE_DEFAULT,
```

Add a guard test in `tests/test_topf.py`:

```python
def test_parse_args_history_defaults():
    args = topf._parse_args([])
    assert args.history_file is None
    assert args.no_history is False
    assert args.vmstat_halflife == topf.VMSTAT_HALFLIFE_DEFAULT
```

- [ ] **Step 2: Run the guard test to verify it fails**

Run: `python -m pytest tests/test_topf.py -k parse_args_history -q`
Expected: FAIL — `AttributeError: 'Namespace' object has no attribute 'history_file'`.

- [ ] **Step 3: Wire the histogram into `run_live`**

In `run_live`, after `sysinfo_cores = cores_count()` (topf.py:1651) add (4-space `run_live`-body indent, matching the surrounding `vmring = []` etc.):

```python
    vmhist = (vmstat_hist_new() if args.no_history
              else vmstat_hist_load(vmstat_hist_path(args)))
    vmd = 0.5 ** (1.0 / max(1, args.vmstat_halflife))
    vmcolored = []          # ring of (rate_row, levels), frozen at sample
    vm_write_ctr = [0]
```

In `sample_and_build`, replace the existing vmring block (topf.py:1684-1686):

```python
        vmring.append(read_vmstat_sample(t_now))
        if len(vmring) > args.vmstat_rows + 1:
            del vmring[0]
```

with:

```python
        vmring.append(read_vmstat_sample(t_now))
        if len(vmring) > args.vmstat_rows + 1:
            del vmring[0]
        if len(vmring) >= 2:
            prev_s, cur_s = vmring[-2], vmring[-1]
            dt = cur_s.t - prev_s.t
            if dt > 0:
                row = _vmstat_row(prev_s, cur_s, dt)
                levels = {}
                for k, _h, ki in VMSTAT_COLS:
                    val = row.get(k)
                    levels[k] = vmstat_cell_level(k, ki, val, vmhist[k],
                                                  sysinfo_cores)
                    vmstat_hist_fold(vmhist[k], val, ki, vmd)   # fold AFTER level
                vmcolored.append((row, levels))
                if len(vmcolored) > args.vmstat_rows * 2 + 2:
                    del vmcolored[0]
                vm_write_ctr[0] += 1
                if not args.no_history and \
                        vm_write_ctr[0] % VMSTAT_WRITE_EVERY == 0:
                    vmstat_hist_save(vmstat_hist_path(args), vmhist)
```

In `repaint`, replace `rate_rows = vmstat_rate_rows(vmring)` (topf.py:1659) and the two places it is used:

```python
        region_h, pane_h, show = split_regions(
            term_rows, cols, ui.vmstat_on, args.vmstat_rows, len(vmcolored))
```

and

```python
        if show:
            swap_on = any(s.swap_total for s in vmring if s.swap_total)
            frame.append("─" * cols)
            frame += format_vmstat_pane(vmcolored, swap_on, cols, pane_h - 1,
                                        color=not args.no_color)
```

(Remove the now-unused `rate_rows = vmstat_rate_rows(vmring)` line from `repaint`.)

Add a final save on exit, inside the existing terminal-restore `finally` block (topf.py:1744-1749). After the `termios.tcsetattr(...)` line and before/after the `if not args.no_cache:` cache save, add:

```python
        if not args.no_history:
            vmstat_hist_save(vmstat_hist_path(args), vmhist)
```

So the tail reads:

```python
    finally:
        out.write("\x1b[?1049l")
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
        if not args.no_history:
            vmstat_hist_save(vmstat_hist_path(args), vmhist)
        if not args.no_cache:
            Cache(cache_path(), boot_id=read_boot_id(),
                  now=time.time()).save(live_keys=set())
```

Because `vmhist`/`vmcolored`/`vm_write_ctr` are created at the top of `run_live` (Step 3, after `sysinfo_cores`), they are in scope in the `finally` even if the loop never iterates.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/test_topf.py -q`
Expected: PASS (all, including `test_parse_args_history_defaults`).

- [ ] **Step 5: Smoke-test the binary**

Run: `python topf.py --help | grep -E "history|halflife"`
Expected: the three new flags (`--history-file`, `--no-history`, `--vmstat-halflife`) appear.

Note: the history file is only written from `run_live`, which requires a real
TTY (with stdout redirected, topf takes the `--once` path and `run_live` never
runs). Persistence is covered by Task 6's unit tests; to verify the live write
by hand, run `XDG_STATE_HOME=$(mktemp -d) python topf.py` in a terminal, quit
with `q`, and check `$XDG_STATE_HOME/topf/vmstat-hist.json` exists (a write
happens every `VMSTAT_WRITE_EVERY` samples and on clean exit). A scripted PTY
check is optional:

```bash
D=$(mktemp -d); cd "$(git rev-parse --show-toplevel)"; XDG_STATE_HOME=$D python - <<'PY'
import os, pty, time
pid, fd = pty.fork()
if pid == 0:
    os.execvp("python", ["python", "topf.py", "--vmstat-halflife", "20"])
time.sleep(3); os.write(fd, b"q"); time.sleep(0.5)
PY
ls -l "$D/topf/vmstat-hist.json" 2>/dev/null && echo OK || echo "no file yet"
```
Expected: clean exit and `OK` (the on-exit save runs when `q` is received).

- [ ] **Step 6: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: live vmstat history coloring + --history-file/--no-history/--vmstat-halflife"
```

---

## Task 9: Update committed pyc artifacts and final verification

**Files:**
- Modify: `__pycache__/topf.cpython-314.pyc`, `tests/__pycache__/test_topf.cpython-314-pytest-8.3.5.pyc` (these are tracked — the repo commits bytecode; the git status shows them already dirty)

- [ ] **Step 1: Regenerate and run everything**

Run: `python -m pytest tests/test_topf.py -q && python -m py_compile topf.py`
Expected: all tests PASS; compile clean.

- [ ] **Step 2: Confirm dead references are gone**

Run: `rg -n "outlier_level|VMSTAT_OUTLIER_ANCHORS" topf.py tests/test_topf.py`
Expected: no matches.

- [ ] **Step 3: Commit any regenerated artifacts**

```bash
git add -A
git commit -m "chore: regenerate bytecode for vmstat history coloring"
```

(If the `.pyc` files are not meant to be tracked, skip this task — confirm with the repo owner; the initial git status listed them as modified, so they appear to be tracked.)

---

## Self-review notes

- **Spec coverage:** decaying log histogram (Tasks 1-2), high-tail percentile + floor + warmup (Task 3), ceiling table incl. `r`×cores (Task 4), `max(ceiling, relative)` (Task 4), persistence/atomic/version-tolerant (Tasks 5-6), frozen per-row levels + no-disco renderer (Task 7), live wiring + decoupled write cadence + exit save + flags (Task 8), removal of `outlier_level`/anchors (Task 7). High-tail-only limitation is inherent (no two-tailed code added).
- **Type consistency:** state shape `{col_key: {"hist": list[float], "count": int}}` is identical across `vmstat_hist_new`, `_fold`, `_cdf`, `_relative_level`, `_cell_level`, JSON (de)serialize, and the live loop. `format_vmstat_pane` consistently consumes `(row, levels)` pairs in Task 7 and Task 8. `vmstat_cell_level(key, kind, value, col, cores)` signature matches its call site in `sample_and_build`.
- **No placeholders:** every code step is complete.
