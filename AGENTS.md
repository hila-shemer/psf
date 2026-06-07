# AGENTS.md

## Project overview

**topf** — a top-like, full-screen Linux process viewer that shows only interesting/heavy subtrees, with windowed CPU, vmstat pane, and interactive expand/collapse. **topfig** — a static HTML config editor generated from topf.py's knob constants.

## Architecture

- `topf.py` (~1900 lines) — the entire application (scan, render, live TUI loop, once/piped mode). Single-file, no package structure.
- `build.py` (~150 lines) — parses topf.py's knob constants, injects them into `topfig_template.html`, writes `index.html`.
- `tests/test_topf.py`, `tests/test_build.py` — pytest-based. `conftest.py` is empty; it exists solely so `import topf` / `import build` resolves from the test directory (both source files live at repo root).
- `docs/superpowers/specs/` and `docs/superpowers/plans/` — design docs.

## Commands

- **Run tests:** `pytest tests/` (pytest must be installed — no lockfile or requirements.txt manages deps)
- **Run a single test file:** `pytest tests/test_topf.py`
- **Run topf once (piped):** `python topf.py --once`
- **Run topf live (interactive TUI):** `python topf.py`
- **Regenerate index.html:** `python build.py`

## Conventions

- No packaging; both source files are bare Python scripts at repo root, not inside a package directory.
- `index.html` is a **generated artifact** — never edit it directly. Edit `topfig_template.html` or `topf.py` constants, then `python build.py`.
- Git remote `origin` is `hila-shemer/psf.git`; never push to any Majestic-Labs remote.

## Gotchas

- pytest is not listed in any config/lockfile — install manually if missing (`pip install pytest`).
- Some tests (`test_render_once_smoke`) read real `/proc` — they pass on Linux but will fail on macOS/WSL1 or in containers without a populated `/proc`.
- `topf.py` is both the CLI entrypoint and the importable module — tests do `import topf`, not from a package.
- The `KNOBS` list in `build.py` must stay in sync with the actual constant names in `topf.py`; `build.py` validates this at runtime and raises `ValueError` on mismatch.