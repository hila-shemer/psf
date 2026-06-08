# AGENTS.md

## Project overview

<!-- REVIEW(Claude): topfig was removed in b14f665 — stale reference -->
**topf** — a top-like, full-screen Linux process viewer that shows only interesting/heavy subtrees, with windowed CPU, vmstat pane, and interactive expand/collapse. **psf** — a process session finder: classify, group, and summarize running processes by session type, detect venvs.

## Architecture

<!-- REVIEW(Claude): build.py and test_build.py were deleted — stale references. conftest.py also deleted. -->
- `topf.py` (~2400 lines) — the entire application (scan, render, live TUI loop, once/piped mode). Single-file, no package structure.
- `psf.py` (~380 lines) — process session finder; imports public functions from topf.py.
- `tests/test_topf.py`, `tests/test_psf.py` — pytest-based.
- `docs/superpowers/specs/` and `docs/superpowers/plans/` — design docs.

## Commands

- **Run tests:** `pytest tests/` (pytest must be installed — no lockfile or requirements.txt manages deps)
- **Run a single test file:** `pytest tests/test_topf.py`
- **Run topf once (piped):** `python topf.py --once`
- **Run topf live (interactive TUI):** `python topf.py`
<!-- REVIEW(Claude): build.py was deleted — stale command -->

## Conventions

<!-- REVIEW(Claude): topfig/build/index.html are gone — stale conventions -->
- Both source files are bare Python scripts at repo root; installed as console scripts via pyproject.toml (hatchling).
- Git remote `origin` is `hila-shemer/psf.git`; never push to any Majestic-Labs remote.

## Gotchas

- pytest is not listed in any config/lockfile — install manually if missing (`pip install pytest`).
- Some tests (`test_render_once_smoke`) read real `/proc` — they pass on Linux but will fail on macOS/WSL1 or in containers without a populated `/proc`.
<!-- REVIEW(Claude): build.py gone — stale gotcha about KNOBS sync -->
- `topf.py` is both the CLI entrypoint and the importable module — tests do `import topf`, not from a package.