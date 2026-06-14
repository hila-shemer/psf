# psf/topf — review brief

## What this is

Two single-file Python CLI tools sharing a common `/proc` parsing core:
- **topf** (~2400 lines) — a `top`-like live TUI process viewer with windowed CPU,
  vmstat pane, disk pane, and interactive expand/collapse.
- **psf** (~380 lines after cleanup) — a process session finder that classifies
  processes by type, detects venvs, and groups by session leader subtrees.

`psf.py` imports public functions from `topf.py` (pure-core parsing, tree building,
formatting) but reimplements private I/O (`_read_text`, `_read_bin`, `CLK_TCK`,
`PAGE_SIZE`) locally. Both are installed as console scripts via hatchling in
`pyproject.toml`.

## Patterns to hold in mind

- **Config-as-module-constants**: All tunables live as UPPER_CASE module-level
  constants at the top of each file — no config files, no env vars (beyond CLI flags).
- **Pure core vs I/O shell**: Functions that don't touch the filesystem are "pure
  core" and testable without `/proc`; the I/O shell (`scan`, `fd_socket_inodes`,
  `read_links`) wraps them. Tests mock at the I/O boundary.
- **Proc dataclass**: `topf.Proc` is the shared process record. `psf` extends it at
  runtime by attaching `.exe` and `.environ` fields (not subclasses — just attribute
  assignment).
- **Test factories**: Both test files define `_proc()` / `_rproc()` helpers that
  construct `topf.Proc` instances with sensible defaults. Tests are pure Python —
  no `/proc` dependency except for the few marked as needing real proc.
- **`prepare_frame` returns 4-tuple**: `(visible_roots, suppressed, collapsible,
  breakout_map)` — this contract is consumed by both `build_rows` and `render`.
- **Named tuples for lightweight types**: `Category`, `SysInfo`, and `Proc` (a
  dataclass) are the only user-defined types beyond builtins.

## Deeper docs

- `CLAUDE.md` — architecture sections, command reference, test running
- `mds/` — design specs and implementation plans (in parent or project root)