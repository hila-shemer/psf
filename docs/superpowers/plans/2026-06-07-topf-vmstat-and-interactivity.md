# topf vmstat pane + interactive scroll/expand — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a pinned bottom `vmstat`-style pane (reimplemented from `/proc`, human units + outlier color) and an interactive cursor/scroll/expand model to topf's live tree.

**Architecture:** Everything stays in the single stdlib-only `topf.py`. New work is mostly *pure* functions (vmstat parsers/ring/formatter; identity helpers; a row-record builder; a viewport presenter) that are unit-tested the way the existing core is. The live loop (`run_live`) gains a persistent `UIState` and composes three regions (pinned header, scrolling tree, pinned vmstat) instead of one clipped list.

**Tech Stack:** Python 3 stdlib only. Tests: `pytest` in `tests/test_topf.py`. Raw ANSI (no curses).

**Spec:** `docs/superpowers/specs/2026-06-07-topf-vmstat-and-interactivity-design.md`

---

## File structure

- **Modify `topf.py`** — all production code. New sections, in dependency order:
  - vmstat `/proc` parsers (`parse_proc_stat_counters`, `parse_meminfo`, `parse_vmstat_counters`, `parse_net_dev`).
  - vmstat sample model (`VmstatSample`, `read_vmstat_sample`, `vmstat_rate_rows`) + `fmt_count`.
  - `outlier_level` + `format_vmstat_pane`.
  - identity helpers (`proc_id`, `group_id`, `ROOT_ID`) + `collapse` returning `collapsible`.
  - `Row`, `build_rows`, `render` wrapper, `prepare_frame` extraction.
  - `UIState`, `selectable_ids`, `move_cursor`, `present_viewport`, `split_regions`.
  - `run_live` rewrite (layout + keys), header/glossary/flag updates.
- **Modify `tests/test_topf.py`** — append tests per task (flat functions, `topf.*`).

## Shared definitions (introduced by the tasks that first need them — listed here for consistency)

```python
# vmstat per-sample raw counters (monotonic t); any field may be None if /proc lacked it.
VmstatSample = namedtuple("VmstatSample",
    "t procs_running procs_blocked cpu_user cpu_nice cpu_system cpu_idle "
    "cpu_iowait cpu_total intr ctxt pgpgin pgpgout pswpin pswpout rx tx "
    "free buff cache swap_total")

# vmstat columns: (key, header, kind). kind in {int, bytes, bps, count, pct}.
VMSTAT_COLS = [
    ("r", "r", "int"), ("b", "b", "int"),
    ("free", "free", "bytes"), ("buff", "buff", "bytes"), ("cache", "cache", "bytes"),
    ("si", "si", "bps"), ("so", "so", "bps"),
    ("bi", "bi", "bps"), ("bo", "bo", "bps"),
    ("ni", "ni", "bps"), ("no", "no", "bps"),
    ("in", "in", "count"), ("cs", "cs", "count"),
    ("us", "us", "pct"), ("sy", "sy", "pct"), ("id", "id", "pct"), ("wa", "wa", "pct"),
]
SWAP_KEYS = frozenset({"si", "so"})

VMSTAT_OUTLIER_ANCHORS = (3.0, 6.0, 10.0)   # robust z-score levels -> TINT_SGR 1..3
VMSTAT_GUTTER = "vmstat"

MIN_ROWS_FOR_VMSTAT = 18
MIN_COLS_FOR_VMSTAT = 60
MIN_TREE_ROWS = 5
MIN_VMSTAT_SAMPLE_ROWS = 3
VMSTAT_ROWS_DEFAULT = 12

Row = namedtuple("Row", "text item_id expandable selectable")
ROOT_ID = ("root",)
```

---

### Task 1: vmstat /proc parsers

**Files:**
- Modify: `topf.py` (new "vmstat parsing" section, after `parse_net_unix`)
- Test: `tests/test_topf.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_proc_stat_counters_basic():
    txt = ("cpu  100 5 30 1000 20 1 2 0 0 0\n"
           "cpu0 50 2 15 500 10 0 1 0 0 0\n"
           "intr 12345 0 0\n"
           "ctxt 67890\n"
           "procs_running 3\n"
           "procs_blocked 1\n")
    c = topf.parse_proc_stat_counters(txt)
    assert c["cpu_user"] == 100 and c["cpu_nice"] == 5
    assert c["cpu_system"] == 30 and c["cpu_idle"] == 1000 and c["cpu_iowait"] == 20
    assert c["cpu_total"] == 100 + 5 + 30 + 1000 + 20 + 1 + 2  # all fields on the cpu line
    assert c["intr"] == 12345 and c["ctxt"] == 67890
    assert c["procs_running"] == 3 and c["procs_blocked"] == 1


def test_parse_proc_stat_counters_missing_fields_are_none():
    c = topf.parse_proc_stat_counters("cpu 1 1 1 1 1\n")
    assert c["intr"] is None and c["procs_blocked"] is None


def test_parse_meminfo_to_bytes():
    txt = "MemFree:  1024 kB\nBuffers: 2048 kB\nCached: 4096 kB\nSwapTotal: 0 kB\n"
    m = topf.parse_meminfo(txt)
    assert m["free"] == 1024 * 1024 and m["buff"] == 2048 * 1024
    assert m["cache"] == 4096 * 1024 and m["swap_total"] == 0


def test_parse_vmstat_counters_basic():
    txt = "pgpgin 10\npgpgout 20\npswpin 3\npswpout 4\nnr_free_pages 999\n"
    v = topf.parse_vmstat_counters(txt)
    assert v["pgpgin"] == 10 and v["pgpgout"] == 20
    assert v["pswpin"] == 3 and v["pswpout"] == 4


def test_parse_net_dev_sums_excluding_lo():
    txt = ("Inter-|   Receive                    |  Transmit\n"
           " face |bytes    packets ... |bytes    packets ...\n"
           "    lo: 500 1 0 0 0 0 0 0 600 1 0 0 0 0 0 0\n"
           "  eth0: 1000 5 0 0 0 0 0 0 2000 7 0 0 0 0 0 0\n"
           "  eth1: 30 1 0 0 0 0 0 0 40 1 0 0 0 0 0 0\n")
    rx, tx = topf.parse_net_dev(txt)
    assert rx == 1000 + 30 and tx == 2000 + 40   # lo excluded
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_topf.py -k "parse_proc_stat or parse_meminfo or parse_vmstat or parse_net_dev" -v`
Expected: FAIL with `AttributeError: module 'topf' has no attribute 'parse_proc_stat_counters'`

- [ ] **Step 3: Implement the parsers**

Add after `parse_net_unix` in `topf.py`:

```python
# --- pure core: vmstat parsing ----------------------------------------------


def parse_proc_stat_counters(content):
    """Parse the bits of /proc/stat we need into a flat dict. cpu_total is the
    sum of ALL fields on the aggregate 'cpu' line (so the dropped irq/steal/...
    time still counts toward the denominator). Absent lines -> None values."""
    out = {"cpu_user": None, "cpu_nice": None, "cpu_system": None,
           "cpu_idle": None, "cpu_iowait": None, "cpu_total": None,
           "intr": None, "ctxt": None,
           "procs_running": None, "procs_blocked": None}
    for line in content.splitlines():
        f = line.split()
        if not f:
            continue
        if f[0] == "cpu":
            nums = [int(x) for x in f[1:]]
            out["cpu_total"] = sum(nums)
            names = ["cpu_user", "cpu_nice", "cpu_system", "cpu_idle", "cpu_iowait"]
            for i, name in enumerate(names):
                out[name] = nums[i] if i < len(nums) else None
        elif f[0] == "intr":
            out["intr"] = int(f[1])
        elif f[0] == "ctxt":
            out["ctxt"] = int(f[1])
        elif f[0] == "procs_running":
            out["procs_running"] = int(f[1])
        elif f[0] == "procs_blocked":
            out["procs_blocked"] = int(f[1])
    return out


def parse_meminfo(content):
    """Parse /proc/meminfo. Return {free, buff, cache, swap_total} in BYTES
    (meminfo is kB). Missing keys -> None (swap_total -> 0 so swap-off is the
    safe default)."""
    raw = {}
    for line in content.splitlines():
        f = line.split()
        if len(f) >= 2 and f[0].endswith(":"):
            try:
                raw[f[0][:-1]] = int(f[1]) * 1024     # kB -> bytes
            except ValueError:
                pass
    return {"free": raw.get("MemFree"), "buff": raw.get("Buffers"),
            "cache": raw.get("Cached"), "swap_total": raw.get("SwapTotal", 0)}


def parse_vmstat_counters(content):
    """Parse /proc/vmstat 'name value' lines for the page/swap counters we use."""
    want = ("pgpgin", "pgpgout", "pswpin", "pswpout")
    out = {k: None for k in want}
    for line in content.splitlines():
        f = line.split()
        if len(f) >= 2 and f[0] in out:
            out[f[0]] = int(f[1])
    return out


def parse_net_dev(content):
    """Sum rx/tx bytes across all interfaces except loopback. /proc/net/dev has
    two header lines; each data line is 'iface: rxbytes ... txbytes ...' with rx
    bytes in column 0 and tx bytes in column 8 after the colon. Returns
    (rx_total, tx_total) bytes."""
    rx = tx = 0
    for line in content.splitlines()[2:]:
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        if name.strip() == "lo":
            continue
        f = rest.split()
        if len(f) < 9:
            continue
        rx += int(f[0])
        tx += int(f[8])
    return rx, tx
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_topf.py -k "parse_proc_stat or parse_meminfo or parse_vmstat or parse_net_dev" -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: vmstat /proc parsers (stat/meminfo/vmstat/net-dev)"
```

---

### Task 2: vmstat sample model + rate rows + fmt_count

**Files:**
- Modify: `topf.py` (add `VmstatSample`, `read_vmstat_sample`, `vmstat_rate_rows`, `fmt_count`; add `VmstatSample`/constants from "Shared definitions")
- Test: `tests/test_topf.py`

- [ ] **Step 1: Write the failing tests**

```python
def _vs(t, **kw):
    base = dict(procs_running=0, procs_blocked=0, cpu_user=0, cpu_nice=0,
                cpu_system=0, cpu_idle=0, cpu_iowait=0, cpu_total=0, intr=0,
                ctxt=0, pgpgin=0, pgpgout=0, pswpin=0, pswpout=0, rx=0, tx=0,
                free=0, buff=0, cache=0, swap_total=0)
    base.update(kw)
    return topf.VmstatSample(t=t, **base)


def test_vmstat_rate_rows_deltas_per_second():
    a = _vs(0.0, pgpgin=0, pgpgout=0, rx=0, tx=0, intr=0, ctxt=0,
            cpu_user=0, cpu_total=0, procs_running=2)
    b = _vs(2.0, pgpgin=2048, pgpgout=0, rx=4000, tx=8000, intr=200, ctxt=400,
            cpu_user=50, cpu_total=100, procs_running=3)
    rows = topf.vmstat_rate_rows([a, b])
    assert len(rows) == 1
    row = rows[0]
    assert row["r"] == 3                       # instantaneous (from newest)
    assert row["bi"] == 2048 * 1024 / 2.0      # pgpgin kB -> bytes/s
    assert row["ni"] == 4000 / 2.0 and row["no"] == 8000 / 2.0
    assert row["in"] == 200 / 2.0 and row["cs"] == 400 / 2.0
    assert row["us"] == 50.0                    # 50 of 100 total jiffies -> 50%


def test_vmstat_rate_rows_needs_two_samples():
    assert topf.vmstat_rate_rows([_vs(0.0)]) == []


def test_vmstat_rate_rows_none_counter_gives_none_cell():
    a = _vs(0.0, intr=None)
    b = _vs(1.0, intr=None)
    assert topf.vmstat_rate_rows([a, b])[0]["in"] is None


def test_fmt_count():
    assert topf.fmt_count(0) == "0"
    assert topf.fmt_count(950) == "950"
    assert topf.fmt_count(9100) == "9.1k"
    assert topf.fmt_count(44000) == "44k"
    assert topf.fmt_count(None) == "—"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_topf.py -k "vmstat_rate_rows or fmt_count" -v`
Expected: FAIL (`VmstatSample` / `vmstat_rate_rows` / `fmt_count` missing)

- [ ] **Step 3: Implement**

Add the `VmstatSample`, `VMSTAT_COLS`, `SWAP_KEYS`, `VMSTAT_OUTLIER_ANCHORS`, `VMSTAT_GUTTER`, and `MIN_*`/`VMSTAT_ROWS_DEFAULT` constants from "Shared definitions" to the config section. Then add a "vmstat sampling" section after the parsers:

```python
def _delta_rate(cur, prev, dt, scale=1.0):
    """(cur-prev)*scale/dt, or None if either counter is None."""
    if cur is None or prev is None:
        return None
    return (cur - prev) * scale / dt


def _d(a, b):
    """a-b, or None if either operand is None."""
    return None if a is None or b is None else a - b


def _vmstat_row(prev, cur, dt):
    """One vmstat rate-row dict (column key -> number or None) from an adjacent
    sample pair. Levels (r/b/free/buff/cache) come from the newer sample;
    byte/count columns are per-second deltas; cpu columns are a share of the
    total jiffie delta as a percentage. us folds nice into user (vmstat
    convention). Any missing counter yields None for that cell."""
    cpu_dtot = _d(cur.cpu_total, prev.cpu_total)

    def pct(cur_a, prev_a, cur_b=None, prev_b=None):
        if not cpu_dtot:                       # None or 0
            return None
        num = _d(cur_a, prev_a)
        if num is None:
            return None
        if cur_b is not None or prev_b is not None:
            extra = _d(cur_b, prev_b)
            if extra is None:
                return None
            num += extra
        return num / cpu_dtot * 100.0

    return {
        "r": cur.procs_running, "b": cur.procs_blocked,
        "free": cur.free, "buff": cur.buff, "cache": cur.cache,
        "si": _delta_rate(cur.pswpin, prev.pswpin, dt, PAGE_SIZE),
        "so": _delta_rate(cur.pswpout, prev.pswpout, dt, PAGE_SIZE),
        "bi": _delta_rate(cur.pgpgin, prev.pgpgin, dt, 1024),
        "bo": _delta_rate(cur.pgpgout, prev.pgpgout, dt, 1024),
        "ni": _delta_rate(cur.rx, prev.rx, dt),
        "no": _delta_rate(cur.tx, prev.tx, dt),
        "in": _delta_rate(cur.intr, prev.intr, dt),
        "cs": _delta_rate(cur.ctxt, prev.ctxt, dt),
        "us": pct(cur.cpu_user, prev.cpu_user, cur.cpu_nice, prev.cpu_nice),
        "sy": pct(cur.cpu_system, prev.cpu_system),
        "id": pct(cur.cpu_idle, prev.cpu_idle),
        "wa": pct(cur.cpu_iowait, prev.cpu_iowait),
    }


def vmstat_rate_rows(ring):
    """Turn a ring of VmstatSamples (ascending t) into one rate-row dict per
    adjacent pair. Pairs with non-positive dt are skipped. < 2 samples -> []."""
    rows = []
    for prev, cur in zip(ring, ring[1:]):
        dt = cur.t - prev.t
        if dt <= 0:
            continue
        rows.append(_vmstat_row(prev, cur, dt))
    return rows


def read_vmstat_sample(t):
    """I/O: read the four /proc files once and assemble a VmstatSample at
    monotonic time t. Any unreadable file degrades to None fields, never raises."""
    def safe(fn, default):
        try:
            return fn()
        except (OSError, ValueError, IndexError):
            return default
    stat = safe(lambda: parse_proc_stat_counters(_read("/proc/stat")),
                parse_proc_stat_counters(""))
    mem = safe(lambda: parse_meminfo(_read("/proc/meminfo")),
               {"free": None, "buff": None, "cache": None, "swap_total": 0})
    vm = safe(lambda: parse_vmstat_counters(_read("/proc/vmstat")),
              {"pgpgin": None, "pgpgout": None, "pswpin": None, "pswpout": None})
    rx, tx = safe(lambda: parse_net_dev(_read("/proc/net/dev")), (None, None))
    return VmstatSample(
        t=t, procs_running=stat["procs_running"], procs_blocked=stat["procs_blocked"],
        cpu_user=stat["cpu_user"], cpu_nice=stat["cpu_nice"],
        cpu_system=stat["cpu_system"], cpu_idle=stat["cpu_idle"],
        cpu_iowait=stat["cpu_iowait"], cpu_total=stat["cpu_total"],
        intr=stat["intr"], ctxt=stat["ctxt"], pgpgin=vm["pgpgin"],
        pgpgout=vm["pgpgout"], pswpin=vm["pswpin"], pswpout=vm["pswpout"],
        rx=rx, tx=tx, free=mem["free"], buff=mem["buff"], cache=mem["cache"],
        swap_total=mem["swap_total"])


def fmt_count(n):
    """Compact decimal-SI count: 950 -> '950', 9100 -> '9.1k', 44000 -> '44k'.
    None -> em dash."""
    if n is None:
        return "—"
    if n < 1000:
        return "%d" % n
    val = float(n)
    for unit in ("k", "M", "G", "T"):
        val /= 1000.0
        if val < 1000 or unit == "T":
            return "%.1f%s" % (val, unit) if val < 10 else "%d%s" % (round(val), unit)
```

Note the `us` branch handles "user+nice" while reusing `pct` for the simple columns; `_d`/`pct` both yield `None` on missing counters.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_topf.py -k "vmstat_rate_rows or fmt_count" -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: vmstat sample model, per-second rate rows, fmt_count"
```

---

### Task 3: outlier_level

**Files:**
- Modify: `topf.py` (add `outlier_level` near `_tint_level`)
- Test: `tests/test_topf.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_outlier_level_flat_window_is_zero():
    assert topf.outlier_level(5, [5, 5, 5, 5]) == 0
    assert topf.outlier_level(99, [5, 5, 5]) == 0     # zero spread -> no tint


def test_outlier_level_spike_is_high():
    window = [10, 11, 9, 10, 200]            # 200 is a gross outlier
    assert topf.outlier_level(200, window) == 3


def test_outlier_level_small_deviation_is_zero():
    window = [10, 11, 9, 10, 12]
    assert topf.outlier_level(11, window) == 0


def test_outlier_level_too_few_or_none():
    assert topf.outlier_level(5, [5, 5]) == 0        # < 3 samples
    assert topf.outlier_level(None, [1, 2, 3, 4]) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_topf.py -k outlier_level -v`
Expected: FAIL (`outlier_level` missing)

- [ ] **Step 3: Implement**

Add near `_tint_level` in `topf.py`:

```python
def outlier_level(value, window_values):
    """How much `value` deviates from its column's recent distribution, as a
    tint level 0..3 (indexing TINT_SGR). Robust: deviation measured in units of
    1.4826*MAD (a normal-consistent sigma estimate) against the median. Returns
    0 for None, < 3 samples, or a zero-spread window (so steady columns never
    tint). Levels come from VMSTAT_OUTLIER_ANCHORS."""
    if value is None:
        return 0
    vals = [v for v in window_values if v is not None]
    if len(vals) < 3:
        return 0
    s = sorted(vals)
    med = s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2.0
    devs = sorted(abs(v - med) for v in vals)
    mad = devs[len(devs) // 2] if len(devs) % 2 else \
        (devs[len(devs) // 2 - 1] + devs[len(devs) // 2]) / 2.0
    sigma = 1.4826 * mad
    if sigma <= 0:
        return 0
    z = abs(value - med) / sigma
    return min(3, sum(1 for a in VMSTAT_OUTLIER_ANCHORS if z >= a))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_topf.py -k outlier_level -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: robust (MAD) outlier level for vmstat cell coloring"
```

---

### Task 4: format_vmstat_pane

**Files:**
- Modify: `topf.py` (add `format_vmstat_pane` + `_fmt_vmstat_cell`)
- Test: `tests/test_topf.py`

- [ ] **Step 1: Write the failing tests**

```python
def _rate_row(**kw):
    row = {k: 0 for k, _h, _ki in topf.VMSTAT_COLS}
    row.update(kw)
    return row


def test_format_vmstat_pane_header_and_swap_off():
    rows = [_rate_row(free=3 * 1024**3, bi=0, ni=1024**2)]
    rows[0]["in"] = 9100                 # "in" is an ordinary dict key here
    lines = topf.format_vmstat_pane(rows, swap_on=False, width=200, height=4,
                                    color=False)
    header = lines[0]
    assert header.startswith(topf.VMSTAT_GUTTER)
    assert " si " not in header and " so " not in header   # swap off -> dropped
    assert " ni " in header and " no " in header           # network present
    assert " us " in header and " id " in header


def test_format_vmstat_pane_swap_on_includes_si_so():
    lines = topf.format_vmstat_pane([_rate_row()], swap_on=True, width=200,
                                    height=3, color=False)
    assert " si " in lines[0] and " so " in lines[0]


def test_format_vmstat_pane_uses_human_units():
    rows = [_rate_row(free=2 * 1024**3, ni=4 * 1024**2)]
    lines = topf.format_vmstat_pane(rows, swap_on=False, width=200, height=3,
                                    color=False)
    body = lines[-1]
    assert "2.0G" in body and "4.0M" in body


def test_format_vmstat_pane_dashes_when_empty():
    lines = topf.format_vmstat_pane([], swap_on=False, width=200, height=3,
                                    color=False)
    assert lines and lines[0].startswith(topf.VMSTAT_GUTTER)   # header still drawn
    assert len(lines) == 1                                     # no data rows
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_topf.py -k format_vmstat_pane -v`
Expected: FAIL (`format_vmstat_pane` missing)

- [ ] **Step 3: Implement**

Add a "vmstat rendering" section in `topf.py`:

```python
def _fmt_vmstat_cell(value, kind):
    if value is None:
        return "—"
    if kind == "int":
        return "%d" % value
    if kind in ("bytes", "bps"):
        return fmt_bytes(value)
    if kind == "count":
        return fmt_count(value)
    if kind == "pct":
        return "%d" % round(value)
    return str(value)


def format_vmstat_pane(rate_rows, swap_on, width, height, color):
    """Render the pinned vmstat pane: a header row of column names plus up to
    height-1 data rows (oldest..newest, top..bottom), columns right-aligned to
    their content, each data cell tinted by outlier_level against its column's
    values across the shown rows. swap_on=False drops the si/so columns. With no
    data rows, only the header is returned (so the layout is stable)."""
    cols = [(k, h, ki) for (k, h, ki) in VMSTAT_COLS
            if swap_on or k not in SWAP_KEYS]
    shown = rate_rows[-(height - 1):] if height > 1 else []

    # per-column formatted cells + width
    formatted = {k: [_fmt_vmstat_cell(r.get(k), ki) for r in shown]
                 for (k, _h, ki) in cols}
    colw = {k: max(len(h), max((len(c) for c in formatted[k]), default=0))
            for (k, h, _ki) in cols}

    gutter = VMSTAT_GUTTER
    pad = " " * len(gutter)

    def join_cells(cell_strs):
        return "  ".join(s.rjust(colw[k]) for (k, _h, _ki), s in
                         zip(cols, cell_strs))

    lines = [gutter + "  " + join_cells([h for (_k, h, _ki) in cols])]

    # column value windows for outlier coloring
    windows = {k: [r.get(k) for r in shown] for (k, _h, _ki) in cols}
    for ri, r in enumerate(shown):
        cells = []
        for (k, _h, _ki) in cols:
            cell = formatted[k][ri]
            lpad = " " * (colw[k] - len(cell))       # right-align padding
            if color:
                lvl = outlier_level(r.get(k), windows[k])
                if lvl:
                    cell = "\x1b[%sm%s\x1b[0m" % (TINT_SGR[lvl], cell)
            cells.append(lpad + cell)                # pad OUTSIDE the SGR wrap
        lines.append(pad + "  " + "  ".join(cells))
    return lines
```

The padding is applied *outside* the SGR wrap so right-alignment is preserved (the escape bytes don't count toward visible width) while only the digits are tinted.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_topf.py -k format_vmstat_pane -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: format_vmstat_pane (human units, aligned cols, outlier tint)"
```

---

### Task 5: identity helpers + collapse returns collapsible (honoring expanded)

**Files:**
- Modify: `topf.py` (add `proc_id`, `group_id`, `ROOT_ID`, `Row`; change `collapse` signature/return; update its caller in `build_frame`)
- Test: `tests/test_topf.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_proc_and_group_id():
    p = _rproc(7, starttime=3, comm="clang")
    p.exe = "/usr/bin/clang"
    assert topf.proc_id(p) == ("p", 7, 3)
    assert topf.group_id(topf.ROOT_ID, "clang", "/usr/bin/clang") == \
        ("g", topf.ROOT_ID, "clang", "/usr/bin/clang")


def _kept(p):
    p.kept = True
    return p


def test_collapse_returns_collapsible_and_suppresses():
    root = _kept(_rproc(1, ppid=0, comm="root"))
    root.interesting = True
    kids = {1: root}
    for i in range(2, 8):                       # 6 noise children > threshold 3
        c = _kept(_rproc(i, ppid=1, comm="noise"))
        kids[i] = c
    topf.build_tree(kids)
    suppressed, collapsible = topf.collapse(kids, threshold=3)
    assert topf.proc_id(root) in collapsible
    assert len(suppressed) == 6


def test_collapse_expanded_node_not_suppressed_but_still_collapsible():
    root = _kept(_rproc(1, ppid=0, comm="root"))
    root.interesting = True
    kids = {1: root}
    for i in range(2, 8):
        kids[i] = _kept(_rproc(i, ppid=1, comm="noise"))
    topf.build_tree(kids)
    suppressed, collapsible = topf.collapse(
        kids, threshold=3, expanded={topf.proc_id(root)})
    assert topf.proc_id(root) in collapsible    # still a candidate
    assert suppressed == set()                  # but nothing hidden
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_topf.py -k "proc_and_group_id or collapse_returns or collapse_expanded" -v`
Expected: FAIL (`proc_id` missing / `collapse` returns a set, not a tuple)

- [ ] **Step 3: Implement**

Add `Row`, `ROOT_ID` to config (from "Shared definitions") and identity helpers near `_descendants`:

```python
def proc_id(proc):
    """Stable identity for a process row: (pid, starttime) survives re-sorting;
    starttime distinguishes a reused pid."""
    return ("p", proc.pid, proc.starttime)


def group_id(parent_id, comm, exe):
    """Stable identity for a merged group row, qualified by its parent so the
    same (comm, exe) under different parents are distinct."""
    return ("g", parent_id, comm, exe)
```

Replace `collapse` with the version that computes candidacy and honors `expanded`:

```python
def collapse(procs, threshold=COLLAPSE_THRESHOLD, expanded=frozenset()):
    """For each kept node whose non-interesting kept descendants exceed
    threshold, record it as collapsible. Unless its id is in `expanded`, also
    mark it .collapsed with a histogram note and suppress those descendants.
    Returns (suppressed_pids, collapsible_ids)."""
    suppressed = set()
    collapsible = set()
    for p in procs.values():
        if not p.kept:
            continue
        kept_desc = [d for d in _descendants(p) if d.kept]
        if len(kept_desc) <= threshold:
            continue
        hide = [d for d in kept_desc if not d.interesting and d.pid not in suppressed]
        if len(hide) <= threshold:
            continue
        collapsible.add(proc_id(p))
        if proc_id(p) in expanded:
            continue                    # user forced open: reveal, don't suppress
        p.collapsed = True
        suppressed.update(d.pid for d in hide)
        hist = Counter(d.comm for d in hide)
        top = ", ".join("%s×%d" % (c, n)
                        for c, n in hist.most_common(REPR_COMMS))
        extra = len(hist) - REPR_COMMS
        if extra > 0:
            top += ", …"
        p.collapse_note = "… (+%d descendants: %s)" % (len(hide), top)
    return suppressed, collapsible
```

Update the caller in `build_frame` (line ~1234) from:

```python
    suppressed = collapse(cur, threshold=args.threshold)
```

to:

```python
    suppressed, collapsible = collapse(cur, threshold=args.threshold)
```

and thread `collapsible` into the `render(...)` call added in Task 6 (for now `render` ignores it; the variable is unused until then — acceptable between commits, but to avoid a lint complaint pass it through immediately if Task 6 lands in the same session).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_topf.py -k "proc_and_group_id or collapse_returns or collapse_expanded" -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the full suite (collapse caller changed)**

Run: `pytest tests/test_topf.py -q`
Expected: PASS (all prior tests still green; `test_render_once_smoke` exercises the new `collapse` return via `build_frame`)

- [ ] **Step 6: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: stable row identities; collapse honors expanded + reports collapsible"
```

---

### Task 6: build_rows + render wrapper + prepare_frame extraction

**Files:**
- Modify: `topf.py` (add `build_rows`; rewrite `render` as a wrapper; extract `prepare_frame`; update `build_frame`)
- Test: `tests/test_topf.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_build_rows_proc_is_selectable_detail_is_not():
    p = _rproc(20, ppid=0, comm="hot", windows=[5.0, 0, 0])
    procs = {20: p}
    topf.build_tree(procs)
    p.kept = True
    rows = topf.build_rows([p], set(), sysinfo=None)
    heads = [r for r in rows if r.selectable]
    assert len(heads) == 1
    assert heads[0].item_id == topf.proc_id(p)
    assert heads[0].selectable and not heads[0].expandable


def test_build_rows_group_is_expandable_with_group_id():
    members = {i: _rproc(i, ppid=1, comm="clang") for i in range(10, 14)}
    for m in members.values():
        m.kept = True
        m.exe = "/usr/bin/clang"
    root = _rproc(1, ppid=0, comm="root")
    root.kept = True
    procs = {1: root, **members}
    topf.build_tree(procs)
    rows = topf.build_rows([root], set(), dedup_min=3)
    groups = [r for r in rows if r.expandable and r.item_id[0] == "g"]
    assert len(groups) == 1
    gid = topf.group_id(topf.proc_id(root), "clang", "/usr/bin/clang")
    assert groups[0].item_id == gid


def test_build_rows_expanded_group_shows_members():
    members = {i: _rproc(i, ppid=1, comm="clang") for i in range(10, 14)}
    for m in members.values():
        m.kept = True
        m.exe = "/usr/bin/clang"
    root = _rproc(1, ppid=0, comm="root")
    root.kept = True
    procs = {1: root, **members}
    topf.build_tree(procs)
    gid = topf.group_id(topf.proc_id(root), "clang", "/usr/bin/clang")
    rows = topf.build_rows([root], set(), dedup_min=3, expanded={gid})
    member_ids = {topf.proc_id(m) for m in members.values()}
    selectable_ids = {r.item_id for r in rows if r.selectable}
    assert member_ids <= selectable_ids        # all 4 members now individual rows
    assert gid in selectable_ids               # group header still present (re-collapse target)


def test_render_still_returns_strings():
    cold = _rproc(10, ppid=0, comm="cold", windows=[0.1, 0, 0])
    hot = _rproc(20, ppid=0, comm="hot", windows=[5.0, 0, 0])
    procs = {10: cold, 20: hot}
    topf.build_tree(procs)
    for p in procs.values():
        p.kept = True
    key = lambda item: topf.subtree_window_cpu(
        item.members[0] if isinstance(item, topf.Group) else item, 0)
    lines = topf.render([cold, hot], set(), top_sort_key=key)
    assert all(isinstance(ln, str) for ln in lines)
    assert lines[0].endswith("hot")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_topf.py -k "build_rows or render_still_returns" -v`
Expected: FAIL (`build_rows` missing)

- [ ] **Step 3: Implement build_rows and rewrite render as a wrapper**

Replace the body of `render` (lines ~1044-1088) with `build_rows` + a thin `render`:

```python
def build_rows(roots, suppressed, width=CMD_WIDTH, color=None, sysinfo=None,
               dedup_min=None, never_merge=frozenset(), top_sort_key=None,
               show_avg=False, expanded=frozenset(), collapsible=frozenset()):
    """Build the tree as Row records (text, item_id, expandable, selectable).
    Head rows (Proc/Group) are selectable; detail/collapse-note rows are
    continuation lines. A Group whose id is in `expanded` renders a header row
    followed by its members individually (so you can re-collapse it); otherwise
    it renders the merged ×N line and recurses over the union of children. A
    Proc head is expandable iff its id is in `collapsible`."""
    if color is None:
        color = False
    rows = []

    def emit(text, item_id=None, expandable=False, selectable=False):
        rows.append(Row(text, item_id, expandable, selectable))

    def walk_items(items, prefix, parent_id):
        for i, item in enumerate(items):
            is_last = i == len(items) - 1
            connector = "" if prefix == "" and is_last else (
                "└─ " if is_last else "├─ ")
            child_prefix = prefix + ("   " if is_last else "│  ")
            if isinstance(item, Group):
                gid = group_id(parent_id, item.members[0].comm,
                               item.members[0].exe)
                head = "%s%s×%d %s" % (prefix, connector, len(item.members),
                                       _group_label(item.members, width))
                emit(head, item_id=gid, expandable=True, selectable=True)
                detail = _group_detail(item.members, color, sysinfo, show_avg)
                if detail is not None:
                    emit(child_prefix + detail)
                if gid in expanded:
                    walk_items(list(item.members), child_prefix, gid)
                else:
                    kids = [c for m in item.members
                            for c in _visible_children(m, suppressed)]
                    walk_items(group_siblings(kids, dedup_min, never_merge),
                               child_prefix, gid)
            else:
                pid_id = proc_id(item)
                head = "%s%s%d %s" % (prefix, connector, item.pid,
                                      compress_cmdline(item.cmdline, width))
                emit(head, item_id=pid_id, expandable=pid_id in collapsible,
                     selectable=True)
                detail = _detail(item, color, sysinfo, show_avg)
                if detail is not None:
                    emit(child_prefix + detail)
                kids = _visible_children(item, suppressed)
                walk_items(group_siblings(kids, dedup_min, never_merge),
                           child_prefix, pid_id)
                if item.collapsed and item.collapse_note:
                    emit(child_prefix + item.collapse_note)

    top_items = group_siblings(list(roots), dedup_min, never_merge)
    if top_sort_key is not None:
        top_items.sort(key=top_sort_key, reverse=True)
    walk_items(top_items, "", ROOT_ID)
    return rows


def render(roots, suppressed, width=CMD_WIDTH, color=None, sysinfo=None,
           dedup_min=None, never_merge=frozenset(), top_sort_key=None,
           show_avg=False, expanded=frozenset(), collapsible=frozenset()):
    """Backward-compatible string view of build_rows (used by the once/piped
    path and by tests)."""
    return [r.text for r in build_rows(
        roots, suppressed, width=width, color=color, sysinfo=sysinfo,
        dedup_min=dedup_min, never_merge=never_merge, top_sort_key=top_sort_key,
        show_avg=show_avg, expanded=expanded, collapsible=collapsible)]
```

- [ ] **Step 4: Extract prepare_frame from build_frame**

Add, before `build_frame`:

```python
def prepare_frame(cur, args, sysinfo, expanded=frozenset()):
    """Shared pipeline: build the tree, select interesting/heavy nodes, collapse
    (honoring expanded), probe the printed nodes. Returns
    (visible_roots, suppressed, collapsible)."""
    roots = build_tree(cur)
    select(cur, DEFAULT_MATCHERS, sysinfo.page_size, args.promote_level,
           args.rss_needs_cpu)
    suppressed, collapsible = collapse(cur, threshold=args.threshold,
                                       expanded=expanded)
    visible_roots = [r for r in roots if r.kept]
    printed = [n for n in collect_printed(visible_roots, suppressed) if n.kept]
    if args.no_cache:
        cache = Cache(os.devnull, boot_id="", now=time.time())
    else:
        cache = Cache(cache_path(), boot_id=read_boot_id(), now=time.time())
    probe(printed, cache)
    if not args.no_cache:
        cache.save(live_keys={(p.pid, p.starttime) for p in cur.values()})
    return visible_roots, suppressed, collapsible
```

Then rewrite `build_frame`'s body (the tree-building portion) to call it and pass `collapsible` to `render`:

```python
def build_frame(prev, cur, history, t_prev, t_now, args, color, sysinfo,
                sort_idx, show_avg, frozen=False):
    visible_roots, suppressed, collapsible = prepare_frame(cur, args, sysinfo)
    dedup_min = None if args.no_dedup else args.dedup_min
    key = lambda item: subtree_window_cpu(
        item.members[0] if isinstance(item, Group) else item, sort_idx)
    frame_dt = (t_now - t_prev) if prev is not None else 0.0
    hidden = sum(1 for p in cur.values() if not p.kept)
    out = [header_line(frame_dt, sysinfo, len(cur), hidden,
                       args.sample_interval, frozen)]
    if not args.no_glossary:
        out += [""] + glossary(color)
    out += [""]
    out += render(visible_roots, suppressed, width=args.width, color=color,
                  sysinfo=sysinfo, dedup_min=dedup_min, never_merge=NEVER_MERGE,
                  top_sort_key=key, show_avg=show_avg, collapsible=collapsible)
    if prev is not None and not args.no_lifecycle:
        born, died = diff_snapshots(prev, cur)
        section = format_lifecycle(born, died, _parents_map(prev, cur),
                                   sysinfo, frame_dt, color=color)
        if section:
            out += [""] + section
    return out
```

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_topf.py -k "build_rows or render_still_returns" -v`
Expected: PASS (4 tests)

Run: `pytest tests/test_topf.py -q`
Expected: PASS (whole suite green; `test_render_orders_top_level_by_window_desc_pid_tiebreak` and `test_render_once_smoke` exercise the wrapper)

- [ ] **Step 6: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: build_rows row-records (render wraps it); extract prepare_frame"
```

---

### Task 7: UIState + viewport presenter (cursor, scroll, markers)

**Files:**
- Modify: `topf.py` (add `UIState`, `selectable_ids`, `move_cursor`, `present_viewport`)
- Test: `tests/test_topf.py`

- [ ] **Step 1: Write the failing tests**

```python
def _rows(n_select):
    # n_select selectable head rows, each with a non-selectable detail line
    rows = []
    for i in range(n_select):
        rows.append(topf.Row("head%d" % i, ("p", i, 1),
                             expandable=(i % 2 == 0), selectable=True))
        rows.append(topf.Row("  detail%d" % i, ("p", i, 1), False, False))
    return rows


def test_selectable_ids_in_order():
    rows = _rows(3)
    assert topf.selectable_ids(rows) == [("p", 0, 1), ("p", 1, 1), ("p", 2, 1)]


def test_move_cursor_clamps():
    ids = [("p", i, 1) for i in range(3)]
    assert topf.move_cursor(ids, ("p", 0, 1), +1) == ("p", 1, 1)
    assert topf.move_cursor(ids, ("p", 0, 1), -1) == ("p", 0, 1)   # clamp at top
    assert topf.move_cursor(ids, ("p", 2, 1), +1) == ("p", 2, 1)   # clamp at bottom
    assert topf.move_cursor(ids, None, +1) == ("p", 0, 1)          # none -> first


def test_present_viewport_highlights_cursor_and_glyph():
    rows = _rows(2)
    ui = topf.UIState(cursor=("p", 0, 1))
    lines, cursor, top = topf.present_viewport(rows, ui, height=10, color=True)
    assert cursor == ("p", 0, 1) and top == 0
    assert "\x1b[7m" in lines[0]            # cursor row reverse-video
    assert lines[0].startswith("\x1b[7m▸ ") or "▸ " in lines[0]   # expandable glyph


def test_present_viewport_scrolls_to_keep_cursor_visible():
    rows = _rows(20)                        # 40 rows total
    ui = topf.UIState(cursor=("p", 19, 1))  # bottom selectable
    lines, cursor, top = topf.present_viewport(rows, ui, height=6, color=False)
    assert len(lines) == 6
    assert cursor == ("p", 19, 1)
    assert any("head19" in ln for ln in lines)        # cursor visible
    assert lines[0].startswith("▲")                   # "more above" marker


def test_present_viewport_bottom_marker_when_overflow_below():
    rows = _rows(20)
    ui = topf.UIState(cursor=("p", 0, 1))
    lines, cursor, top = topf.present_viewport(rows, ui, height=6, color=False)
    assert lines[-1].startswith("▼")                  # "more below" marker


def test_present_viewport_snaps_when_cursor_gone():
    rows = _rows(3)
    ui = topf.UIState(cursor=("p", 99, 1))  # not present
    lines, cursor, top = topf.present_viewport(rows, ui, height=10, color=False)
    assert cursor == ("p", 0, 1)            # snapped to first selectable
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_topf.py -k "selectable_ids or move_cursor or present_viewport" -v`
Expected: FAIL (`UIState` / `present_viewport` missing)

- [ ] **Step 3: Implement**

Add a "live UI state & viewport" section in `topf.py`:

```python
@dataclass
class UIState:
    expanded: set = field(default_factory=set)
    cursor: tuple = None
    scroll_top: int = 0
    frozen: bool = False
    sort_idx: int = 0
    vmstat_on: bool = True


def selectable_ids(rows):
    """Ordered item ids of the selectable head rows."""
    return [r.item_id for r in rows if r.selectable]


def move_cursor(ids, cursor, delta):
    """Move the cursor `delta` selectable rows, clamped to the ends. A cursor of
    None (or one no longer present) starts from the first row."""
    if not ids:
        return None
    try:
        i = ids.index(cursor)
    except ValueError:
        return ids[0]
    return ids[max(0, min(len(ids) - 1, i + delta))]


def _row_index_of(rows, item_id):
    for i, r in enumerate(rows):
        if r.selectable and r.item_id == item_id:
            return i
    return None


def present_viewport(rows, ui, height, color):
    """Slice `rows` to a `height`-row viewport around the cursor and decorate it:
    a 2-col gutter (▸ closed / ▾ open on expandable rows, else blank), reverse
    video on the cursor's row, and dim ▲/▼ 'more' markers on the first/last line
    when content extends past the viewport. The markers occupy whole lines, so
    when the content overflows the cursor is held one row inside each edge (an
    'inner band') — a marker never overwrites the cursor's row. Returns (lines,
    resolved_cursor, scroll_top). Pure: no terminal I/O."""
    sel = [i for i, r in enumerate(rows) if r.selectable]
    if not sel:
        return ([r.text for r in rows[:height]], None, 0)

    cur_idx = _row_index_of(rows, ui.cursor)
    if cur_idx is None:
        cur_idx = sel[0]
    cursor = rows[cur_idx].item_id
    n = len(rows)

    def dim(s):
        return ("\x1b[2m%s\x1b[0m" % s) if color else s

    if n <= height:                       # everything fits, no markers/scroll
        top = 0
    else:
        # reserve one line at each edge for a potential marker; keep the cursor
        # within [top+1, top+height-2] so a marker can never land on it.
        band = max(1, height - 2)
        top = max(0, min(ui.scroll_top, n - height))
        if cur_idx - top < 1:
            top = cur_idx - 1
        elif cur_idx - top > band:
            top = cur_idx - band
        top = max(0, min(top, n - height))

    window = rows[top:top + height]
    out = []
    for off, r in enumerate(window):
        idx = top + off
        gutter = ("▾ " if r.item_id in ui.expanded else "▸ ") if r.expandable \
            else "  "
        text = gutter + r.text
        if idx == cur_idx and color:
            text = "\x1b[7m" + text + "\x1b[0m"
        out.append(text)

    if top > 0:                           # content hidden above
        out[0] = dim("▲ %d more above" % top)
    if top + height < n:                  # content hidden below
        out[-1] = dim("▼ %d more below" % (n - (top + height)))
    return out, cursor, top
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_topf.py -k "selectable_ids or move_cursor or present_viewport" -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: UIState + viewport presenter (cursor, scroll, gutter, markers)"
```

---

### Task 8: split_regions + live frame composition helper

**Files:**
- Modify: `topf.py` (add `split_regions`, `lifecycle_section`, `compose_live_body`)
- Test: `tests/test_topf.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_split_regions_hidden_when_small():
    # below MIN_ROWS_FOR_VMSTAT -> pane hidden, tree gets the whole body
    region, pane, show = topf.split_regions(rows=12, cols=200, vmstat_on=True,
                                            vmstat_rows_cap=12, sample_rows=10)
    assert show is False and pane == 0 and region == 11   # rows-1 header


def test_split_regions_narrow_hides_pane():
    region, pane, show = topf.split_regions(rows=40, cols=50, vmstat_on=True,
                                            vmstat_rows_cap=12, sample_rows=10)
    assert show is False


def test_split_regions_shows_pane_when_room():
    # 40 rows: header 1, tree gets the rest minus a pane of 2 + k sample rows
    region, pane, show = topf.split_regions(rows=40, cols=200, vmstat_on=True,
                                            vmstat_rows_cap=12, sample_rows=10)
    assert show is True
    assert pane == 2 + 10                 # separator + header + 10 samples
    assert region == (40 - 1) - pane


def test_split_regions_caps_pane_to_keep_tree():
    # tiny body: ensure tree keeps >= MIN_TREE_ROWS and pane >= MIN samples or hides
    region, pane, show = topf.split_regions(rows=18, cols=200, vmstat_on=True,
                                            vmstat_rows_cap=12, sample_rows=10)
    assert region >= topf.MIN_TREE_ROWS
    if show:
        assert pane >= 2 + topf.MIN_VMSTAT_SAMPLE_ROWS


def test_split_regions_off_when_toggled():
    region, pane, show = topf.split_regions(rows=40, cols=200, vmstat_on=False,
                                            vmstat_rows_cap=12, sample_rows=10)
    assert show is False and region == 39
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_topf.py -k split_regions -v`
Expected: FAIL (`split_regions` missing)

- [ ] **Step 3: Implement**

```python
def split_regions(rows, cols, vmstat_on, vmstat_rows_cap, sample_rows):
    """Divide the screen height into (tree_region, vmstat_pane, show_pane).
    One row is the pinned header. The pane (separator + header + k sample rows)
    is shown only when the terminal clears the size thresholds, the user hasn't
    toggled it off, and there is room to keep at least MIN_TREE_ROWS for the
    tree and MIN_VMSTAT_SAMPLE_ROWS samples. sample_rows is how many rate rows
    are actually available so a cold start doesn't reserve empty space."""
    body = rows - 1
    if (not vmstat_on or rows < MIN_ROWS_FOR_VMSTAT
            or cols < MIN_COLS_FOR_VMSTAT):
        return body, 0, False
    k = min(vmstat_rows_cap, max(sample_rows, MIN_VMSTAT_SAMPLE_ROWS))
    k = min(k, body - MIN_TREE_ROWS - 2)        # 2 = separator + pane header
    if k < MIN_VMSTAT_SAMPLE_ROWS:
        return body, 0, False
    pane = 2 + k
    return body - pane, pane, True


def lifecycle_section(prev, cur, sysinfo, frame_dt, color):
    """The born/died lines (empty list when nothing changed). Shared by the
    once frame and the live loop."""
    if prev is None:
        return []
    born, died = diff_snapshots(prev, cur)
    return format_lifecycle(born, died, _parents_map(prev, cur), sysinfo,
                            frame_dt, color=color)
```

(`build_frame` may now call `lifecycle_section` instead of its inline copy — optional tidy; not required for tests.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_topf.py -k split_regions -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: split_regions layout math + shared lifecycle_section"
```

---

### Task 9: rewrite run_live (regions, vmstat ring, keys, navigation)

**Files:**
- Modify: `topf.py` (`run_live`, `header_line`; add `_read_key`)
- Test: `tests/test_topf.py` (key-decoder unit test + run_live smoke is manual)

- [ ] **Step 1: Write the failing test (key decoder)**

```python
def test_read_key_decodes_arrows_and_plain():
    import io
    # plain char
    assert topf._read_key(io.StringIO("q")) == "q"
    # up / down arrow escape sequences
    assert topf._read_key(io.StringIO("\x1b[A")) == "up"
    assert topf._read_key(io.StringIO("\x1b[B")) == "down"
    assert topf._read_key(io.StringIO("\x1b[5~")) == "pgup"
    assert topf._read_key(io.StringIO("\x1b[6~")) == "pgdn"
    # a lone ESC (no following bytes) is returned as escape, not a hang
    assert topf._read_key(io.StringIO("\x1b")) == "esc"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_topf.py -k read_key_decodes -v`
Expected: FAIL (`_read_key` missing)

- [ ] **Step 3: Implement `_read_key`**

`_read_key` reads from an object with `.read(n)`; for the real loop it's a small wrapper over the tty fd that only reads more bytes when they're already pending (so a lone ESC doesn't block). For testability it takes a stream:

```python
def _read_key(stream, pending=lambda: True):
    """Decode one logical key from `stream`. Returns a plain char, or one of
    'up'/'down'/'pgup'/'pgdn'/'home'/'end'/'esc'/'enter'. `pending()` says
    whether more bytes are immediately available (used so a lone ESC returns
    'esc' instead of blocking on a CSI read)."""
    ch = stream.read(1)
    if ch == "\r" or ch == "\n":
        return "enter"
    if ch != "\x1b":
        return ch
    if not pending():
        return "esc"
    if stream.read(1) != "[":
        return "esc"
    seq = ""
    while True:
        c = stream.read(1)
        if not c:
            break
        seq += c
        if c.isalpha() or c == "~":
            break
    return {"A": "up", "B": "down", "H": "home", "F": "end",
            "5~": "pgup", "6~": "pgdn", "1~": "home", "4~": "end"}.get(seq, "esc")
```

- [ ] **Step 4: Run the decoder test**

Run: `pytest tests/test_topf.py -k read_key_decodes -v`
Expected: PASS

- [ ] **Step 5: Rewrite `run_live` and `header_line`**

Replace `header_line` to advertise the new keys:

```python
def header_line(frame_dt, sysinfo, nprocs, hidden, interval, frozen=False):
    """Top-style status line."""
    state = "  FROZEN" if frozen else ""
    return ("topf — %.2gs, %d cores, %d procs (%d hidden)   every %.2gs   "
            "[q]uit [f]reeze [w]in [v]mstat [↑↓]nav [␣]expand%s"
            % (frame_dt, sysinfo.cores, nprocs, hidden, interval, state))
```

Replace `run_live` with the region-composing, navigable version:

```python
def run_live(args):
    """Full-screen live loop with a pinned header, a scrolling/cursored process
    tree, and a pinned vmstat pane. Keys: q/Ctrl-C quit, f freeze, w sort window,
    v toggle vmstat, ↑/k ↓/j move cursor, PgUp/PgDn page, g/G top/bottom,
    Space/Enter expand-collapse the selected group/subtree. Terminal state is
    always restored."""
    fd = sys.stdin.fileno()
    out = sys.stdout
    old_attr = termios.tcgetattr(fd)
    windows = args.windows
    longest = max(windows)
    history = {}
    vmring = []
    ui = UIState(vmstat_on=not args.no_vmstat)
    sysinfo_cores = cores_count()
    prev, t_prev = None, None
    cur, rows, sysinfo = {}, [], None

    def repaint():
        """Re-present the current `rows` + vmstat ring without resampling (used
        after navigation/expand/freeze so input feels instant)."""
        cols, term_rows = os.get_terminal_size()
        rate_rows = vmstat_rate_rows(vmring)
        region_h, pane_h, show = split_regions(
            term_rows, cols, ui.vmstat_on, args.vmstat_rows, len(rate_rows))
        body, ui.cursor, ui.scroll_top = present_viewport(
            rows, ui, region_h, color=not args.no_color)
        if show:
            body += [""] * (region_h - len(body))   # pad so the pane pins to bottom
        frame = [header_line((t_prev and (time.monotonic() - t_prev)) or 0.0,
                             sysinfo, len(cur),
                             sum(1 for p in cur.values() if not p.kept),
                             args.sample_interval, ui.frozen)]
        frame += body
        if show:
            swap_on = any(s.swap_total for s in vmring if s.swap_total)
            frame.append("─" * cols)
            frame += format_vmstat_pane(rate_rows, swap_on, cols, pane_h - 1,
                                        color=not args.no_color)
        _draw_frame(out, [visible_truncate(ln, cols) for ln in frame[:term_rows]])

    def sample_and_build():
        nonlocal prev, t_prev, cur, rows, sysinfo
        cur = scan()
        t_now = time.monotonic()
        update_history(history, cur, t_now, longest)
        compute_windows(cur, history, windows, CLK_TCK)
        vmring.append(read_vmstat_sample(t_now))
        if len(vmring) > args.vmstat_rows + 1:
            del vmring[0]
        sysinfo = SysInfo(clk_tck=CLK_TCK, page_size=PAGE_SIZE,
                          uptime=read_uptime(), cores=sysinfo_cores)
        visible_roots, suppressed, collapsible = prepare_frame(
            cur, args, sysinfo, expanded=ui.expanded)
        dedup_min = None if args.no_dedup else args.dedup_min
        key = lambda item: subtree_window_cpu(
            item.members[0] if isinstance(item, Group) else item, ui.sort_idx)
        rows = build_rows(visible_roots, suppressed, width=args.width,
                          color=not args.no_color, sysinfo=sysinfo,
                          dedup_min=dedup_min, never_merge=NEVER_MERGE,
                          top_sort_key=key, expanded=ui.expanded,
                          collapsible=collapsible)
        prev, t_prev = cur, t_now

    try:
        tty.setcbreak(fd)
        out.write("\x1b[?1049h")
        out.flush()
        sample_and_build()
        repaint()
        while True:
            r, _w, _e = _select.select([fd], [], [], args.sample_interval)
            if r:
                key = _read_key(sys.stdin,
                                pending=lambda: bool(_select.select([fd], [], [], 0)[0]))
                ids = selectable_ids(rows)
                if key in ("q", "\x03"):
                    break
                elif key == "f":
                    ui.frozen = not ui.frozen
                elif key == "w":
                    ui.sort_idx = (ui.sort_idx + 1) % len(windows)
                elif key == "v":
                    ui.vmstat_on = not ui.vmstat_on
                elif key in ("up", "k"):
                    ui.cursor = move_cursor(ids, ui.cursor, -1)
                elif key in ("down", "j"):
                    ui.cursor = move_cursor(ids, ui.cursor, +1)
                elif key == "pgup":
                    ui.cursor = move_cursor(ids, ui.cursor, -10)
                elif key == "pgdn":
                    ui.cursor = move_cursor(ids, ui.cursor, +10)
                elif key in ("g", "home"):
                    ui.cursor = ids[0] if ids else None
                elif key in ("G", "end"):
                    ui.cursor = ids[-1] if ids else None
                elif key in (" ", "enter"):
                    if ui.cursor is not None:
                        ui.expanded ^= {ui.cursor}      # toggle membership
                        sample_and_build()              # tree shape changed
                repaint()                               # instant feedback
            elif not ui.frozen:
                sample_and_build()
                repaint()
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\x1b[?1049l")
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
        if not args.no_cache:
            Cache(cache_path(), boot_id=read_boot_id(),
                  now=time.time()).save(live_keys=set())
```

Notes for the implementer:
- `ui.expanded ^= {ui.cursor}` toggles the id; after an expand/collapse the tree shape changes so we rebuild rows immediately, then `repaint()`.
- Navigation keys do NOT resample — they only move the cursor and `repaint()`, so they're instant and work while frozen.
- The glossary is intentionally dropped from the live composition (the header carries the hints and vertical space is scarce); `--no-glossary` still affects the once path.

- [ ] **Step 6: Manual smoke verification**

Run: `python3 topf.py` in a terminal ≥ 24 rows tall for ~10 seconds.
Verify: header shows the new key hints; the vmstat pane is pinned at the bottom with aligned columns and human units; `↓`/`j` moves a reverse-video cursor; landing on a `×N` group and pressing Space expands it to individual members (glyph flips `▸`→`▾`); Space again collapses; `v` hides/shows the pane; `f` freezes (header shows FROZEN) yet the cursor still moves; `q` quits and the terminal is restored cleanly.

Run: `pytest tests/test_topf.py -q`
Expected: PASS (whole suite; `run_live` itself is covered by the manual smoke).

- [ ] **Step 7: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: live regions, vmstat ring, cursor/scroll/expand navigation"
```

---

### Task 10: CLI flags + once-mode vmstat row

**Files:**
- Modify: `topf.py` (`main` argparse; `render_once`/`build_frame` optional vmstat row; `_once_defaults`)
- Test: `tests/test_topf.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_main_parses_vmstat_flags():
    ns = topf._parse_args(["--no-vmstat", "--vmstat-rows", "5"])
    assert ns.no_vmstat is True and ns.vmstat_rows == 5
    ns2 = topf._parse_args([])
    assert ns2.no_vmstat is False and ns2.vmstat_rows == topf.VMSTAT_ROWS_DEFAULT


def test_once_defaults_have_vmstat_fields():
    d = topf._once_defaults()
    assert hasattr(d, "no_vmstat") and hasattr(d, "vmstat_rows")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_topf.py -k "vmstat_flags or once_defaults_have" -v`
Expected: FAIL (`_parse_args` missing / `_once_defaults` lacks fields)

- [ ] **Step 3: Implement**

Extract the parser into `_parse_args` and add the flags. In `main`, replace the inline `ap.parse_args` with a call to `_parse_args(argv)`:

```python
def _parse_args(argv):
    ap = argparse.ArgumentParser(prog="topf",
                                 description="Focused live process viewer.")
    # ... (all existing arguments unchanged) ...
    ap.add_argument("--no-vmstat", action="store_true",
                    help="start with the bottom vmstat pane hidden (toggle: v)")
    ap.add_argument("--vmstat-rows", type=int, default=VMSTAT_ROWS_DEFAULT,
                    help="max vmstat sample rows in the pane (default %d)"
                         % VMSTAT_ROWS_DEFAULT)
    return ap.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    use_once = args.once or not sys.stdout.isatty()
    if use_once:
        lines = render_once(args.sample_interval, args)
        print("\n".join(lines))
        return
    run_live(args)
```

Add the two new fields to `_once_defaults`:

```python
        windows=DEFAULT_WINDOWS, promote_level=PROMOTE_LEVEL,
        rss_needs_cpu=True, no_vmstat=False, vmstat_rows=VMSTAT_ROWS_DEFAULT)
```

(The once/piped path does not render a vmstat pane — it has no ring/trend — so `no_vmstat`/`vmstat_rows` are carried only so the same `args` works everywhere. The `--vmstat` one-shot single-row option from the spec is **dropped from this plan** as YAGNI; the pane is a live-only feature. If wanted later it is a clean addition.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_topf.py -k "vmstat_flags or once_defaults_have" -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the full suite + a piped smoke**

Run: `pytest tests/test_topf.py -q`
Expected: PASS (all)

Run: `python3 topf.py --once | head -5`
Expected: a plain frame starting with `topf —` (no pane, no ANSI), proving the once path is unaffected.

- [ ] **Step 6: Commit**

```bash
git add topf.py tests/test_topf.py
git commit -m "feat: --no-vmstat / --vmstat-rows flags; _parse_args extraction"
```

---

## Self-review notes (addressed)

- **Spec coverage:** pinned-pane layout (Tasks 8–9), reimplemented-from-/proc sources (Tasks 1–2), human units (Tasks 2,4), outlier color (Tasks 3–4), proper widths (Task 4), drop si/so when swap off (Task 4), network ni/no (Tasks 1–2,4), four CPU cols us/sy/id/wa with "rest" dropped (Task 2), cursor+scroll+markers (Task 7), expand/collapse groups & collapsed subtrees via stable identity (Tasks 5–7,9), `f`=freeze / `v`=vmstat / Space=expand keys (Task 9), flags (Task 10), edge cases — missing /proc fields → `—` (Tasks 1–2,4), tiny terminal hides pane (Task 8), cursor snap on id loss (Task 7), navigation works while frozen (Task 9).
- **Deliberate spec deviation:** the spec's optional one-shot `--vmstat` single row is dropped as YAGNI (noted in Task 10). Flag at review if you want it kept.
- **Type/name consistency:** `Row(text,item_id,expandable,selectable)`, `proc_id`/`group_id`/`ROOT_ID`, `UIState`, `collapse(...)->(suppressed,collapsible)`, `build_rows`/`render` shared signature, `split_regions(rows,cols,vmstat_on,vmstat_rows_cap,sample_rows)->(region,pane,show)`, `present_viewport(rows,ui,height,color)->(lines,cursor,scroll_top)`, `vmstat_rate_rows`, `format_vmstat_pane(rate_rows,swap_on,width,height,color)` — all used consistently across tasks.
```
