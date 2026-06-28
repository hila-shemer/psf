# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**topf** — a `top`-like, full-screen Linux process viewer that shows only interesting/heavy subtrees, with windowed CPU, vmstat pane, disk pane, and interactive expand/collapse. **psf** — a process session finder: classify, group, and summarize running processes by session type, detect venvs.

## Commands

- **Run tests:** `pytest tests/`
- **Run a single test file:** `pytest tests/test_topf.py` or `pytest tests/test_psf.py`
- **Run a single test by name:** `pytest tests/test_topf.py -k "test_windowed_rate_constant_one_core"`
- **Run topf once (piped):** `topf --once` or `python topf.py --once`
- **Run topf live (interactive TUI):** `topf` or `python topf.py`
- **Run psf (snapshot):** `psf` or `python psf.py` (default `--group session`)
- **Run psf, other axes:** `psf --group consolidated` (fold alike sessions; `--pool-global` to flatten), `psf --group dir` (by directory)
- **Run psf path query:** `psf --path ~/proj` (every process touching a subtree)
- **Install editable:** `pip install -e .`

## Architecture

`pyproject.toml` (hatchling) defines both `topf:main` and `psf:main` console scripts. `topf.py` and `psf.py` are single-file modules at repo root. **`psf.py` is self-contained — it does NOT import topf** (it has its own /proc parsing, tree building, formatting, and the model→view→render renderer). topf.py is unrelated to psf and untouched by psf work.

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

### psf.py (~1900 lines) — sections in order

| Section | Purpose |
|---|---|
| **config** | `CATEGORIES`/`CLASSIFICATION_RULES`, `DEFAULT_MATCHERS`, `SESSION_LEADERS`, venv markers, `COLOR_FAMILIES` and the OKLab colour knobs |
| **pure core: parsing / tree / selection** | `parse_stat`, `clean_cmdline`, `build_tree`, `is_interesting`, `select`, `collapse` |
| **grouping** | `group_siblings(key_fn=…)`, `merge_key` (category-gated), `is_session_leader`, `find_session_roots`, `collect_session_printed`, `session_signature`, `consolidate_sessions` |
| **classification + venv** | `classify(proc)` (venv override for python), `read_environ`, `detect_venv` |
| **sockets / resource stats / path snapshot** | socket parsing, `windowed`/cpu rates, `proc_path_hits`/`collect_path_hits` for `--path` |
| **identity colour** | OKLab `_hex_to_oklab`/`family_rgb`, `detect_anchor`, `ColorMap`/`build_colormap`, `make_colormap`, `_truecolor` — cartographic, protanomaly-safe |
| **view → render** | `ViewNode`, `build_view(axis=tree/session/consolidated/dir/flat)`, `emit_lines` (the only place connectors + ANSI are produced), `render()` (thin tree-axis wrapper) |
| **CLI** | `main` — `--group`, `--pool-global`, `--verbose`/`--expand-all`, `--color-anchor`, `--path`; snapshot only (no live TUI / `--watch`) |

Architecture is **model → view → render**: the model (scan/select/collapse) and `build_view` (pure, no IO, returns a `ViewNode` tree annotated with colour-keys) are unit-testable as data; `emit_lines` is the dumb renderer. See `docs/superpowers/specs/2026-06-27-psf-grouping-color-design.md`.

### tests/

- `test_topf.py` — pure-core + integration tests for topf (separate tool)
- `test_psf.py` — parsing, classification, venv, the grouping axes (session/consolidated/dir/flat), merge_key, the view layer, `--path`, and the colour layer incl. a Machado-2009 protanomaly palette guard
- `conftest.py` — empty; exists so `import psf` resolves

## Key interactions

- **psf is self-contained** — no `import topf`. The `Proc` dataclass, /proc readers, formatting and the renderer all live in psf.py.
- **session view re-roots past collapse** — `find_session_roots` descends through `suppressed` plumbing (a claude's shell may be collapse-suppressed), so the probe/classify/colour set is `collect_session_printed`, not a plain kept-tree walk.
- **two colour channels** — heat-tint (warm, cpu/rss magnitude) on the stat bits and cartographic identity hue (project, on dir/cmd spans) never share a bit; identity is gated on a truecolor terminal, `--no-color` kills both.

## Conventions

- pytest is a dev dependency: `pip install -e ".[dev]"` or `pip install pytest`.
- Git remote `origin` is `hila-shemer/psf.git`; never push to any Majestic-Labs remote.
- Some tests read real `/proc` — pass on Linux only (macOS/WSL1 will fail).
