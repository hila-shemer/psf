# psf: annotate containerized processes with their container

## Problem

A `claude` running inside a docker/containerd sandbox shows up in `psf` as a
bare process with no hint that it lives in a container, or which one. On a host
running several sandbox loops, every containerized claude looks alike. We want
the container surfaced on the process line — ideally the friendly compose name
(`loop-model-prepare_profiler-claude-loop-run-…`), falling back to the id.

## Constraints

psf is a pure `/proc` reader and self-contained. The container **id** is free
from `/proc/PID/cgroup`; the **friendly name** exists only in docker's metadata
(this host uses the containerd snapshotter — `/var/lib/docker/containers` does
not exist — so the name is reachable only via the `docker` CLI, not a file
read). We accept an *optional, best-effort* `docker` shell-out for the name and
degrade cleanly to the bare id when docker is absent, denied, or slow.

## 1. Container id from /proc (always, pure)

`read_cgroup_container(pid) -> str | None` reads `/proc/PID/cgroup` and extracts
the container id: the 64-hex run as it appears in `docker-<id>.scope` (cgroup v2
systemd, what this host uses), `/docker/<id>` (v1), or the k8s/containerd
variants (`.../<id>`, `cri-containerd-<id>`). Match the first `[0-9a-f]{64}` run;
none → not containerized → `None`. Short id = first 12 chars. A host process's
cgroup has no 64-hex run, so this never false-positives the `docker compose`
launcher itself (it runs on the host).

## 2. Friendly names best-effort (docker ps, cached)

`docker_container_names() -> dict[str, str]` runs one
`docker ps --format '{{.ID}} {{.Names}}'` (`{{.ID}}` is the 12-char short id) with
a ~1s timeout, parsing lines into `{short_id: name}`. `FileNotFoundError`
(no docker), non-zero exit, or `TimeoutExpired` → `{}`. Parsing is a pure
function over the captured stdout string, tested independently of the subprocess.

The map is memoized in the existing on-disk cache under a reserved key with
`CACHE_TTL` (30s): back-to-back snapshots reuse it instead of re-shelling. The
call only fires when at least one printed proc is containerized.

## 3. Boundary detection (which proc gets the bit)

A printed proc `P` is a **container boundary** — and is the one annotated — iff
it has a container id AND (its parent is not in the printed set OR its parent's
container id differs from `P`'s). This marks each container's topmost visible
proc; descendants that share the id stay unannotated, so a whole in-container
subtree carries the label once. Nested containers (different id below) get their
own boundary line. Cgroup is read only for the printed set — the cheap set psf
already probes and samples.

## 4. Rendering

`_detail(node, …)` gains an optional `containers` param (`{pid: label}`, only
boundary pids — mirrors the existing `venv_map`). When `node.pid` is in it,
**prepend** a leading `container:<label>` bit (dim baseline, no identity hue).
`containers` defaults `None` → no bit → existing output is byte-for-byte
unchanged.

Label = friendly name when resolved, else the 12-hex short id. A long friendly
name is compressed like a path: strip a trailing `[-_][0-9a-f]{8,}` run-hash,
then head…tail-truncate to a fixed width, e.g.
`loop-model-prepare_profiler-claude-loop-run-953864d14744`
→ `loop-model-prepare_profiler-…-loop-run`.

## 5. Wiring

In `main()`, after `printed`/`categories` are built:

1. `cid = {p.pid: read_cgroup_container(p.pid) for p in printed}`, dropping
   `None`s.
2. If `cid` non-empty, resolve `names = docker_container_names()` (cached).
3. Compute the boundary pids from `cid` + the parent relation; build
   `containers = {pid: label(cid[pid], names)}` for boundary pids only.
4. Pass `containers=containers` into `build_view` → threaded to `_detail`.

`--path` (`_run_path`) passes the same map (bonus — same cheap computation over
its hit set). No new CLI flag: the `docker ps` timeout and the TTL cache are the
only safety valves.

## 6. Testing

Pure-function unit tests (no live docker):

- `read_cgroup_container`: cgroup v2 `docker-<64>.scope`, v1 `/docker/<64>`,
  k8s/containerd `.../<64>`, and a host cgroup (no hex) → `None`.
- boundary logic: parent not printed → annotate; parent same id → skip; nested
  (parent different id) → annotate.
- name label + compression: friendly name with trailing hash stripped and
  head…tail-truncated; id-only fallback when the name is absent.
- `docker ps` output parsing: well-formed lines → map; empty/garbage → `{}`;
  a mocked `FileNotFoundError`/`TimeoutExpired` → `{}`.
- `_detail`: includes the `container:` bit only when `containers` is passed;
  omitting it leaves the bit list unchanged (byte-compat).

## Out of scope

- `podman` / `nerdctl` friendly names (id-from-cgroup already covers them; the
  name shell-out is docker-only — a trivial later addition).
- Colouring the container bit (stays dim baseline).
- A `--no-docker` flag (timeout + TTL suffice).
