#!/usr/bin/env python3
"""psf - focused process-snapshot tool.

Prints a compact tree of only the *interesting* process subtrees (bazel,
ssh sessions, tmux, claude) under a system summary header, each process
annotated with a category badge ([usr]/[bld]/[cmp]/[inf]/[sys]/[mis]), the
start of its command line, a summarized cwd, the executing binary, the venv it
belongs to (for python), and open ports/sockets.

Deep-probes only the nodes it prints, and caches the expensive socket
analysis across runs (keyed by (pid, starttime), validated by fd-count + TTL).

`--path PATH` switches to a flat listing of every process whose cwd, exe, open
fd, or mmap touches PATH or its subtree — for answering "what is touching this".

Run under sudo/root to see other users' processes; degrades to '?' otherwise.
"""
import argparse
import json
import math
import os
import re
import stat
import subprocess
import sys
import time
from collections import Counter, namedtuple
from dataclasses import dataclass, field


# --- config -----------------------------------------------------------------

CMD_WIDTH = 50            # chars of cmdline shown per process
COLLAPSE_THRESHOLD = 20   # kept-descendant count above which a subtree collapses
TREE_THRESHOLD = 80       # relaxed threshold for `psf tree` - bigger, less-collapsed
CACHE_TTL = 30            # seconds before a cached socket entry is re-probed
REPR_COMMS = 4            # distinct comms named in a collapse summary
SAMPLE_INTERVAL = 0.2     # seconds slept to measure current CPU (0 disables)
PATH_HEAD = 2             # leading path components kept when compressing
PATH_MAX_BASENAME = 30    # basename length above which it is itself shortened
PATH_BASENAME_KEEP = 6    # chars kept each side of '...' in a long basename
DEDUP_MIN = 3             # min identical siblings to merge into one ×N group
NEVER_MERGE = frozenset({"claude"})   # comms never grouped, even when identical
GROUP_PIDS = 8            # member pids listed on a group's detail line
LIFECYCLE_MAX = 40        # max born (and max died) comm-groups listed
# Graduated tint for the cpu/rss detail bits. A value's level = how many of its
# (exponential) anchors it clears; the level indexes TINT_SGR. Level 0 is the
# dim baseline shared by the rest of the line; higher levels add a faint warm
# tint, then full color, then bold red so heavy consumers pop. CPU anchors are
# in cores (1.0 == one full core / 100%, independent of how many cores exist).
TINT_SGR = ("2", "2;33", "33", "1;31")          # dim, dim-yellow, yellow, bold-red
RSS_TINT_ANCHORS = (100 * 1024**2, 1024**3, 5 * 1024**3)   # 100M, 1G, 5G
CPU_TINT_ANCHORS = (0.10, 1.0, 4.0)                        # 10%, 100%, 400%

# System constants, read once. CLK_TCK converts stat jiffies -> seconds;
# PAGE_SIZE converts stat rss (in pages) -> bytes.
CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")

# Resolved-once view of system state needed to turn raw counters into rates.
SysInfo = namedtuple("SysInfo", "clk_tck page_size uptime")

# One /proc/PID/stat row, only the fields we use.
Stat = namedtuple("Stat", "comm state ppid num_threads starttime "
                          "utime stime rss_pages")

# A merged set of >= DEDUP_MIN near-identical sibling Procs.
Group = namedtuple("Group", "members")

# Each matcher: (label, target, regex) where target is "comm" or "cmdline".
DEFAULT_MATCHERS = [
    ("bazel", "comm", re.compile(r"^bazel")),
    ("bazel", "cmdline", re.compile(r"\bbazel\(")),
    ("sshd", "cmdline", re.compile(r"^sshd: ")),
    ("tmux", "comm", re.compile(r"^tmux")),
    ("claude", "comm", re.compile(r"claude")),
    ("claude", "cmdline", re.compile(r"\bclaude\b")),
]

# Session leaders for the `session`/`consolidated` axes - the proc that owns an
# interactive session. The session axis roots each session at the shallowest
# leader (or, lacking one, the shallowest interesting proc) on a path and drops
# the plumbing above it. Patterns over comm/cmdline, like DEFAULT_MATCHERS.
SESSION_LEADERS = [
    ("comm", re.compile(r"^sshd$")),
    ("cmdline", re.compile(r"^sshd: ")),
    ("comm", re.compile(r"^tmux")),
    ("comm", re.compile(r"^login$")),
    ("comm", re.compile(r"claude")),
]

# Process classification: a rule-based category (+ short badge) assigned to
# every printed process, orthogonal to the "interesting subtree" selection above.
# First matching rule wins; a python compile-worker inside a venv is promoted to
# user-session (an interactive .venv python is a session, not a build worker —
# see classify()).
Category = namedtuple("Category", "name badge priority")
CATEGORIES = [
    Category("user-session",   "usr", 5),
    Category("build-daemon",   "bld", 4),
    Category("compile-worker", "cmp", 3),
    Category("infrastructure", "inf", 2),
    Category("system-service", "sys", 1),
    Category("misc",           "mis", 0),
]
CAT_BY_NAME = {c.name: c for c in CATEGORIES}

CLASSIFICATION_RULES = [
    # (target, compiled_regex, category_name) — target is "comm" or "cmdline".
    ("comm", re.compile(r"^claude"),      "user-session"),
    ("comm", re.compile(r"^vim$"),        "user-session"),
    ("comm", re.compile(r"^nvim$"),       "user-session"),
    ("comm", re.compile(r"^emacs"),       "user-session"),
    ("cmdline", re.compile(r"\bcode\b"),  "user-session"),
    ("comm", re.compile(r"^bazel"),       "build-daemon"),
    ("cmdline", re.compile(r"\bbazel\("), "build-daemon"),
    ("comm", re.compile(r"^buck"),        "build-daemon"),
    ("comm", re.compile(r"^gradle"),      "build-daemon"),
    ("comm", re.compile(r"^ninja"),       "build-daemon"),
    ("comm", re.compile(r"^java$"),       "build-daemon"),
    ("comm", re.compile(r"^clang"),       "compile-worker"),
    ("comm", re.compile(r"^cc1"),         "compile-worker"),
    ("comm", re.compile(r"^gcc"),         "compile-worker"),
    ("comm", re.compile(r"^g\+\+"),       "compile-worker"),
    ("comm", re.compile(r"^javac"),       "compile-worker"),
    ("comm", re.compile(r"^rustc"),       "compile-worker"),
    ("comm", re.compile(r"^go$"),         "compile-worker"),
    ("comm", re.compile(r"^python"),      "compile-worker"),
    ("comm", re.compile(r"^node"),        "compile-worker"),
    ("comm", re.compile(r"^systemd$"),    "system-service"),
    ("comm", re.compile(r"^dbus"),        "system-service"),
    ("comm", re.compile(r"^cron"),        "system-service"),
    ("comm", re.compile(r"^sshd$"),       "infrastructure"),
    ("comm", re.compile(r"^bash$"),       "infrastructure"),
    ("comm", re.compile(r"^zsh$"),        "infrastructure"),
    ("comm", re.compile(r"^tmux"),        "infrastructure"),
    ("comm", re.compile(r"^login"),       "infrastructure"),
]

# venv detection: env-var names checked first, then these exe-path markers.
VENV_PATH_MARKERS = (".venv/bin/", "env/bin/", "venv/bin/")

# --path: max /proc/PID/maps lines scanned per process when matching a subtree.
MAPS_MAX_PER_PROC = 3

# Container annotation. A containerized proc's cgroup carries the 64-hex id in a
# docker-<id>.scope / /docker/<id> / k8s path; the short id is its first 12 chars
# (the free, universal signal). The friendly compose name lives only in docker's
# metadata, so it's a best-effort `docker ps` shell-out (DOCKER_PS_TIMEOUT, cached
# under CACHE_TTL) that degrades to the bare id. A long name is compressed like a
# path: a trailing run-hash is stripped, then head…tail-elided to fit the width.
CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}")
CONTAINER_HASH_RE = re.compile(r"[-_][0-9a-f]{8,}$")   # trailing compose run-hash
CONTAINER_NAME_WIDTH = 40      # friendly-name chars before head…tail elision
DOCKER_PS_TIMEOUT = 1.0        # seconds before the `docker ps` probe is abandoned

# Idle MCP-helper hiding. Every claude session spawns the same context7-mcp
# helper chain (npm exec -> sh -c -> node); it is almost always idle yet triples
# a claude's line count, and collapse() won't fold it (the subtree is too small).
# A *unit* is the shallowest cmdline match (HELPER_MARKERS) plus its whole kept
# subtree - the node leaf's cmdline is truncated and does NOT literally contain
# "context7", so per-proc matching would miss it. A unit is only hidden when its
# nearest visible ancestor is a claude (classified user-session). A node is "hot"
# (kept visible) if ANY threshold below is strictly exceeded; CPU is in cores
# (1.0 == one full core), matching CPU_TINT_ANCHORS. See plan_helper_hiding().
HELPER_MARKERS = (re.compile(r"context7-mcp"),)   # cmdline; shallowest match roots a unit
HELPER_CUR_CPU = 0.02       # recent-sample CPU fraction above which a helper is busy
HELPER_AVG_CPU = 0.05       # lifetime-average CPU fraction above which it is busy
HELPER_RSS = 200 * 1024**2  # resident bytes above which it is busy

# ---------------------------------------------------------------------------


@dataclass
class Proc:
    pid: int
    ppid: int
    comm: str
    cmdline: str
    state: str
    num_threads: int
    starttime: int
    uid: int
    utime: int = 0                  # user jiffies (stat field 14)
    stime: int = 0                  # system jiffies (stat field 15)
    rss_pages: int = 0              # resident pages (stat field 24)
    cpu_current: float = None       # recent CPU fraction from probe sampling
    children: list = field(default_factory=list)   # list[Proc]
    interesting: bool = False
    kept: bool = False
    collapsed: bool = False         # this node's filler descendants are summarized
    collapse_note: str = ""         # histogram summary line for collapsed nodes
    cwd: str = None
    exe: str = None
    sockets_str: str = ""           # rendered socket summary for this process


# --- pure core: parsing -----------------------------------------------------


def parse_stat(content):
    """Parse /proc/PID/stat text into a Stat. comm may contain spaces and
    parens, so split on the LAST ')'."""
    open_paren = content.index("(")
    close_paren = content.rindex(")")
    comm = content[open_paren + 1:close_paren]
    rest = content[close_paren + 2:].split()
    # rest[i] is stat field (i + 3): field3=state, field4=ppid, field14=utime,
    # field15=stime, field20=num_threads, field22=starttime, field24=rss.
    return Stat(
        comm=comm,
        state=rest[0],
        ppid=int(rest[1]),
        num_threads=int(rest[17]),
        starttime=int(rest[19]),
        utime=int(rest[11]),
        stime=int(rest[12]),
        rss_pages=int(rest[21]),
    )


def clean_cmdline(raw, comm=""):
    """Turn raw /proc/PID/cmdline (NUL-separated) into a readable string.
    Kernel threads and zombies have an empty cmdline -> show [comm]."""
    s = raw.replace("\0", " ").strip()
    if s:
        return s
    return "[%s]" % comm if comm else ""


# --- pure core: tree --------------------------------------------------------


def build_tree(procs):
    """Populate .children from .ppid. Return list of root Procs (ppid not in
    the set, or ppid 0) sorted by pid."""
    for p in procs.values():
        p.children = []
    roots = []
    for p in procs.values():
        parent = procs.get(p.ppid)
        if parent is not None and parent is not p:
            parent.children.append(p)
        else:
            roots.append(p)
    for p in procs.values():
        p.children.sort(key=lambda c: c.pid)
    roots.sort(key=lambda r: r.pid)
    return roots


# --- pure core: selection ---------------------------------------------------


def is_interesting(proc, matchers):
    for _label, target, rx in matchers:
        hay = proc.comm if target == "comm" else proc.cmdline
        if rx.search(hay or ""):
            return True
    return False


def _descendants(proc):
    out = []
    stack = list(proc.children)
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(node.children)
    return out


def select(procs, matchers):
    """Mark .interesting and .kept. Kept = interesting roots + their
    descendants + their ancestors (so the tree stays rooted). Kernel-thread
    subtrees (under pid 2) are never kept unless explicitly matched."""
    kthreadd = procs.get(2)
    kthread_pids = set()
    if kthreadd is not None:
        kthread_pids = {2} | {d.pid for d in _descendants(kthreadd)}

    for p in procs.values():
        p.interesting = is_interesting(p, matchers) and p.pid not in kthread_pids
        p.kept = False

    for p in list(procs.values()):
        if not p.interesting:
            continue
        p.kept = True
        for d in _descendants(p):       # subtree
            d.kept = True
        anc = procs.get(p.ppid)         # ancestors up to a root
        while anc is not None and not anc.kept:
            anc.kept = True
            anc = procs.get(anc.ppid)


def group_siblings(procs, dedup_min, never_merge, key_fn=None):
    """Partition a node's visible sibling Procs into a render list. Procs sharing
    a key - default (comm, exe), or pass key_fn for the category-gated merge key -
    with >= dedup_min members, none of whose comm is in never_merge, become a
    Group; everything else stays an individual Proc. dedup_min falsy (None/0)
    disables grouping. Items are ordered by their smallest pid so output is
    stable."""
    if not dedup_min:
        return list(procs)
    if key_fn is None:
        key_fn = lambda p: (p.comm, p.exe)
    buckets = {}
    order = []
    for p in procs:
        key = key_fn(p)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(p)
    items = []
    for key in order:
        members = buckets[key]
        if (len(members) >= dedup_min
                and not any(m.comm in never_merge for m in members)):
            items.append(Group(members=members))
        else:
            items.extend(members)
    items.sort(key=lambda it: min(m.pid for m in it.members)
               if isinstance(it, Group) else it.pid)
    return items


def _exe_unknown(exe):
    """True when a proc's exe is missing or unstable - unreadable link ('?'),
    empty, or a kernel ' (deleted)' marker. Such procs must not merge."""
    return (not exe) or exe == "?" or exe.endswith(" (deleted)")


def merge_key(proc, categories=None):
    """Sibling-merge key for the grouping axes: procs collapse into one ×N only
    when category, comm and exe all match. Category gates merging so a venv
    python (promoted to user-session) never merges with a bare compile-worker
    python - same comm, different role. An unknown/unstable exe yields a per-pid
    unique key, so distinct unknown-exe daemons don't fold into one bogus group."""
    if _exe_unknown(proc.exe):
        return ("\x00unknown", proc.pid)
    cat = (categories[proc.pid].name
           if categories and proc.pid in categories else "")
    return (cat, proc.comm, proc.exe)


def is_session_leader(proc):
    """True when a proc heads an interactive session (ssh/tmux/login/claude),
    matched over comm/cmdline like the interest matchers."""
    for target, rx in SESSION_LEADERS:
        if rx.search(proc.comm if target == "comm" else (proc.cmdline or "")):
            return True
    return False


def find_session_roots(roots):
    """Cut the kept forest into sessions top-down: descending each path, the
    first proc that is a session leader or interesting becomes a session root -
    the plumbing above it is dropped and its whole subtree (nested leaders
    included as sub-nodes) is that session. Descends THROUGH suppressed plumbing:
    collapse may suppress the bash that parents a claude, but the claude is still
    the session the axis exists to surface. Returns the session-root Procs sorted
    by pid for stable output."""
    out = []

    def descend(node):
        if not node.kept:
            return
        if is_session_leader(node) or node.interesting:
            out.append(node)            # session root - its subtree is the session
            return
        for c in node.children:
            descend(c)

    for r in roots:
        descend(r)
    return sorted(out, key=lambda p: p.pid)


def _session_procs(root, suppressed, transparent=frozenset()):
    """A session root plus its visible (kept, non-suppressed) descendants.
    `transparent` helper members are passed through, not counted."""
    out = []

    def walk(node):
        out.append(node)
        for c in _visible_children(node, suppressed, transparent):
            walk(c)

    walk(root)
    return out


def collect_session_printed(roots, suppressed):
    """The procs a session-axis view shows. This is the set to decorate/probe/
    sample - a plain kept-tree walk stops at suppressed plumbing and would skip a
    session whose parent shell was collapsed away."""
    return [p for r in find_session_roots(roots)
            for p in _session_procs(r, suppressed)]


def session_signature(leader, categories, suppressed, transparent=frozenset()):
    """What makes two sessions 'the same' for the consolidated axis: the leader's
    category/comm/uid plus the sorted multiset of its visible children's merge
    keys. Folding on comm alone would fold an idle shell into a heavy build and
    mislead through the pooled child counts."""
    cat = (categories[leader.pid].name
           if categories and leader.pid in categories else None)
    child_keys = tuple(sorted(
        merge_key(c, categories)
        for c in _visible_children(leader, suppressed, transparent)))
    return (cat, leader.comm, leader.uid, child_keys)


def consolidate_sessions(sroots, categories, suppressed, dedup_min, never_merge,
                         transparent=frozenset()):
    """Fold signature-equal session roots into one ×N (their children pool and
    sum downstream); the rest stay individual. Just group_siblings keyed on the
    session signature instead of the per-proc merge key."""
    return group_siblings(
        sroots, dedup_min, never_merge,
        key_fn=lambda r: session_signature(r, categories, suppressed,
                                           transparent))


# --- pure core: collapse ----------------------------------------------------


def collapse(procs, threshold=COLLAPSE_THRESHOLD):
    """For each kept node whose kept-descendant count exceeds threshold, set
    .collapsed and a .collapse_note histogram, and add its *non-interesting*
    kept descendants to the suppressed set (interesting descendants stay
    visible). Returns the set of suppressed pids."""
    suppressed = set()
    for p in procs.values():
        if not p.kept:
            continue
        kept_desc = [d for d in _descendants(p) if d.kept]
        if len(kept_desc) <= threshold:
            continue
        hide = [d for d in kept_desc if not d.interesting and d.pid not in suppressed]
        if len(hide) <= threshold:
            continue
        p.collapsed = True
        suppressed.update(d.pid for d in hide)
        hist = Counter(d.comm for d in hide)
        top = ", ".join("%s×%d" % (c, n)
                        for c, n in hist.most_common(REPR_COMMS))
        extra = len(hist) - REPR_COMMS
        if extra > 0:
            top += ", …"
        p.collapse_note = "… (+%d descendants: %s)" % (len(hide), top)
    return suppressed


def _helper_marker(proc):
    """The HELPER_MARKERS pattern text a proc's cmdline matches, else None.
    The pattern doubles as the helper name shown in the trailing note."""
    for rx in HELPER_MARKERS:
        if rx.search(proc.cmdline or ""):
            return rx.pattern
    return None


def _helper_hot(proc, sysinfo):
    """True when a helper proc is doing enough to stay visible: current CPU,
    lifetime-average CPU, or RSS strictly over its threshold. A None current
    sample (sampling off) counts as 0, so only avg/rss can trip it."""
    if proc.rss_pages * sysinfo.page_size > HELPER_RSS:
        return True
    cur = proc.cpu_current if proc.cpu_current is not None else 0.0
    if cur > HELPER_CUR_CPU:
        return True
    life = lifetime_secs(proc.starttime, sysinfo.uptime, sysinfo.clk_tck)
    avg = cpu_fraction(proc.utime + proc.stime, life, sysinfo.clk_tck)
    return avg is not None and avg > HELPER_AVG_CPU


def plan_helper_hiding(roots, categories, sysinfo):
    """Post-sampling planner: walk the kept forest and decide which idle helper
    procs to hide. Runs AFTER sampling so current-CPU is known (helpers stay in
    the sampled/probed set). Returns (transparent, lifted, notes):

    - transparent: set[pid] of cold members - skipped in the render walk but
      *passed through* (their visible descendants surface in their place), so a
      lifted hot node under a hidden parent still renders.
    - lifted: set[pid] of hot members that surface through a transparent parent;
      the renderer marks them with '↑ '.
    - notes: {anchor_pid: [note, …]} trailing dim note(s), keyed by the claude.

    A unit is the shallowest cmdline match (its whole kept subtree); it is only
    eligible when its nearest visible ancestor is classified user-session. An
    all-cold unit goes wholly transparent (nothing surfaces); a partial one puts
    cold members in transparent and hot members in lifted."""
    transparent, lifted, notes = set(), set(), {}

    def unit_members(root):
        out, stack = [], [root]
        while stack:
            n = stack.pop()
            if not n.kept:
                continue
            out.append(n)
            stack.extend(n.children)
        return out

    def descend(node, anchor):
        if not node.kept:
            return
        name = _helper_marker(node)
        eligible = (name is not None and anchor is not None and categories
                    and anchor.pid in categories
                    and categories[anchor.pid].name == "user-session")
        if eligible:
            members = unit_members(node)
            hot, cold = [], []          # partition once: predicate is expensive
            for m in members:
                (hot if _helper_hot(m, sysinfo) else cold).append(m)
            if not cold:
                # Every member is hot - nothing idle to hide. Leave the unit
                # rendered normally (no transparent, no lift marker, no note);
                # a "0 idle hidden" note with spurious '↑ ' markers would lie.
                return
            transparent.update(m.pid for m in cold)
            if hot:
                lifted.update(m.pid for m in hot)
                note = "… (%s helper: %d idle hidden)" % (name, len(cold))
            else:
                note = "… (%s helper hidden)" % name
            notes.setdefault(anchor.pid, []).append(note)
            return                      # whole unit handled - don't recurse in
        for c in node.children:
            descend(c, node)

    for r in roots:
        descend(r, None)
    return transparent, lifted, notes


# --- pure core: classification + venv ---------------------------------------


def classify(proc, matchers=None, venv_resolver=None):
    """Return the Category for a process. First matching rule in `matchers`
    (CLASSIFICATION_RULES by default) wins. A python process that would be a
    compile-worker is promoted to user-session when venv_resolver(proc) reports a
    venv. venv_resolver is injectable so classify() itself stays pure."""
    if matchers is None:
        matchers = CLASSIFICATION_RULES
    for target, rx, cat_name in matchers:
        hay = proc.comm if target == "comm" else proc.cmdline
        if rx.search(hay or ""):
            if (cat_name == "compile-worker" and proc.comm.startswith("python")
                    and venv_resolver is not None and venv_resolver(proc)):
                return CAT_BY_NAME["user-session"]
            return CAT_BY_NAME[cat_name]
    return CAT_BY_NAME["misc"]


def read_environ(pid):
    """Parse /proc/PID/environ (NUL-separated KEY=VAL) into a dict. {} on any
    failure — unreadable without privilege, or the process vanished."""
    raw = _read_bin("%s/%d/environ" % (PROC, pid))
    if not raw:
        return {}
    text = raw.decode("utf-8", errors="replace")
    return dict(kv.split("=", 1) for kv in text.split("\0") if "=" in kv)


def detect_venv(proc):
    """The Python virtualenv a process belongs to, or None. Prefers the
    VIRTUAL_ENV / CONDA_PREFIX env vars (which name the venv dir directly); else
    derives it from the exe path, returning the venv DIR — the component before
    '/bin/', e.g. '…/proj/.venv' — so both forms read the same."""
    env = read_environ(proc.pid)
    if env.get("VIRTUAL_ENV"):
        return env["VIRTUAL_ENV"]
    if env.get("CONDA_PREFIX"):
        return env["CONDA_PREFIX"]
    exe = proc.exe or ""
    for marker in VENV_PATH_MARKERS:
        idx = exe.find(marker)
        if idx >= 0:
            return exe[:idx] + marker.split("/", 1)[0]
    return None


# --- pure core: socket parsing ----------------------------------------------


_TCP_STATES = {"0A": "LISTEN", "01": "ESTAB"}


def parse_net_tcp(content, ipv6=False):
    """Parse /proc/net/tcp or tcp6. Return {inode: (proto, state, port)}.
    Addresses are hex; the port is the hex part after ':' in local_address."""
    proto = "tcp6" if ipv6 else "tcp"
    out = {}
    for line in content.splitlines()[1:]:        # skip header
        f = line.split()
        if len(f) < 10:
            continue
        local = f[1]
        st = f[3]
        inode = int(f[9])
        if inode == 0:
            continue
        port = int(local.rsplit(":", 1)[1], 16)
        out[inode] = (proto, _TCP_STATES.get(st, st), port)
    return out


def parse_net_udp(content, ipv6=False):
    """Parse /proc/net/udp(6). UDP has no LISTEN state; report the bound port
    as state 'UDP'."""
    proto = "udp6" if ipv6 else "udp"
    out = {}
    for line in content.splitlines()[1:]:
        f = line.split()
        if len(f) < 10:
            continue
        inode = int(f[9])
        if inode == 0:
            continue
        port = int(f[1].rsplit(":", 1)[1], 16)
        out[inode] = (proto, "UDP", port)
    return out


def parse_net_unix(content):
    """Parse /proc/net/unix. Return {inode: ('unix', path)} for NAMED sockets
    only (unnamed sockets carry no useful info for our summary)."""
    out = {}
    for line in content.splitlines()[1:]:
        f = line.split()
        if len(f) < 8:                # column 8 (path) absent => unnamed
            continue
        inode = int(f[6])
        out[inode] = ("unix", f[7])
    return out


def format_sockets(inodes, netmap):
    """Summarize a process's socket inodes against a merged netmap.
    Listening/UDP ports shown explicitly; established TCP counted; named unix
    paths listed. Returns a single compact string ('' if nothing matched)."""
    listen_ports = set()
    est = 0
    unix_paths = []
    for ino in inodes:
        entry = netmap.get(ino)
        if entry is None:
            continue
        if entry[0] == "unix":
            unix_paths.append(entry[1])
        elif entry[1] == "ESTAB":
            est += 1
        else:                          # LISTEN or UDP
            listen_ports.add(entry[2])
    parts = []
    if listen_ports:
        parts.append("LISTEN " + " ".join(":%d" % p for p in sorted(listen_ports)))
    if est:
        parts.append("+%d est" % est)
    for path in sorted(set(unix_paths)):
        parts.append("unix:" + path)
    return "  ".join(parts)


# --- pure core: resource stats ----------------------------------------------


def lifetime_secs(starttime, uptime, clk_tck):
    """Wall-clock seconds the process has been alive: system uptime minus the
    process's start offset (stat starttime is in clock ticks since boot)."""
    return uptime - starttime / clk_tck


def cpu_fraction(cpu_ticks, wall_secs, clk_tck):
    """CPU busy fraction = cpu-seconds / wall-seconds. 1.0 == one core saturated
    (can exceed 1.0 across cores). None when the window is non-positive."""
    if wall_secs <= 0:
        return None
    return (cpu_ticks / clk_tck) / wall_secs


def fmt_pct(frac):
    """Format a CPU fraction as a percentage with magnitude-scaled precision so
    tiny lifetime averages stay legible (e.g. 0.0002 -> '0.02%') instead of
    rounding to '0.00%'. None -> None; non-positive -> '0%'."""
    if frac is None:
        return None
    pct = frac * 100.0
    if pct <= 0:
        return "0%"
    if pct >= 10:
        return "%.0f%%" % pct
    if pct >= 1:
        return "%.1f%%" % pct
    # sub-1%: two significant figures in plain decimal (never sci notation,
    # which would be unreadable in a process monitor), trailing zeros trimmed.
    decimals = 1 - int(math.floor(math.log10(pct)))
    return ("%.*f" % (decimals, pct)).rstrip("0").rstrip(".") + "%"


def fmt_bytes(n):
    """Human-readable binary size: 0 -> '0', <1KiB -> 'NB', else one decimal
    with a K/M/G/T suffix."""
    if n <= 0:
        return "0"
    if n < 1024:
        return "%dB" % n
    val = float(n)
    for unit in ("K", "M", "G", "T"):
        val /= 1024.0
        if val < 1024 or unit == "T":
            return "%.1f%s" % (val, unit)


def fmt_duration(secs):
    """Compact elapsed time, top two units: '5s', '1m5s', '1h2m', '1d1h'."""
    secs = int(secs) if secs > 0 else 0
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins, sec = divmod(rem, 60)
    if days:
        return "%dd%dh" % (days, hours)
    if hours:
        return "%dh%dm" % (hours, mins)
    if mins:
        return "%dm%ds" % (mins, sec)
    return "%ds" % sec


def brace_summary(values, max_items=5):
    """Collapse strings to a common prefix + brace of the differing tails:
    ['a/x', 'a/y'] -> 'a/{x,y}'. A single distinct value is returned as-is."""
    uniq = sorted(set(values))
    if len(uniq) == 1:
        return uniq[0]
    prefix = os.path.commonprefix(uniq)
    tails = [v[len(prefix):] for v in uniq]
    shown = tails[:max_items]
    more = ",..." if len(tails) > max_items else ""
    return "%s{%s%s}" % (prefix, ",".join(shown), more)


def range_str(values, fmt):
    """'lo–hi' (en-dash) via fmt(); a single value when min == max. `values`
    must be non-empty."""
    lo, hi = min(values), max(values)
    if lo == hi:
        return fmt(lo)
    return "%s–%s" % (fmt(lo), fmt(hi))


def count_states(procs):
    """(running, sleeping, zombie) task counts from a {pid: Proc} snapshot;
    sleeping folds in D (uninterruptible) and I (idle). Pure."""
    running = sum(1 for p in procs.values() if p.state == "R")
    sleeping = sum(1 for p in procs.values() if p.state in ("S", "D", "I"))
    zombie = sum(1 for p in procs.values() if p.state == "Z")
    return running, sleeping, zombie


def parse_meminfo(content):
    """Parse /proc/meminfo into BYTES (the file is in kB), keeping the few keys
    the header needs: free/buff/cache/mem_total/swap_total/swap_free. Missing ->
    None, except swap_total -> 0 so swap-off is the safe default. Pure."""
    raw = {}
    for line in content.splitlines():
        f = line.split()
        if len(f) >= 2 and f[0].endswith(":"):
            try:
                raw[f[0][:-1]] = int(f[1]) * 1024
            except ValueError:
                pass
    return {"free": raw.get("MemFree"), "buff": raw.get("Buffers"),
            "cache": raw.get("Cached"), "mem_total": raw.get("MemTotal"),
            "swap_total": raw.get("SwapTotal", 0),
            "swap_free": raw.get("SwapFree")}


def diff_snapshots(before, after):
    """Given two {pid: Proc} snapshots, return (born, died) Proc lists keyed by
    (pid, starttime) so a reused PID is treated as a death + a birth. born are
    drawn from `after`, died from `before`. Each list is sorted by pid."""
    def keyed(snap):
        return {(p.pid, p.starttime): p for p in snap.values()}
    kb, ka = keyed(before), keyed(after)
    born = [ka[k] for k in ka if k not in kb]
    died = [kb[k] for k in kb if k not in ka]
    born.sort(key=lambda p: p.pid)
    died.sort(key=lambda p: p.pid)
    return born, died


def _dominant_parent(members, parents):
    """The most common parent comm among members (None if unknown)."""
    counts = Counter(parents.get(m.ppid) for m in members)
    counts.pop(None, None)
    return counts.most_common(1)[0][0] if counts else None


def _lifecycle_side(procs, sign, parents, sysinfo, is_died):
    """One 'born'/'died' line body: group by comm (×N), largest first, each
    annotated with its dominant parent comm; singletons show pid + comm and,
    for deaths, how long they had lived. Capped at LIFECYCLE_MAX comm-groups."""
    buckets = {}
    for p in procs:
        buckets.setdefault(p.comm, []).append(p)
    ordered = sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    parts = []
    for comm, members in ordered[:LIFECYCLE_MAX]:
        pcomm = _dominant_parent(members, parents)
        tag = " (←%s)" % pcomm if pcomm else ""
        if len(members) >= 2:
            parts.append("×%d %s%s" % (len(members), comm, tag))
        else:
            p = members[0]
            lived = ""
            if is_died:
                lived = " lived %s" % fmt_duration(
                    lifetime_secs(p.starttime, sysinfo.uptime, sysinfo.clk_tck))
            parts.append("%s%d %s%s%s" % (sign, p.pid, comm, tag, lived))
    if len(ordered) > LIFECYCLE_MAX:
        parts.append("+%d more" % (len(ordered) - LIFECYCLE_MAX))
    return "  ".join(parts)


def format_lifecycle(born, died, parents, sysinfo, dt, color=False):
    """The born/died section: a header naming the measured window, then a
    'born:' and/or 'died:' line. `parents` maps pid -> comm. Returns [] when
    nothing changed in the window."""
    if not born and not died:
        return []
    lines = ["lifecycle — system-wide, %.2gs window:" % dt]
    if born:
        lines.append("  born:  " + _lifecycle_side(born, "+", parents,
                                                    sysinfo, False))
    if died:
        lines.append("  died:  " + _lifecycle_side(died, "-", parents,
                                                    sysinfo, True))
    if color:
        lines = ["\x1b[2m%s\x1b[0m" % ln for ln in lines]
    return lines


# --- cache ------------------------------------------------------------------


class Cache:
    """Cross-run cache of resolved socket summaries, keyed by (pid, starttime).
    Freshness = fd count matches AND entry age < CACHE_TTL. A changed boot_id
    invalidates the whole file. `now` is injectable for testing."""

    def __init__(self, path, boot_id, now, ttl=CACHE_TTL):
        self.path = path
        self.boot_id = boot_id
        self.now = now
        self.ttl = ttl
        self._loaded = {}      # key "pid:start" -> {fdcount, sockets, ts}
        self._fresh = {}       # entries probed/validated this run, to persist
        self._load()

    def _load(self):
        try:
            with open(self.path) as fh:
                blob = json.load(fh)
        except (OSError, ValueError):
            return
        if blob.get("boot_id") != self.boot_id:
            return
        self._loaded = blob.get("entries", {})

    @staticmethod
    def _key(pid, starttime):
        return "%d:%d" % (pid, starttime)

    def get(self, pid, starttime, fdcount):
        """Return cached socket summary if fresh, else None."""
        e = self._loaded.get(self._key(pid, starttime))
        if e is None:
            return None
        if e.get("fdcount") != fdcount:
            return None
        if self.now - e.get("ts", 0) >= self.ttl:
            return None
        self._fresh[self._key(pid, starttime)] = e   # carry forward
        return e.get("sockets")

    def put(self, pid, starttime, fdcount, sockets):
        self._fresh[self._key(pid, starttime)] = {
            "fdcount": fdcount, "sockets": sockets, "ts": self.now}

    def save(self, live_keys):
        """Persist only entries whose (pid, starttime) is still live."""
        live = {self._key(p, s) for (p, s) in live_keys}
        entries = {k: v for k, v in self._fresh.items() if k in live}
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w") as fh:
                json.dump({"boot_id": self.boot_id, "entries": entries}, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass     # cache is best-effort; never fail the run over it


# --- I/O shell: live /proc readers ------------------------------------------

PROC = "/proc"


def _read(path):
    with open(path) as fh:
        return fh.read()


def _read_text(path):
    """Read a /proc file as text, '' on failure — for best-effort reads (environ,
    maps) where a vanished or unprivileged pid must not abort the pass."""
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


def _read_bin(path):
    """Read a /proc file as bytes, b'' on failure (environ is NUL-separated)."""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return b""


def scan():
    """One cheap pass over /proc. Returns {pid: Proc} with stat/cmdline/uid
    filled; cwd/exe/sockets are deferred to probe()."""
    procs = {}
    for name in os.listdir(PROC):
        if not name.isdigit():
            continue
        pid = int(name)
        base = "%s/%d" % (PROC, pid)
        try:
            st = parse_stat(_read(base + "/stat"))
            cmdline = clean_cmdline(_read(base + "/cmdline"), st.comm)
            uid = os.stat(base).st_uid
        except (OSError, ValueError, IndexError):
            continue        # process vanished mid-scan, or unreadable
        procs[pid] = Proc(pid=pid, ppid=st.ppid, comm=st.comm, cmdline=cmdline,
                          state=st.state, num_threads=st.num_threads,
                          starttime=st.starttime, uid=uid,
                          utime=st.utime, stime=st.stime,
                          rss_pages=st.rss_pages)
    return procs


def fd_socket_inodes(pid):
    """Return (fd_count, {socket_inodes}) for a pid. fd_count is the cheap
    freshness fingerprint (one readdir, no readlinks beyond socket fds)."""
    fddir = "%s/%d/fd" % (PROC, pid)
    try:
        fds = os.listdir(fddir)
    except OSError:
        return 0, set()
    inodes = set()
    for fd in fds:
        try:
            target = os.readlink("%s/%s" % (fddir, fd))
        except OSError:
            continue
        if target.startswith("socket:["):
            inodes.add(int(target[8:-1]))
    return len(fds), inodes


def read_links(pid):
    """Return (cwd, exe) via readlink, or '?' for whichever is unreadable."""
    base = "%s/%d" % (PROC, pid)

    def link(name):
        try:
            return os.readlink(base + "/" + name)
        except OSError:
            return "?"
    return link("cwd"), link("exe")


def netns_of(pid):
    try:
        return os.readlink("%s/%d/ns/net" % (PROC, pid))
    except OSError:
        return ""


def read_cgroup_container(pid, read=None):
    """Short (12-char) container id from /proc/PID/cgroup, or None if the proc
    isn't in a container. Matches the 64-hex id docker/containerd/k8s embed in
    the cgroup path (docker-<id>.scope, /docker/<id>, /kubepods/.../<id>); a host
    proc's cgroup has no 64-hex run, so the docker-compose launcher on the host
    never false-positives. `read` is injectable for testing."""
    if read is None:
        read = lambda p: _read_text("%s/%d/cgroup" % (PROC, p))
    m = CONTAINER_ID_RE.search(read(pid) or "")
    return m.group(0)[:12] if m else None


def _parse_docker_ps(output):
    """{short_id: name} from `docker ps --format '{{.ID}} {{.Names}}'` output.
    Pure, so it's tested without a live docker."""
    out = {}
    for line in output.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            out[parts[0]] = parts[1].strip()
    return out


def docker_container_names(runner=None):
    """Best-effort {short_id: friendly_name} via one `docker ps`. Returns {} when
    docker is missing, errors, or times out - the id-from-cgroup path still works,
    this only enriches it. `runner` (the subprocess call) is injectable."""
    if runner is None:
        runner = lambda: subprocess.run(
            ["docker", "ps", "--format", "{{.ID}} {{.Names}}"],
            capture_output=True, text=True, timeout=DOCKER_PS_TIMEOUT)
    try:
        res = runner()
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return {}
    if getattr(res, "returncode", 1) != 0:
        return {}
    return _parse_docker_ps(res.stdout or "")


def docker_names_cached(cache_dir, now, no_cache=False, runner=None):
    """docker_container_names() memoized to <cache_dir>/docker.json under a
    CACHE_TTL window, so back-to-back snapshots don't re-shell. A cache miss (or
    --no-cache) probes live; only a non-empty map is persisted (the common
    no-docker failure is instant, so caching {} would just stall a fresh start)."""
    path = os.path.join(cache_dir, "docker.json")
    if not no_cache:
        try:
            with open(path) as fh:
                blob = json.load(fh)
            if now - blob.get("ts", 0) < CACHE_TTL:
                return blob["map"]
        except (OSError, ValueError, KeyError):
            pass
    names = docker_container_names(runner)
    if names and not no_cache:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(path + ".tmp", "w") as fh:
                json.dump({"ts": now, "map": names}, fh)
            os.replace(path + ".tmp", path)
        except OSError:
            pass          # cache is best-effort; never fail the run over it
    return names


def compress_container_name(name, width=CONTAINER_NAME_WIDTH):
    """Fit a friendly container name: drop a trailing compose run-hash, then
    head…tail-elide if still over `width`."""
    stripped = CONTAINER_HASH_RE.sub("", name) or name
    if len(stripped) <= width:
        return stripped
    k = (width - 1) // 2
    return stripped[:k] + "…" + stripped[-k:]


def container_label(short_id, names):
    """Display label for a container: the compressed friendly name if docker
    resolved one, else the bare short id."""
    name = names.get(short_id)
    return compress_container_name(name) if name else short_id


def container_annotations(printed, cid, names):
    """{pid: label} for each container-boundary proc - the topmost printed proc
    in a container. A proc is a boundary when its parent isn't printed or sits in
    a different container; descendants sharing the id inherit the label silently,
    so a whole in-container subtree is annotated once. cid: {pid: short_id} for
    the containerized printed procs."""
    printed_ids = {p.pid for p in printed}
    out = {}
    for p in printed:
        my = cid.get(p.pid)
        if my is None:
            continue
        parent = cid.get(p.ppid) if p.ppid in printed_ids else None
        if parent != my:
            out[p.pid] = container_label(my, names)
    return out


def build_container_map(printed, cache_dir, now, no_cache=False):
    """{pid: label} for the container-boundary procs among `printed`. Reads each
    printed proc's cgroup (cheap), and only when something is containerized does
    the one cached `docker ps` to enrich ids into friendly names. Empty when no
    printed proc lives in a container."""
    cid = {}
    for p in printed:
        c = read_cgroup_container(p.pid)
        if c:
            cid[p.pid] = c
    if not cid:
        return {}
    names = docker_names_cached(cache_dir, now, no_cache)
    return container_annotations(printed, cid, names)


def read_boot_id():
    try:
        return _read("/proc/sys/kernel/random/boot_id").strip()
    except OSError:
        return ""


def read_uptime():
    """System uptime in seconds (first field of /proc/uptime)."""
    try:
        return float(_read("/proc/uptime").split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def read_loadavg():
    """(1m, 5m, 15m) load averages from /proc/loadavg; (None, None, None) on
    failure."""
    try:
        f = _read("/proc/loadavg").split()
        return float(f[0]), float(f[1]), float(f[2])
    except (OSError, ValueError, IndexError):
        return (None, None, None)


def cores_count():
    """Number of online CPUs — for the header line only; CPU figures are already
    in cores. Falls back to 1."""
    return os.cpu_count() or 1


def _parents_map(*snaps):
    """pid -> comm across one or more {pid: Proc} snapshots (for annotating the
    parent of a born/died process)."""
    out = {}
    for snap in snaps:
        for p in snap.values():
            out[p.pid] = p.comm
    return out


# --- probe orchestration ----------------------------------------------------


def resolve_netmaps(pids_by_ns):
    """For each network namespace, read /proc/<rep>/net/* ONCE (any pid in the
    ns sees the same tables) and merge into one inode->desc map per ns. Each
    file is read independently so a missing tcp6/udp6 (IPv6 off) doesn't drop
    the others. Input: {netns_id: [pids]}. Output: {netns_id: {inode: desc}}."""
    out = {}
    for ns, pids in pids_by_ns.items():
        # pick a representative whose net tables are readable
        rep = next((p for p in pids
                    if os.path.exists("%s/%d/net/tcp" % (PROC, p))), pids[0])
        base = "%s/%d/net" % (PROC, rep)
        netmap = {}
        for fn, parser in (
            ("tcp", lambda c: parse_net_tcp(c, False)),
            ("tcp6", lambda c: parse_net_tcp(c, True)),
            ("udp", lambda c: parse_net_udp(c, False)),
            ("udp6", lambda c: parse_net_udp(c, True)),
            ("unix", parse_net_unix),
        ):
            try:
                netmap.update(parser(_read(base + "/" + fn)))
            except OSError:
                pass
        out[ns] = netmap
    return out


def probe(nodes, cache, resolver=resolve_netmaps):
    """Fill cwd/exe/sockets_str for the given printed nodes. Sockets come from
    the cache when fd count matches and the entry is fresh; otherwise the node
    is grouped by netns and resolved in one batch. `resolver` is injectable."""
    # cwd/exe are cheap single readlinks -> always re-read.
    miss = []                       # (node, fdcount, inodes)
    pids_by_ns = {}
    for node in nodes:
        node.cwd, node.exe = read_links(node.pid)
        fdcount, inodes = fd_socket_inodes(node.pid)
        cached = cache.get(node.pid, node.starttime, fdcount)
        if cached is not None:
            node.sockets_str = cached
            continue
        ns = netns_of(node.pid)
        pids_by_ns.setdefault(ns, []).append(node.pid)
        miss.append((node, fdcount, inodes))

    netmaps = resolver(pids_by_ns) if pids_by_ns else {}
    for node, fdcount, inodes in miss:
        ns = netns_of(node.pid)
        summary = format_sockets(inodes, netmaps.get(ns, {}))
        node.sockets_str = summary
        cache.put(node.pid, node.starttime, fdcount, summary)


# --- pure core: path snapshot ('--path') ------------------------------------

PathHit = namedtuple("PathHit", "kind detail path")


def normalize_path_target(path):
    """Resolve a user-supplied path to an absolute, symlink-free subtree root for
    prefix matching."""
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def _strip_deleted_marker(path):
    marker = " (deleted)"
    if path and path.endswith(marker):
        return path[:-len(marker)]
    return path


def _path_is_in_subtree(path, root):
    """True when `path` resolves to `root` or a descendant. Pseudo-paths
    (socket:/pipe:/anon_inode:/memfd:), '?' and '' are never matches; a kernel
    ' (deleted)' marker is stripped before resolving."""
    if not path or path == "?":
        return False
    if path.startswith(("socket:", "pipe:", "anon_inode:", "memfd:")):
        return False
    clean = _strip_deleted_marker(path)
    try:
        real = os.path.realpath(clean)
    except OSError:
        real = os.path.abspath(clean)
    try:
        return os.path.commonpath([root, real]) == root
    except ValueError:        # not comparable (e.g. relative vs absolute)
        return False


def _fd_kind(pid, fd):
    """Label an fd by what it points at: 'fd:N dir', 'fd:N file', or bare 'fd:N'."""
    try:
        mode = os.stat("%s/%d/fd/%s" % (PROC, pid, fd)).st_mode
    except OSError:
        return "fd:%s" % fd
    if stat.S_ISDIR(mode):
        return "fd:%s dir" % fd
    if stat.S_ISREG(mode):
        return "fd:%s file" % fd
    return "fd:%s" % fd


def proc_path_hits(proc, root, readlink=os.readlink, listdir=os.listdir,
                   read_maps=None, fd_kind=None):
    """Every way `proc` currently touches `root`: its cwd, exe, open fds, and the
    first MAPS_MAX_PER_PROC mmap'd files under root. Returns a de-duplicated list
    of PathHit. The os/read callables are injectable for testing."""
    hits = []
    seen = set()

    def add(kind, detail, path):
        key = (kind, detail, path)
        if key not in seen:
            hits.append(PathHit(kind, detail, path))
            seen.add(key)

    for kind, path in (("cwd", proc.cwd), ("exe", proc.exe)):
        if _path_is_in_subtree(path, root):
            add(kind, "", path)

    try:
        fds = sorted(listdir("%s/%d/fd" % (PROC, proc.pid)), key=int)
    except (OSError, ValueError):
        fds = []
    for fd in fds:
        try:
            path = readlink("%s/%d/fd/%s" % (PROC, proc.pid, fd))
        except OSError:
            continue
        if _path_is_in_subtree(path, root):
            add((fd_kind or _fd_kind)(proc.pid, fd), "", path)

    if read_maps is None:
        read_maps = lambda pid: _read_text("%s/%d/maps" % (PROC, pid))
    maps_seen = 0
    for line in read_maps(proc.pid).splitlines():
        if maps_seen >= MAPS_MAX_PER_PROC:
            break
        fields = line.split(None, 5)
        if len(fields) < 6:
            continue
        path = fields[5]
        if path.startswith("/") and _path_is_in_subtree(path, root):
            add("mmap", "", path)
            maps_seen += 1
    return hits


def collect_path_hits(procs, root):
    """{pid: [PathHit]} for every process currently touching `root`. Fills each
    proc's cwd/exe (cheap single readlinks) as a side effect."""
    hits = {}
    for p in procs.values():
        p.cwd, p.exe = read_links(p.pid)
        phits = proc_path_hits(p, root)
        if phits:
            hits[p.pid] = phits
    return hits


def _format_path_hits(hits):
    """Join PathHits into one detail bit: 'kind:compressed/path  kind:…'."""
    bits = []
    for hit in hits:
        label = hit.kind + (":" + hit.detail if hit.detail else "")
        bits.append("%s:%s" % (label, compress_path(hit.path)))
    return "  ".join(bits)


# --- render -----------------------------------------------------------------

HOME = os.path.expanduser("~")


def _compress_basename(name):
    if len(name) <= PATH_MAX_BASENAME:
        return name
    k = PATH_BASENAME_KEEP
    return name[:k] + "..." + name[-k:]


def compress_path(path):
    """Shorten a cwd/exe for display: $HOME -> ~, elide the middle of deep
    paths (keep the first PATH_HEAD components + '..' + basename), and shorten
    an over-long basename to 'prefix...suffix'. A trailing ' (deleted)' marker
    (kernel-appended for a removed cwd) is preserved."""
    if not path or path == "?":
        return path
    marker = " (deleted)"
    suffix = ""
    if path.endswith(marker):
        path = path[:-len(marker)]
        suffix = marker
    if path.startswith(HOME):
        path = "~" + path[len(HOME):]
    parts = path.split("/")
    parts[-1] = _compress_basename(parts[-1])
    if len(parts) > PATH_HEAD + 1:
        parts = parts[:PATH_HEAD] + ["..", parts[-1]]
    return "/".join(parts) + suffix


def compress_cmdline(cmdline, width):
    """Fit a process cmdline into `width` while keeping the binary name and at
    least the start of its arguments. argv[0] is often a long absolute path
    (e.g. a bazelisk download dir), so blindly truncating shows only path and
    drops the real command — instead compress argv[0]'s path the way exe/cwd
    are compressed, and if that still leaves no room for an argument, fall back
    to the bare (and, if huge, shortened) basename before truncating."""
    if not cmdline or cmdline.startswith("["):
        return cmdline[:width]
    head, sep, rest = cmdline.partition(" ")
    argv0 = compress_path(head)
    if not sep:
        return argv0[:width]
    line = argv0 + " " + rest
    if len(line) <= width:
        return line
    base = _compress_basename(head.rsplit("/", 1)[-1])
    return (base + " " + rest)[:width]


def _tint_level(value, anchors):
    """Number of anchors `value` meets or exceeds -> a tint level (0..len).
    0 is the dim baseline; higher anchors are exponential, so the tint only
    strengthens by orders of magnitude. None/0 -> baseline."""
    if not value:
        return 0
    return sum(1 for a in anchors if value >= a)


def _cpu_bit(node, sysinfo):
    """('cpu 3.4% (2.5% avg)', level) when a current sample exists, else
    ('cpu 2.5% avg', …). avg = lifetime CPU rate (total cpu-time / time alive).
    The tint level tracks live load (current sample, or avg when none) against
    CPU_TINT_ANCHORS. Returns None when no rate is computable."""
    life = lifetime_secs(node.starttime, sysinfo.uptime, sysinfo.clk_tck)
    avg_frac = cpu_fraction(node.utime + node.stime, life, sysinfo.clk_tck)
    avg = fmt_pct(avg_frac)
    cur = fmt_pct(node.cpu_current)
    if avg is None:
        return None
    live = node.cpu_current if node.cpu_current is not None else avg_frac
    level = _tint_level(live, CPU_TINT_ANCHORS)
    if cur is not None:
        return ("cpu %s (%s avg)" % (cur, avg), level)
    return ("cpu %s avg" % avg, level)


def _compose_dim(bits, color, colormap=None):
    """Join Bits into one detail line. A Bit is (text, tint_level[, color_key]);
    by the model invariant a bit carries at most one of {tint_level>0,
    color_key}. Without color, plain text. With color, a bit whose color_key
    `colormap` resolves gets that identity hue; otherwise it gets its tint
    level's warm SGR (TINT_SGR), level 0 being the dim baseline - so a
    colormap-less call is byte-identical to the old heat-only line."""
    norm = [(b[0], b[1], b[2] if len(b) > 2 else None) for b in bits]
    if not color:
        return "  ".join(t for t, _, _ in norm)
    segs = []
    for text, lvl, key in norm:
        sgr = colormap.sgr(key) if (colormap is not None
                                    and key is not None) else None
        if sgr:
            segs.append("%s%s\x1b[0m" % (sgr, text))
        else:
            segs.append("\x1b[%sm%s\x1b[0m" % (TINT_SGR[lvl], text))
    return "\x1b[2m  \x1b[0m".join(segs)


def _detail(node, sysinfo=None, venv=None, path_hits=None, containers=None):
    """The dim detail Bits for one Proc: an optional leading container tag, then
    cwd/exe (unless --path already carries them), venv, sockets, cpu/rss/up,
    thread count, path hits last. Returns list[Bit] (possibly empty);
    composing/coloring is the renderer's job. cwd and exe carry a
    ('dir'/'cmd', raw_path) color_key so the renderer can hue them; the raw path -
    not the compressed display string - is the key. `containers` ({pid: label},
    only container-boundary pids) defaults None -> no tag -> byte-compat."""
    bits = []
    if containers and node.pid in containers:
        bits.append(("container:" + containers[node.pid], 0))
    if not path_hits:        # in --path mode the hits already carry cwd/exe/fd
        if node.cwd and node.cwd not in ("?", ""):
            bits.append(("cwd:" + compress_path(node.cwd), 0,
                         ("dir", node.cwd)))
        if node.exe and node.exe not in ("?", ""):
            bits.append(("exe:" + compress_path(node.exe), 0,
                         ("cmd", node.exe)))
    if venv:
        bits.append(("venv:" + compress_path(venv), 0, ("dir", venv)))
    if node.sockets_str:
        bits.append((node.sockets_str, 0))
    if sysinfo is not None:
        cpu = _cpu_bit(node, sysinfo)
        if cpu is not None:
            bits.append(cpu)
        rss = node.rss_pages * sysinfo.page_size
        if rss > 0:
            bits.append(("rss:" + fmt_bytes(rss),
                         _tint_level(rss, RSS_TINT_ANCHORS)))
        bits.append(("up:" + fmt_duration(
            lifetime_secs(node.starttime, sysinfo.uptime, sysinfo.clk_tck)), 0))
    if node.num_threads > 1:
        bits.append(("%d threads" % node.num_threads, 0))
    if path_hits:
        bits.append((_format_path_hits(path_hits), 0))
    return bits


def _visible_children(node, suppressed, transparent=frozenset()):
    """A node's kept, non-suppressed children. A `transparent` child (a cold
    helper) is skipped but *passed through*: its own visible children take its
    place, so a lifted hot node under it still reaches the view rather than
    being cut off. `transparent` empty -> byte-for-byte the old behaviour."""
    out = []
    for c in node.children:
        if not c.kept:
            continue
        if c.pid in transparent:
            out.extend(_visible_children(c, suppressed, transparent))
            continue
        if c.pid in suppressed:
            continue
        out.append(c)
    return out


def _group_label(members, width):
    return brace_summary([m.cmdline for m in members])[:width]


def _group_detail(members, sysinfo):
    """Aggregated detail Bits for a merged group: shared/braced cwd & exe,
    member pids, and cpu/rss/up ranges (when sysinfo is given). Returns
    list[Bit]; composing/coloring is the renderer's job."""
    bits = []   # (text, tint level)
    for attr, prefix in (("cwd", "cwd:"), ("exe", "exe:")):
        vals = [getattr(m, attr) for m in members
                if getattr(m, attr) and getattr(m, attr) not in ("?", "")]
        if vals:
            bits.append((prefix + brace_summary([compress_path(v)
                                                 for v in vals]), 0))
    pids = sorted(m.pid for m in members)
    extra = " +%d" % (len(pids) - GROUP_PIDS) if len(pids) > GROUP_PIDS else ""
    bits.append(("pids:" + " ".join(str(x) for x in pids[:GROUP_PIDS]) + extra,
                 0))
    if sysinfo is not None:
        avgs = [a for a in (
            cpu_fraction(m.utime + m.stime,
                         lifetime_secs(m.starttime, sysinfo.uptime,
                                       sysinfo.clk_tck), sysinfo.clk_tck)
            for m in members) if a is not None]
        if avgs:
            curs = [m.cpu_current for m in members if m.cpu_current is not None]
            # Tint tracks the group's combined live load (the whole ×N entry).
            live_total = sum(curs) if curs else sum(avgs)
            level = _tint_level(live_total, CPU_TINT_ANCHORS)
            if curs:
                bits.append(("cpu %s (%s avg)" % (range_str(curs, fmt_pct),
                                                  range_str(avgs, fmt_pct)),
                             level))
            else:
                bits.append(("cpu %s avg" % range_str(avgs, fmt_pct), level))
        rss = [m.rss_pages * sysinfo.page_size for m in members
               if m.rss_pages > 0]
        if rss:
            bits.append(("rss:" + range_str(rss, fmt_bytes),
                         _tint_level(sum(rss), RSS_TINT_ANCHORS)))
        lifes = [lifetime_secs(m.starttime, sysinfo.uptime, sysinfo.clk_tck)
                 for m in members]
        bits.append(("up:" + range_str(lifes, fmt_duration), 0))
    threads = max(m.num_threads for m in members)
    if threads > 1:
        bits.append(("%d threads" % threads, 0))
    return bits


# --- identity color: cartographic, protanomaly-safe ------------------------

# Four scatter-validated family hues (see ~/.claude/memory/color-vision.md). Gray
# is deliberately NOT a family: it reads as the terminal default fg, so an
# uncolored span already looks gray and means 'no family'. Order is the palette
# index 0..3.
COLOR_FAMILIES = ("#F2E34C", "#E69F00", "#2E9E6B", "#1565C0")  # yellow amber green blue
COLOR_DEPTH_STEP = 0.11        # OKLab L* added per directory level past the family
COLOR_L_CEILING = 0.93         # cap on lightening - above the lightest base (yellow
                               # L~0.90) so a base hue is never clamped, below white
COLOR_MAX_DEPTH = 2            # directory levels that still get a distinct shade


def _cbrt(x):
    return (x if x >= 0 else 0.0) ** (1.0 / 3.0)


def _srgb_to_linear(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c):
    c = 0.0 if c < 0 else (1.0 if c > 1 else c)
    v = c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
    return int(round(v * 255))


def _hex_to_oklab(h):
    """sRGB hex '#rrggbb' -> OKLab (L, a, b) (Ottosson 2020)."""
    r, g, b = (_srgb_to_linear(int(h[i:i + 2], 16)) for i in (1, 3, 5))
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = _cbrt(l), _cbrt(m), _cbrt(s)
    return (0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_)


def _oklab_to_rgb(L, a, b):
    """OKLab -> sRGB (r, g, b) ints, clamped to gamut."""
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    return (_linear_to_srgb(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
            _linear_to_srgb(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
            _linear_to_srgb(-0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s))


def family_rgb(family_index, depth):
    """Truecolor (r, g, b) for a family at directory `depth`: the base hue
    lightened toward (never to) white in OKLab, so the steps look perceptually
    even and deep paths still read as the same family."""
    L, a, b = _hex_to_oklab(COLOR_FAMILIES[family_index])
    L = min(COLOR_L_CEILING, L + COLOR_DEPTH_STEP * min(depth, COLOR_MAX_DEPTH))
    return _oklab_to_rgb(L, a, b)


def _canon_components(path):
    """Absolute path -> tuple of components for color keying, ' (deleted)'
    stripped. Pseudo/unknown paths -> () (never colored)."""
    if (not path or path == "?"
            or path.startswith(("socket:", "pipe:", "anon_inode:", "memfd:"))):
        return ()
    return tuple(c for c in _strip_deleted_marker(path).split("/") if c)


def detect_anchor(component_lists, override=None):
    """The shared prefix to strip before coloring. With override, that path's
    components. Otherwise the deepest component-prefix shared by >= half of the
    valid paths AND by >= 2, never consuming a path's last component (so a family
    component always remains). < 2 valid paths -> () (nothing anchored)."""
    if override is not None:
        return tuple(c for c in override.split("/") if c)
    valid = [c for c in component_lists if c]
    if len(valid) < 2:
        return ()
    need = max(2, len(valid) // 2 + 1)   # strict majority - a 50/50 split must
    #                                      stop at the common parent, not descend
    #                                      into whichever branch insertion-order
    #                                      happened to surface first
    best = ()
    for d in range(1, max(len(c) for c in valid)):
        cnt = Counter(c[:d] for c in valid if len(c) > d)
        if cnt:
            pref, k = cnt.most_common(1)[0]
            if k >= need:
                best = pref
    return best


class ColorMap:
    """Resolves a ('dir'|'cmd', raw_path) key to a truecolor SGR opener. The
    family is the first path component past the anchor; every family gets a hue
    (the four cycle by weight rank), anything outside the anchor stays default fg.
    Depth past the family lightens the hue."""

    def __init__(self, anchor, families):
        self.anchor = anchor            # tuple of components
        self.families = families        # {component: family_index 0..3}

    def family_of(self, path):
        comps = _canon_components(path)
        a = len(self.anchor)
        if len(comps) <= a or comps[:a] != self.anchor:
            return None, 0
        fam = comps[a]
        if fam not in self.families:
            return None, 0
        return self.families[fam], len(comps) - a - 1

    def sgr(self, key):
        if not key:
            return None
        fam, depth = self.family_of(key[1])
        if fam is None:
            return None
        return "\x1b[38;2;%d;%d;%dm" % family_rgb(fam, depth)


def build_colormap(paths_with_weight, anchor_override=None):
    """paths_with_weight: iterable of (raw_path, weight). Detect the anchor, then
    give every family a hue, cycling the four by weight rank (visual mass)."""
    comps = [(_canon_components(p), w) for p, w in paths_with_weight]
    if anchor_override is None and sum(1 for c, _ in comps if c) < 2:
        return ColorMap((), {})         # < 2 real paths -> nothing to compare, nothing colored
    anchor = detect_anchor([c for c, _ in comps], anchor_override)
    a = len(anchor)
    weight = Counter()
    for c, w in comps:
        if len(c) > a and c[:a] == anchor:
            weight[c[a]] += w
    # Round-robin: every family under the anchor gets a hue, cycling the four by
    # weight rank, so the most projects possible are coloured. Collisions land on
    # the least-frequent pairs and the printed dir text still disambiguates.
    ranked = sorted(weight.items(), key=lambda kv: (-kv[1], kv[0]))
    return ColorMap(anchor, {f: i % len(COLOR_FAMILIES)
                             for i, (f, _) in enumerate(ranked)})


def _truecolor():
    """24-bit terminal? The scatter-validated hues need true color; on a 16/256
    terminal identity color is disabled (heat-tint still applies)."""
    return os.environ.get("COLORTERM", "") in ("truecolor", "24bit")


def make_colormap(procs, color, anchor_override=None):
    """Build the identity ColorMap from the shown procs' cwds, or None when color
    is off or the terminal isn't truecolor."""
    if not (color and _truecolor()):
        return None
    return build_colormap([(p.cwd, 1) for p in procs if p.cwd],
                          anchor_override=anchor_override)


def colormap_for(args, procs, color):
    """The ColorMap for a run, honouring --color-anchor. Shared by main/--path."""
    override = normalize_path_target(args.color_anchor) if args.color_anchor else None
    return make_colormap(procs, color, anchor_override=override)


def anchor_note(colormap, color):
    """The one-line legend naming the active colour anchor - data, shown whenever
    identity colour is on (both the tree and --path). Empty without an anchor."""
    if not (colormap and colormap.anchor):
        return []
    note = ("color: directories & commands are hued by project under /%s "
            "(dim = no project / outside anchor; warm = cpu/rss heat)"
            % "/".join(colormap.anchor))
    return [("\x1b[2m%s\x1b[0m" % note) if color else note, ""]


@dataclass
class ViewNode:
    """One display-tree node: a single Proc or a merged Group, children already
    grouped. The view layer produces these (pure - no /proc IO, no ANSI); the
    renderer turns them into lines. head_label is the colorable span (cmdline or
    group brace) the renderer prefixes with connector/badge/head_pre."""
    kind: str                       # "proc" | "group"
    payload: object                 # Proc | Group
    badge: str = ""                 # "[xxx] " or ""
    head_pre: str = ""              # "<pid> " (proc) or "×N " (group) - never colored
    head_label: str = ""            # cmdline or group brace - gets identity hue
    head_key: object = None         # ('dir'|'cmd', raw_path) hue key for head_label
    detail_bits: list = field(default_factory=list)
    collapse_note: object = None    # proc-only
    helper_note: object = None      # list[str] idle-helper note(s), on the anchor
    children: list = field(default_factory=list)
    sort_key: tuple = ()


def _dir_bucket(cwd):
    """The directory a proc is filed under in the dir axis: its canonical cwd,
    or one shared 'unknown' bucket for '?'/'' so they never mix with real dirs."""
    if not cwd or cwd in ("?", ""):
        return "unknown"
    return _strip_deleted_marker(cwd)


def build_view(roots, suppressed, width=CMD_WIDTH, sysinfo=None,
               dedup_min=None, never_merge=frozenset(), categories=None,
               venv_map=None, axis="tree", key_fn=None, pool_global=False,
               expand_all=False, path_hits=None,
               transparent=frozenset(), lifted=frozenset(), notes=None,
               containers=None):
    """Turn kept Procs into a display tree (list[ViewNode]). Pure: no /proc IO,
    no ANSI. Merging for the chosen axis is fully resolved here. axis="tree" is
    the per-parent grouping render() exposes; "session" roots each session at its
    leader; "consolidated" folds signature-equal sessions (pool_global flattens
    to a global ×N-by-kind list); "dir" buckets by directory. expand_all drops
    the collapse/suppressed set. transparent/lifted/notes come from
    plan_helper_hiding: transparent members pass through the visible-children
    walk, lifted members get a '↑ ' marker, notes attach a trailing line to their
    anchor. merging is off when dedup_min is falsy."""
    if expand_all:
        suppressed = frozenset()
        transparent = frozenset()
        lifted = frozenset()
        notes = None
    elif lifted:
        # Lifting overrides collapse-suppression: a hot helper that collapse()
        # also folded into `suppressed` must still surface (otherwise the
        # _visible_children suppressed-filter would drop it and the hot node
        # would vanish rather than lift).
        suppressed = set(suppressed) - lifted

    def badge(pid):
        if categories and pid in categories:
            return "[%s] " % categories[pid].badge
        return ""

    def node_of(item):
        if isinstance(item, Group):
            cwds = {m.cwd for m in item.members}
            return ViewNode(
                kind="group", payload=item,
                badge=badge(item.members[0].pid),
                head_pre="×%d " % len(item.members),
                head_label=_group_label(item.members, width),
                # a uniform-cwd group takes that project's hue; a mixed-cwd one
                # stays uncoloured rather than picking a member's hue at random.
                head_key=(("dir", item.members[0].cwd) if len(cwds) == 1 else None),
                detail_bits=_group_detail(item.members, sysinfo))
        venv = venv_map.get(item.pid) if venv_map else None
        return ViewNode(
            kind="proc", payload=item,
            badge=badge(item.pid),
            # a lifted hot helper surfacing through a hidden parent gets '↑ '.
            head_pre=("↑ " if item.pid in lifted else "") + "%d " % item.pid,
            head_label=compress_cmdline(item.cmdline, width),
            head_key=("dir", item.cwd),     # the command takes its project's hue
            detail_bits=_detail(item, sysinfo, venv=venv, containers=containers),
            collapse_note=(item.collapse_note
                           if (not expand_all and item.collapsed
                               and item.collapse_note) else None),
            helper_note=(notes.get(item.pid) if notes else None))

    def make_nodes(items):
        nodes = []
        for item in items:
            node = node_of(item)
            if isinstance(item, Group):
                kids = [c for m in item.members
                        for c in _visible_children(m, suppressed, transparent)]
            else:
                kids = _visible_children(item, suppressed, transparent)
            node.children = make_nodes(
                group_siblings(kids, dedup_min, never_merge, key_fn))
            nodes.append(node)
        return nodes

    if axis == "session":
        # Each session is its own node; children merge within it. Roots are found
        # through suppressed plumbing, but their subtrees still honour it.
        return make_nodes(find_session_roots(roots))

    if axis == "consolidated":
        sroots = find_session_roots(roots)
        if pool_global:
            # One flat ×N-by-kind list across every session - a globally-pooled
            # entry has no single session header to nest under.
            procs = [p for r in sroots
                     for p in _session_procs(r, suppressed, transparent)]
            return [node_of(it) for it in
                    group_siblings(procs, dedup_min, never_merge, key_fn)]
        return make_nodes(consolidate_sessions(
            sroots, categories, suppressed, dedup_min, never_merge,
            transparent))

    if axis == "dir":
        procs = [p for r in find_session_roots(roots)
                 for p in _session_procs(r, suppressed, transparent)]
        buckets = {}
        order = []
        for p in procs:
            d = _dir_bucket(p.cwd)
            if d not in buckets:
                buckets[d] = []
                order.append(d)
            buckets[d].append(p)
        nodes = []
        for d in sorted(order):
            items = group_siblings(buckets[d], dedup_min, never_merge, key_fn)
            nodes.append(ViewNode(
                kind="dir", payload=None, head_label=compress_path(d),
                head_key=(("dir", d) if d != "unknown" else None),
                children=[node_of(it) for it in items]))
        return nodes

    if axis == "flat":
        # --path: one priority-ordered line per toucher (roots = the hit procs),
        # its path hits carried as the trailing detail bits. No tree, no merging.
        def flat_node(p):
            venv = venv_map.get(p.pid) if venv_map else None
            ph = path_hits.get(p.pid) if path_hits else None
            prio = (categories[p.pid].priority
                    if categories and p.pid in categories else 0)
            return ViewNode(
                kind="proc", payload=p, badge=badge(p.pid),
                head_pre="%d " % p.pid,
                head_label=compress_cmdline(p.cmdline, width),
                head_key=("dir", p.cwd),
                detail_bits=_detail(p, sysinfo, venv=venv, path_hits=ph,
                                    containers=containers),
                sort_key=(-prio, p.pid))
        return sorted((flat_node(p) for p in roots), key=lambda n: n.sort_key)

    return make_nodes(
        group_siblings(list(roots), dedup_min, never_merge, key_fn))


def emit_lines(nodes, color=False, colormap=None):
    """Walk a display tree into ascii-tree lines - the only place connectors and
    ANSI are produced. With a colormap, head_label and key-bearing detail bits
    get their identity hue; without one this is the old heat-only tree, byte for
    byte."""
    lines = []

    def hue(text, key):
        if color and colormap is not None and key is not None:
            sgr = colormap.sgr(key)
            if sgr:
                return "%s%s\x1b[0m" % (sgr, text)
        return text

    def walk(items, prefix):
        for i, node in enumerate(items):
            is_last = i == len(items) - 1
            if prefix == "":
                # Top-level nodes are independent roots (sessions, dir buckets,
                # touchers) - render each bare, no dangling connector.
                connector, child_prefix = "", "   "
            else:
                connector = "└─ " if is_last else "├─ "
                child_prefix = prefix + ("   " if is_last else "│  ")
            lines.append("%s%s%s%s%s" % (
                prefix, connector, node.badge, node.head_pre,
                hue(node.head_label, node.head_key)))
            if node.detail_bits:
                lines.append(child_prefix
                             + _compose_dim(node.detail_bits, color, colormap))
            walk(node.children, child_prefix)
            if node.collapse_note:
                lines.append(child_prefix + node.collapse_note)
            for note in (node.helper_note or []):
                lines.append(child_prefix
                             + (("\x1b[2m%s\x1b[0m" % note) if color else note))

    walk(nodes, "")
    return lines


def render(roots, suppressed, width=CMD_WIDTH, color=None, sysinfo=None,
           dedup_min=None, never_merge=frozenset(), categories=None,
           venv_map=None):
    """Render kept Procs as an ascii tree (the tree axis, no identity color).
    Thin wrapper over build_view+emit_lines, kept for direct callers/tests.
    categories ({pid: Category}) add a '[badge]' prefix; venv_map ({pid: venv})
    adds a 'venv:' bit; with dedup_min, near-identical siblings merge into one
    ×N entry recursing over the union of members' children."""
    if color is None:
        color = False
    nodes = build_view(roots, suppressed, width=width, sysinfo=sysinfo,
                       dedup_min=dedup_min, never_merge=never_merge,
                       categories=categories, venv_map=venv_map, axis="tree")
    return emit_lines(nodes, color=color, colormap=None)


def glossary(color):
    """A short legend printed at the head of the output explaining the
    annotations (notably what '+N est' means). Returns a list of lines."""
    lines = [
        "psf — interesting process subtrees only; the dim line under each "
        "process annotates it.",
        "  badges:  [usr] user-session  [bld] build-daemon  [cmp] compile-worker"
        "  [inf] infrastructure  [sys] system-service  [mis] misc",
        "  sockets: LISTEN :PORT = listening   "
        "+N est = N established TCP connections   unix:PATH = named socket",
        "  stats:   cpu X% (Y avg) = recent / lifetime-average CPU   "
        "rss = resident memory   up = time since start",
        "  groups:  ×N = N near-identical siblings merged (pids/ranges on the "
        "detail line)   lifecycle = procs born/died during the sample window",
    ]
    if color:
        lines = ["\x1b[2m%s\x1b[0m" % ln for ln in lines]
    return lines


def psf_header(procs):
    """Two header lines: a system summary (cores / load / mem / swap) and a task
    breakdown (proc count + running/sleeping/zombie). Reads loadavg and meminfo
    live; `procs` supplies the counts. Returns a list of two strings."""
    load = read_loadavg()
    n_run, n_sleep, n_zombie = count_states(procs)
    mem = parse_meminfo(_read_text("/proc/meminfo"))
    parts = ["psf — %d cores" % cores_count()]
    if load[0] is not None:
        parts.append("load %.2f/%.2f/%.2f" % load)
    mem_total = mem.get("mem_total")
    if mem_total is not None:
        used = mem_total - (mem.get("free") or 0) - (mem.get("buff") or 0) \
            - (mem.get("cache") or 0)
        parts.append("Mem %s/%s" % (fmt_bytes(used), fmt_bytes(mem_total)))
    swap_total = mem.get("swap_total", 0)
    if swap_total:
        sw_used = swap_total - (mem.get("swap_free") or 0)
        parts.append("Swap %s/%s" % (fmt_bytes(sw_used), fmt_bytes(swap_total)))
    task = ["%d procs" % len(procs)]
    if n_run:
        task.append("%d run" % n_run)
    if n_sleep:
        task.append("%d sleep" % n_sleep)
    if n_zombie:
        task.append("%d zombie" % n_zombie)
    return ["  ".join(parts), "(%s)" % ", ".join(task)]


# --- CLI --------------------------------------------------------------------


def cache_path():
    """Resolve the cache file. Under sudo, write to the invoking user's cache
    dir (via SUDO_USER) rather than root's."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    sudo_user = os.environ.get("SUDO_USER")
    if xdg:
        base = xdg
    elif sudo_user:
        import pwd
        base = os.path.join(pwd.getpwnam(sudo_user).pw_dir, ".cache")
    else:
        base = os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "psf", "cache.json")


def _run_path(args):
    """--path mode: every process touching the target subtree, one per node,
    highest-priority category first, with its path hits as the detail line.
    Bypasses the interesting-subtree tree, collapse, dedup, lifecycle and the
    socket cache - it answers one question, 'what is touching this path'."""
    procs = scan()
    sysinfo = SysInfo(clk_tck=CLK_TCK, page_size=PAGE_SIZE, uptime=read_uptime())
    root = normalize_path_target(args.path)
    hits = collect_path_hits(procs, root)        # fills cwd/exe; {pid: [PathHit]}
    color = sys.stdout.isatty() and not args.no_color
    venv_map = {pid: detect_venv(procs[pid]) for pid in hits
                if procs[pid].comm.startswith("python")}
    categories = {pid: classify(procs[pid],
                                venv_resolver=lambda q: venv_map.get(q.pid))
                  for pid in hits}

    hitprocs = [procs[pid] for pid in hits]
    containers = build_container_map(
        hitprocs, os.path.dirname(cache_path()), time.time(),
        no_cache=args.no_cache)
    colormap = colormap_for(args, hitprocs, color)
    out = []
    if not args.no_header:
        out += psf_header(procs) + [""]
    out.append("touching %s — %d procs" % (compress_path(root), len(hits)))
    out.append("")
    out += anchor_note(colormap, color)
    nodes = build_view(hitprocs, suppressed=set(), width=args.width,
                       sysinfo=sysinfo, categories=categories,
                       venv_map=venv_map, axis="flat", path_hits=hits,
                       containers=containers)
    out += emit_lines(nodes, color=color, colormap=colormap)
    print("\n".join(out))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Focused process snapshot.")
    ap.add_argument("-w", "--width", type=int, default=CMD_WIDTH,
                    help="cmdline chars per process (default %d)" % CMD_WIDTH)
    ap.add_argument("-t", "--threshold", type=int, default=COLLAPSE_THRESHOLD,
                    help="collapse subtrees with more kept descendants")
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore and do not write the socket cache")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--no-glossary", action="store_true",
                    help="suppress the legend printed at the head of output")
    ap.add_argument("-s", "--sample-interval", type=float,
                    default=SAMPLE_INTERVAL,
                    help="seconds slept to measure current CPU (default %.2g; "
                         "0 disables the current-CPU sample)" % SAMPLE_INTERVAL)
    ap.add_argument("-g", "--group",
                    choices=("session", "consolidated", "dir"),
                    default="session",
                    help="grouping axis: session (default) roots each session at "
                         "its leader; consolidated folds alike sessions; dir "
                         "buckets by directory")
    ap.add_argument("--pool-global", action="store_true",
                    help="with --group consolidated, one flat ×N-by-kind list "
                         "across every session")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="do not merge near-identical siblings (list each)")
    ap.add_argument("--expand-all", action="store_true",
                    help="expand everything: no merging and no subtree collapse")
    ap.add_argument("--color-anchor", metavar="PATH",
                    help="strip PATH as the color anchor instead of auto-detecting "
                         "the shared prefix")
    # Back-compat aliases, folded into --verbose/the dedup knob in one place.
    ap.add_argument("--no-dedup", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--dedup-min", type=int, default=DEDUP_MIN,
                    help=argparse.SUPPRESS)
    ap.add_argument("--no-lifecycle", action="store_true",
                    help="suppress the born/died section")
    ap.add_argument("--no-header", action="store_true",
                    help="suppress the system summary header")
    ap.add_argument("--path", metavar="PATH",
                    help="instead of the tree, list every process whose "
                         "cwd/exe/open-fd/mmap touches PATH or its subtree")
    ap.add_argument("target", nargs="?", metavar="TARGET",
                    help="shorthand: a path (psf ., psf ~/proj) lists what "
                         "touches it; or a mode - tree (bigger, less-collapsed "
                         "session), session, consolidated, dir")
    args = ap.parse_args(argv)

    # A positional TARGET is convenience sugar: 'tree' is the session view with
    # collapse relaxed, an axis name picks the mode, anything else is a --path
    # target (so `psf .` and `psf ~/proj` just work). A bare word that names an
    # axis wins over a like-named directory - use --path for that dir.
    if args.target is not None:
        if args.target == "tree":
            args.group = "session"
            if args.threshold == COLLAPSE_THRESHOLD:
                args.threshold = TREE_THRESHOLD
        elif args.target in ("session", "consolidated", "dir"):
            args.group = args.target
        elif args.path is None:
            args.path = args.target
        else:
            ap.error("pass a path as the positional OR --path, not both")

    if args.group == "dir" and args.path:
        ap.error("--group dir is meaningless under --path (the path already "
                 "scopes a directory)")
    if args.pool_global and args.group != "consolidated":
        ap.error("--pool-global requires --group consolidated")

    if args.path:
        _run_path(args)
        return

    s_a = scan()
    t_a = time.monotonic()
    roots = build_tree(s_a)
    select(s_a, DEFAULT_MATCHERS)
    suppressed = collapse(s_a, threshold=args.threshold)
    visible_roots = [r for r in roots if r.kept]
    # The session view re-roots past suppressed plumbing, so the set to
    # probe/classify/sample is the session-printed set, not collect_printed's
    # (which stops at suppressed and would skip a claude whose shell collapsed).
    printed = collect_session_printed(
        visible_roots, frozenset() if args.expand_all else suppressed)

    born, died, s_b, dt = [], [], None, 0.0
    if args.sample_interval > 0:
        time.sleep(args.sample_interval)
        s_b = scan()
        dt = time.monotonic() - t_a
        for n in printed:                      # current CPU over the same window
            other = s_b.get(n.pid)
            if other is not None and other.starttime == n.starttime:
                n.cpu_current = cpu_fraction(
                    (other.utime + other.stime) - (n.utime + n.stime),
                    dt, CLK_TCK)
        born, died = diff_snapshots(s_a, s_b)

    if args.no_cache:
        cache = Cache(os.devnull, boot_id="", now=time.time())
    else:
        cache = Cache(cache_path(), boot_id=read_boot_id(), now=time.time())
    probe(printed, cache)
    if not args.no_cache:
        cache.save(live_keys={(p.pid, p.starttime) for p in s_a.values()})

    color = sys.stdout.isatty() and not args.no_color
    sysinfo = SysInfo(clk_tck=CLK_TCK, page_size=PAGE_SIZE, uptime=read_uptime())
    merge_off = args.no_dedup or args.verbose or args.expand_all
    dedup_min = None if merge_off else args.dedup_min

    # Label the printed nodes: a venv for python procs, then a category badge for
    # each (the venv promotes an interactive python from compile-worker to
    # user-session — see classify()).
    venv_map = {n.pid: detect_venv(n) for n in printed
                if n.comm.startswith("python")}
    categories = {n.pid: classify(n, venv_resolver=lambda q: venv_map.get(q.pid))
                  for n in printed}

    colormap = colormap_for(args, printed, color)

    # Idle helper hiding runs AFTER sampling/classification: current CPU is set
    # and every helper carries a category, so the nearest-visible-ancestor guard
    # and the hot/cold split are decidable. build_view zeroes these under
    # --expand-all, so the full chains reappear there.
    transparent, lifted, notes = plan_helper_hiding(
        visible_roots, categories, sysinfo)

    # Tag each container's topmost printed proc. Id comes free from the cgroup;
    # the friendly compose name is a best-effort, cached `docker ps` that
    # degrades to the id (build_container_map only shells out if something is
    # actually containerized).
    containers = build_container_map(
        printed, os.path.dirname(cache_path()), time.time(),
        no_cache=args.no_cache)

    out = []
    if not args.no_header:
        out += psf_header(s_a) + [""]
    if not args.no_glossary:
        out += glossary(color) + [""]
    out += anchor_note(colormap, color)
    nodes = build_view(visible_roots, suppressed, width=args.width,
                       sysinfo=sysinfo, dedup_min=dedup_min,
                       never_merge=NEVER_MERGE, categories=categories,
                       venv_map=venv_map, axis=args.group,
                       pool_global=args.pool_global, expand_all=args.expand_all,
                       key_fn=lambda p: merge_key(p, categories),
                       transparent=transparent, lifted=lifted, notes=notes,
                       containers=containers)
    out += emit_lines(nodes, color=color, colormap=colormap)
    if s_b is not None and not args.no_lifecycle:
        section = format_lifecycle(born, died, _parents_map(s_a, s_b),
                                   sysinfo, dt, color=color)
        if section:
            out += [""] + section
    print("\n".join(out))

    hidden = sum(1 for p in s_a.values() if not p.kept)
    kthreads = sum(1 for p in s_a.values() if p.ppid == 2 or p.pid == 2)
    sys.stderr.write("(hidden: %d procs, %d kernel threads)\n"
                     % (hidden, kthreads))


if __name__ == "__main__":
    main()
