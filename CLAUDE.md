# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**topf** — a `top`-like, full-screen Linux process viewer that shows only interesting/heavy subtrees, with windowed CPU, vmstat pane, disk pane, and interactive expand/collapse. **topfig** — a static HTML config editor generated from topf.py's knob constants. **psf** — a process session finder: classify, group, and summarize running processes by session type, detect venvs.

## Commands

- **Run tests:** `pytest tests/`
- **Run a single test file:** `pytest tests/test_topf.py`, `pytest tests/test_build.py`, or `pytest tests/test_psf.py`
- **Run a single test by name:** `pytest tests/test_topf.py -k "test_windowed_rate_constant_one_core"`
- **Run topf once (piped):** `topf --once` or `python topf.py --once`
- **Run topf live (interactive TUI):** `topf` or `python topf.py`
- **Run psf (snapshot):** `psf` or `python psf.py`
- **Run psf (watch mode):** `psf --watch`
- **Regenerate index.html:** `python build.py`
- **Install editable:** `pip install -e .`

## Architecture

`pyproject.toml` (hatchling) defines both `topf:main` and `psf:main` console scripts. `topf.py` and `psf.py` are single-file modules at repo root. `psf.py` imports public functions from `topf.py` (pure-core /proc parsing, formatting, tree building) but reimplements private I/O (`_read_text`, `_read_bin`) locally.

### topf.py (~2100 lines) — sections in order

| Section | Purpose |
|---|---|
| **config** | Module-level constants (knobs) — window durations, tint anchors, thresholds, matchers, breakout/disk pane settings |
| **pure core: parsing** | `parse_stat`, `clean_cmdline` — parse `/proc/PID/stat` and cmdline |
| **pure core: tree** | `build_tree` — populate `.children` from `.ppid` |
| **pure core: selection** | `is_interesting`, `select`, `is_promoted` — mark processes for output |
| **pure core: collapse** | `group_siblings`, `collapse`, `find_breakouts` — merge siblings, collapse subtrees, hot-proc breakout |
| **pure core: socket parsing** | `parse_net_tcp/udp/unix`, `format_sockets` — inode→port mapping |
| **pure core: vmstat** | Parse and delta-rate vmstat columns |
| **pure core: resource stats** | `windowed_rate`, `update_history`, `compute_windows` — ring-buffer CPU history |
| **disk pane** | `read_mounts`, `statvfs_mount`, `read_disk_sample`, `format_disk_pane` — filesystem usage |
| **cache** | `Cache` class — disk-backed socket cache |
| **I/O shell** | `scan`, `fd_socket_inodes`, `read_links`, `resolve_netmaps` — `/proc` readers |
| **probe orchestration** | `probe` — deep-probe decisions |
| **render** | `build_rows`, `render`, `format_vmstat_pane`, `format_disk_pane`, `_detail`, `_group_detail` |
| **live UI state & viewport** | `UIState`, `present_viewport`, `split_regions`, `run_live` — TUI loop |
| **CLI** | `_parse_args`, `main` — `--once` for piped, otherwise live TUI |

### psf.py (~380 lines) — sections

| Section | Purpose |
|---|---|
| **config** | Category definitions, classification rules, session leader patterns, venv markers |
| **classification** | `classify(proc)` — rule-based first-match-wins, venv override for python |
| **venv detection** | `read_environ(pid)`, `detect_venv(proc)` — VIRTUAL_ENV, CONDA_PREFIX, exe path |
| **session detection** | `find_session_leaders`, `find_sessions` — trace leaders to subtrees |
| **glue summarization** | `summarize_glue` — collapse infrastructure/system-service processes |
| **new-process clusters** | `find_new_clusters` — burst detection across snapshots |
| **render** | `render_psf` — session-first display with category badges |
| **CLI** | `_parse_args`, `main` — `--once`/`--watch` modes |

### build.py (~150 lines)

Parses topf.py's AST, replaces knob constants with sentinels, injects data as JSON into `topfig_template.html` → `index.html`. The `KNOBS` list must stay in sync with topf.py.

### tests/

- `test_topf.py` — pure-core + integration tests for topf (header, vmstat, disk pane, breakout, collapse, render, etc.)
- `test_build.py` — template round-trip, marker coverage, value extraction
- `test_psf.py` — classification, venv detection, sessions, glue, clusters
- `conftest.py` — empty; exists so `import topf` / `import build` / `import psf` resolves

## Key interactions

- **psf imports topf** for `scan`, `build_tree`, `fmt_bytes`, `compress_path`, `is_promoted`, etc. — only public functions. Private I/O (`_read`, `PAGE_SIZE`, `CLK_TCK`) is reimplemented locally in psf.py.
- **topf's `Proc` dataclass** is shared — psf extends it at runtime by reading `/proc/PID/environ` and `/proc/PID/exe` for all processes (not just printed ones like topf does).
- **`prepare_frame`** returns 4-tuple: `(visible_roots, suppressed, collapsible, breakout_map)` — breakout_map drives `build_rows` hot-proc breakout.

## Conventions

- `index.html` is a **generated artifact** — never edit directly.
- pytest is a dev dependency: `pip install -e ".[dev]"` or `pip install pytest`.
- Git remote `origin` is `hila-shemer/psf.git`; never push to any Majestic-Labs remote.
- Some tests read real `/proc` — pass on Linux only (macOS/WSL1 will fail).
