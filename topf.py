#!/usr/bin/env python3
"""topf - focused live process viewer (windowed CPU).

A top-like, full-screen, continuously-sampling viewer that shows only the
*interesting* process subtrees: those matched by comm/cmdline (bazel, ssh
sessions, tmux, claude) AND those that are interesting because they are heavy
(promoted by windowed CPU or RSS). Each node is annotated with the start of its
command line, a summarized cwd, the executing binary, open ports/sockets, and
per-window CPU / RSS / uptime.

Deep-probes only the nodes it prints, and caches the expensive socket analysis
across frames (keyed by (pid, starttime), validated by fd-count + TTL).

With stdout piped or --once, prints a single plain frame (the old psf
behaviour). Run under sudo/root to see other users' processes.
"""
import argparse
import json
import math
import os
import re
import select as _select      # stdlib selector; avoid clash with select() below
import sys
import termios
import time
import tty
from collections import Counter, namedtuple
from dataclasses import dataclass, field


# --- config -----------------------------------------------------------------

CMD_WIDTH = 50            # chars of cmdline shown per process
COLLAPSE_THRESHOLD = 90   # kept-descendant count above which a subtree collapses
CACHE_TTL = 30            # seconds before a cached socket entry is re-probed
REPR_COMMS = 4            # distinct comms named in a collapse summary
SAMPLE_INTERVAL = 0.2     # seconds slept to measure current CPU (0 disables)
PATH_HEAD = 2             # leading path components kept when compressing
PATH_MAX_BASENAME = 30    # basename length above which it is itself shortened
PATH_BASENAME_KEEP = 6    # chars kept each side of '...' in a long basename
DEDUP_MIN = 3             # min identical siblings to merge into one ×N group
NEVER_MERGE = frozenset({"qemu", "claude"})   # comms never grouped, even when identical
GROUP_PIDS = 8            # member pids listed on a group's detail line
LIFECYCLE_MAX = 40        # max born (and max died) comm-groups listed
# Graduated tint for the cpu/rss detail bits. A value's level = how many of its
# (exponential) anchors it clears; the level indexes TINT_SGR. Level 0 is the
# dim baseline shared by the rest of the line; higher levels add a faint warm
# tint, then full color, then bold red so heavy consumers pop. CPU anchors are
# in cores (1.0 == one full core / 100%, independent of how many cores exist).
TINT_SGR = ("2", "2;33", "33", "1;31")          # dim, dim-yellow, yellow, bold-red
FOCUS_SPARK_SAMPLES = 40    # max recent subtree-CPU samples kept per row for the
                            # cursor-row sparkline (~the visible history window)
RSS_TINT_ANCHORS = (100 * 1024**2, 1024**3, 5 * 1024**3)   # 100M, 1G, 5G
CPU_TINT_ANCHORS = (0.10, 1.0, 4.0)                        # 10%, 100%, 400%

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

DEFAULT_WINDOWS = (2.0, 10.0, 60.0)   # CPU window seconds (shortest..longest)
PROMOTE_LEVEL = 2         # tint-anchor level required to promote (>= 1.0 core / >= 1G)
RSS_GATE_LEVEL = 1        # longest-window CPU floor for RSS-only promotion (~10%)
BREAKOUT_MAX = 5          # max hot procs that poke through a collapsed subtree
REFRESH_INTERVAL = 1.0    # default sample == redraw cadence (seconds)

# vmstat pane: columns (key, header, kind), kind in {int, bytes, bps, count, pct}.
# si/so are dropped when swap is off; the four cpu cols are us/sy/id/wa (the
# "rest" — nice already folded into us, plus irq/steal/guest — is not shown).
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
VMSTAT_GUTTER = "vmstat"
MIN_ROWS_FOR_VMSTAT = 18      # terminal rows below which the pane is hidden
MIN_COLS_FOR_VMSTAT = 60      # terminal cols below which the pane is hidden
MIN_TREE_ROWS = 5             # tree region never shrinks below this for the pane
MIN_VMSTAT_SAMPLE_ROWS = 3    # fewer pane sample rows than this -> hide the pane
VMSTAT_ROWS_DEFAULT = 12      # default cap on pane sample rows

# disk pane: mount-point usage, similar layout to vmstat.
DISK_PANE_GUTTER = "diskfs"
MIN_ROWS_FOR_DISK = 18       # terminal rows below which the disk pane hides
MIN_COLS_FOR_DISK = 60       # terminal cols below which the disk pane hides
DISK_ROWS_DEFAULT = 10       # default cap on mount rows
DISK_USAGE_WARN_PCT = 80     # pct above which a mount gets warm tint
DISK_USAGE_CRIT_PCT = 95     # pct above which a mount gets red tint

# System constants, read once. CLK_TCK converts stat jiffies -> seconds;
# PAGE_SIZE converts stat rss (in pages) -> bytes.
CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")

# Resolved-once view of system state needed to turn raw counters into rates.
SysInfo = namedtuple("SysInfo", "clk_tck page_size uptime cores")

# One /proc/PID/stat row, only the fields we use.
Stat = namedtuple("Stat", "comm state ppid num_threads starttime "
                          "utime stime cutime cstime rss_pages")

# A merged set of >= DEDUP_MIN near-identical sibling Procs.
Group = namedtuple("Group", "members")

# One vmstat sample: raw /proc counters at monotonic time t. Any field may be
# None if /proc lacked it; rates are deltas between adjacent samples.
VmstatSample = namedtuple("VmstatSample",
    "t procs_running procs_blocked cpu_user cpu_nice cpu_system cpu_idle "
    "cpu_iowait cpu_total intr ctxt pgpgin pgpgout pswpin pswpout rx tx "
    "free buff cache swap_total swap_free mem_total")

# One mount-point disk usage row.
MountInfo = namedtuple("MountInfo",
    "mount_point device fstype total used avail pct")

# One rendered tree line + its selection metadata. Head rows (Proc/Group) are
# selectable; detail/collapse-note rows are non-selectable continuation lines.
# `focus` is an optional bright "zoomed-in" replacement string shown in place of
# a detail row's text when the cursor sits on its head row (item_id matches).
Row = namedtuple("Row", "text item_id expandable selectable focus",
                 defaults=(None,))

# Sentinel parent id for the top-level sibling set (see group_id / build_rows).
ROOT_ID = ("root",)

# Each matcher: (label, target, regex) where target is "comm" or "cmdline".
DEFAULT_MATCHERS = [
    ("bazel", "comm", re.compile(r"^bazel")),
    ("bazel", "cmdline", re.compile(r"\bbazel\(")),
    ("qemu-vp", "comm", re.compile(r"qemu-vp")),
    ("qemu-vp", "cmdline", re.compile(r"\bqemu-vp\b")),
    ("qemu", "comm", re.compile(r"qemu")),
    ("qemu", "cmdline", re.compile(r"\bqemu\b")),
    ("sshd", "cmdline", re.compile(r"^sshd: ")),
    ("tmux", "comm", re.compile(r"^tmux")),
    ("claude", "comm", re.compile(r"claude")),
    ("claude", "cmdline", re.compile(r"\bclaude\b")),
]

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
    cutime: int = 0                 # reaped children's user jiffies (field 16)
    cstime: int = 0                 # reaped children's system jiffies (field 17)
    rss_pages: int = 0              # resident pages (stat field 24)
    cpu_windows: list = None        # per-window CPU rate (cores), aligned to windows
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
    # field15=stime, field16=cutime, field17=cstime (cpu of reaped children),
    # field20=num_threads, field22=starttime, field24=rss.
    return Stat(
        comm=comm,
        state=rest[0],
        ppid=int(rest[1]),
        num_threads=int(rest[17]),
        starttime=int(rest[19]),
        utime=int(rest[11]),
        stime=int(rest[12]),
        cutime=int(rest[13]),
        cstime=int(rest[14]),
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


def proc_id(proc):
    """Stable identity for a process row: (pid, starttime) survives re-sorting;
    starttime distinguishes a reused pid."""
    return ("p", proc.pid, proc.starttime)


def group_id(parent_id, comm, exe):
    """Stable identity for a merged group row, qualified by its parent so the
    same (comm, exe) under different parents are distinct."""
    return ("g", parent_id, comm, exe)


def is_promoted(proc, page_size, promote_level, rss_needs_cpu, is_kthread):
    """A process is promoted (interesting because heavy) when it clears
    tint-anchor level >= promote_level on any CPU window, or (non-kthreads only)
    on RSS. RSS-only promotion is gated: it also requires the longest window's
    CPU to clear RSS_GATE_LEVEL unless rss_needs_cpu is False. Kernel threads
    promote by CPU alone (they have no meaningful RSS)."""
    cpu_level = max((_tint_level(f, CPU_TINT_ANCHORS)
                     for f in proc.cpu_windows if f is not None), default=0)
    if cpu_level >= promote_level:
        return True
    if is_kthread:
        return False
    rss = proc.rss_pages * page_size
    if _tint_level(rss, RSS_TINT_ANCHORS) >= promote_level:
        if not rss_needs_cpu:
            return True
        longest = proc.cpu_windows[-1] if proc.cpu_windows else None
        return _tint_level(longest, CPU_TINT_ANCHORS) >= RSS_GATE_LEVEL
    return False


def select(procs, matchers, page_size, promote_level, rss_needs_cpu):
    """Mark .interesting and .kept. Interesting = matched (bazel/ssh/tmux/claude)
    OR resource-promoted (heavy CPU/RSS). Kept = interesting + their descendants
    + their ancestors (so the tree stays rooted). Kernel-thread subtrees (under
    pid 2) are never matched, but ARE promotable when heavy (CPU only)."""
    kthreadd = procs.get(2)
    kthread_pids = set()
    if kthreadd is not None:
        kthread_pids = {2} | {d.pid for d in _descendants(kthreadd)}

    for p in procs.values():
        is_kthread = p.pid in kthread_pids
        matched = (not is_kthread) and is_interesting(p, matchers)
        promoted = is_promoted(p, page_size, promote_level, rss_needs_cpu,
                               is_kthread)
        p.interesting = matched or promoted
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


def group_siblings(procs, dedup_min, never_merge):
    """Partition a node's visible sibling Procs by (comm, exe) into a render
    list. A partition with >= dedup_min members whose comm is not in
    never_merge becomes a Group; everything else stays an individual Proc.
    dedup_min falsy (None/0) disables grouping. Items are ordered by their
    smallest pid so output is stable."""
    if not dedup_min:
        return list(procs)
    buckets = {}
    order = []
    for p in procs:
        key = (p.comm, p.exe)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(p)
    items = []
    for key in order:
        members = buckets[key]
        if len(members) >= dedup_min and key[0] not in never_merge:
            items.append(Group(members=members))
        else:
            items.extend(members)
    items.sort(key=lambda it: min(m.pid for m in it.members)
               if isinstance(it, Group) else it.pid)
    return items


# --- pure core: collapse ----------------------------------------------------


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


def find_breakouts(procs, suppressed, collapsible, page_size, promote_level,
                   rss_needs_cpu):
    """For each collapsible node, identify suppressed descendants that are
    promoted (heavy CPU/RSS) and should break out as heads-up rows even when
    the ancestor is collapsed. Returns (breakout_pids, breakout_map) where
    breakout_map is {proc_id(ancestor): [Proc]} capped at BREAKOUT_MAX."""
    kthreadd = procs.get(2)
    kthread_pids = set()
    if kthreadd is not None:
        kthread_pids = {2} | {d.pid for d in _descendants(kthreadd)}
    # Build lookup: proc_id -> Proc for collapsible ancestors
    cid_to_proc = {}
    for p in procs.values():
        pid = proc_id(p)
        if pid in collapsible:
            cid_to_proc[pid] = p
    breakout_map = {}
    breakout_pids = set()
    for cid, ancestor in cid_to_proc.items():
        for d in _descendants(ancestor):
            if d.pid not in suppressed or d.interesting:
                continue
            is_kthread = d.pid in kthread_pids
            if is_promoted(d, page_size, promote_level, rss_needs_cpu, is_kthread):
                breakout_map.setdefault(cid, []).append(d)
                breakout_pids.add(d.pid)
    # Cap per ancestor, sorted by total CPU ticks descending
    for cid, bps in breakout_map.items():
        bps.sort(key=lambda p: -(p.utime + p.stime))
        if len(bps) > BREAKOUT_MAX:
            for p in bps[BREAKOUT_MAX:]:
                breakout_pids.discard(p.pid)
            breakout_map[cid] = bps[:BREAKOUT_MAX]
    return breakout_pids, breakout_map


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
    """Parse /proc/meminfo. Return {free, buff, cache, mem_total, swap_total,
    swap_free} in BYTES (meminfo is kB). Missing keys -> None (swap_total -> 0
    so swap-off is the safe default)."""
    raw = {}
    for line in content.splitlines():
        f = line.split()
        if len(f) >= 2 and f[0].endswith(":"):
            try:
                raw[f[0][:-1]] = int(f[1]) * 1024     # kB -> bytes
            except ValueError:
                pass
    return {"free": raw.get("MemFree"), "buff": raw.get("Buffers"),
            "cache": raw.get("Cached"), "mem_total": raw.get("MemTotal"),
            "swap_total": raw.get("SwapTotal", 0),
            "swap_free": raw.get("SwapFree")}


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


# --- pure core: vmstat sampling ---------------------------------------------


def _d(a, b):
    """a-b, or None if either operand is None."""
    return None if a is None or b is None else a - b


def _delta_rate(cur, prev, dt, scale=1.0):
    """(cur-prev)*scale/dt, or None if either counter is None."""
    if cur is None or prev is None:
        return None
    return (cur - prev) * scale / dt


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


def vmstat_colored_row(prev_s, cur_s, dt, hist, d, cores):
    """Build one (rate_row, levels) pair from an adjacent sample pair and fold
    the row into `hist`. Each cell's level is computed against the histogram as
    it stands (absolute ceiling + high-tail percentile), THEN the value is folded
    in — a value is judged against history, then becomes part of it. levels[k] is
    0..3 (frozen). Mutates `hist`."""
    row = _vmstat_row(prev_s, cur_s, dt)
    levels = {}
    for k, _h, ki in VMSTAT_COLS:
        val = row.get(k)
        levels[k] = vmstat_cell_level(k, ki, val, hist[k], cores)
        vmstat_hist_fold(hist[k], val, ki, d)        # fold AFTER reading level
    return row, levels


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
               {"free": None, "buff": None, "cache": None, "mem_total": None,
                "swap_total": 0, "swap_free": None})
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
        swap_total=mem["swap_total"], swap_free=mem["swap_free"],
        mem_total=mem["mem_total"])


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


def windowed_rate(ring, window, clk_tck):
    """CPU rate (in cores) over the trailing `window` seconds of a sample ring.
    `ring` is [(monotonic_t, cpu_ticks)] ascending. Uses the most recent sample
    at or before now-window as the baseline (or the oldest sample if the ring is
    younger than the window), and the ACTUAL elapsed wall time between that
    baseline and the latest sample (frames can be late). None if < 2 samples or
    a non-positive span."""
    if len(ring) < 2:
        return None
    now_t, now_ticks = ring[-1]
    target = now_t - window
    base = ring[0]
    for sample in ring:
        if sample[0] <= target:
            base = sample
        else:
            break
    t0, ticks0 = base
    elapsed = now_t - t0
    if elapsed <= 0:
        return None
    return ((now_ticks - ticks0) / clk_tck) / elapsed


def update_history(history, procs, now, longest_window):
    """Append (now, utime+stime) to each live proc's ring (keyed by
    (pid, starttime)); evict samples older than now-longest_window while keeping
    the single most recent sample before the cutoff (so the longest window stays
    fully covered); drop rings for pids no longer present. Mutates `history`."""
    cutoff = now - longest_window
    seen = set()
    for p in procs.values():
        key = (p.pid, p.starttime)
        seen.add(key)
        ring = history.setdefault(key, [])
        ring.append((now, p.utime + p.stime))
        keep_from = 0
        for i, (ts, _ticks) in enumerate(ring):
            if ts < cutoff:
                keep_from = i
            else:
                break
        if keep_from:
            del ring[:keep_from]
    for key in list(history):
        if key not in seen:
            del history[key]


def compute_windows(procs, history, windows, clk_tck):
    """Set proc.cpu_windows: a list of per-window CPU rates (cores) aligned to
    `windows`, computed from the proc's history ring. Entries are None where the
    ring has < 2 samples."""
    for p in procs.values():
        ring = history.get((p.pid, p.starttime), [])
        p.cpu_windows = [windowed_rate(ring, w, clk_tck) for w in windows]


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


# --- disk pane ---------------------------------------------------------------


_VIRTUAL_FS = frozenset({
    "proc", "sysfs", "devtmpfs", "tmpfs", "cgroup", "cgroup2", "debugfs",
    "tracefs", "securityfs", "binfmt_misc", "fusectl", "pstore", "bpf",
    "mqueue", "configfs", "efivarfs", "hugetlbfs", "rpc_pipefs",
    "fuse.gvfsd-fuse", "fuse.portal",
    "squashfs", "overlay", "nsfs", "devpts", "autofs",
})


def read_mounts():
    """Read /proc/mounts and return [(mount_point, device, fstype)] for real
    filesystems (virtual/pseudo-fs filtered out)."""
    try:
        content = _read("/proc/mounts")
    except OSError:
        return []
    out = []
    for line in content.splitlines():
        f = line.split()
        if len(f) < 4:
            continue
        device, mount, fstype = f[0], f[1], f[2]
        if fstype in _VIRTUAL_FS:
            continue
        if mount.startswith("/proc") or mount.startswith("/sys"):
            continue
        out.append((mount, device, fstype))
    return out


def statvfs_mount(mount_point):
    """Return MountInfo for a mount point via os.statvfs, or None on failure."""
    try:
        s = os.statvfs(mount_point)
    except OSError:
        return None
    total = s.f_blocks * s.f_frsize
    free = s.f_bfree * s.f_frsize
    avail = s.f_bavail * s.f_frsize
    used = total - free
    pct = (used / total * 100.0) if total > 0 else 0.0
    return MountInfo(mount_point=mount_point, device="", fstype="",
                     total=total, used=used, avail=avail, pct=pct)


def read_disk_sample():
    """Read mount points and statvfs each. Returns [MountInfo], sorted by
    pct descending (most full first)."""
    raw = read_mounts()
    mounts = []
    for mount_point, device, fstype in raw:
        mi = statvfs_mount(mount_point)
        if mi is not None:
            mounts.append(MountInfo(
                mount_point=mount_point, device=device, fstype=fstype,
                total=mi.total, used=mi.used, avail=mi.avail, pct=mi.pct))
    mounts.sort(key=lambda m: -m.pct)
    return mounts


def _disk_pct_tint(pct, color):
    """Return tinted string for a disk usage percentage."""
    s = "%d%%" % round(pct)
    if not color:
        return s
    if pct >= DISK_USAGE_CRIT_PCT:
        return "\x1b[1;31m%s\x1b[0m" % s
    if pct >= DISK_USAGE_WARN_PCT:
        return "\x1b[33m%s\x1b[0m" % s
    return s


def format_disk_pane(mounts, width, height, color):
    """Render the disk space pane: header row + up to height-1 mount rows.
    Each row: mount  used/total  pct  avail. Sorted by pct desc."""
    gutter = DISK_PANE_GUTTER
    pad = " " * len(gutter)
    if not mounts or height <= 1:
        return [gutter + "  MOUNT  USED/TOTAL  %USED  AVAIL"]
    sorted_mounts = sorted(mounts, key=lambda m: -m.pct)
    shown = sorted_mounts[:height - 1]
    # Compute column widths
    mount_w = max(len(m.mount_point) for m in shown)
    mount_w = min(mount_w, max(20, width // 3))
    header = gutter + "  " + "MOUNT".ljust(mount_w) + "  USED/TOTAL  %USED  AVAIL"
    lines = [header]
    for m in shown:
        mp = m.mount_point[:mount_w].ljust(mount_w)
        used_total = "%s/%s" % (fmt_bytes(m.used), fmt_bytes(m.total))
        pct = _disk_pct_tint(m.pct, color)
        avail = fmt_bytes(m.avail)
        lines.append(pad + "  %s  %s  %s  %s" % (mp, used_total, pct, avail))
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
                          cutime=st.cutime, cstime=st.cstime,
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


def cores_count():
    """Number of online CPUs (for the header and for context, not for math —
    CPU figures are already in cores). Falls back to 1."""
    return os.cpu_count() or 1


def read_loadavg():
    """Return (load_1m, load_5m, load_15m, running, total) from /proc/loadavg.
    Any unreadable field degrades to None. Format: '0.43 0.41 0.76 1/1101 662997'"""
    try:
        parts = _read("/proc/loadavg").split()
        r_total = parts[3].split("/")
        return (float(parts[0]), float(parts[1]), float(parts[2]),
                int(r_total[0]), int(r_total[1]))
    except (OSError, ValueError, IndexError):
        return (None, None, None, None, None)


def count_states(procs):
    """Return (running, sleeping, zombie) task counts from a {pid: Proc} snapshot.
    Pure function; no I/O."""
    running = sum(1 for p in procs.values() if p.state == "R")
    sleeping = sum(1 for p in procs.values() if p.state in ("S", "D", "I"))
    zombie = sum(1 for p in procs.values() if p.state == "Z")
    return running, sleeping, zombie


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


def vmstat_bucket(value, kind):
    """Log-scale bucket index 0..NBUCKETS-1 for `value` of column `kind`.
    Bucket 0 is zero/non-positive; 1..NBUCKETS-1 are log-spaced over the kind's
    [lo, hi] range, clamped at both ends."""
    if value is None or value <= 0:
        return 0
    lo, hi = VMSTAT_KIND_RANGE[kind]
    frac = math.log(value / lo) / math.log(hi / lo)
    idx = 1 + int(frac * (VMSTAT_NBUCKETS - 1))
    return max(1, min(VMSTAT_NBUCKETS - 1, idx))


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
    h[:] = [v * d for v in h]        # decay every bucket (in place)
    h[b] += (1.0 - d)
    col["count"] += 1


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
    except (ValueError, KeyError, TypeError, AttributeError):
        return vmstat_hist_new()


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
        with open(path) as fh:
            return vmstat_hist_from_json(fh.read())
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
        with open(tmp, "w") as fh:
            fh.write(vmstat_hist_to_json(state))
        os.replace(tmp, path)
    except OSError:
        pass


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


SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values):
    """Render a series of numbers as Unicode block-element bars (one char per
    value). The series is scaled to its own [min, max] range, so the shape shows
    relative busy/idle history regardless of absolute magnitude. None values
    render at the floor. A flat (or single-value) series renders all-baseline.
    Empty -> empty string. Pure."""
    if not values:
        return ""
    nums = [0.0 if v is None else v for v in values]
    lo, hi = min(nums), max(nums)
    span = hi - lo
    if span <= 0:                       # flat / single value: no shape to show
        return SPARK_BLOCKS[0] * len(nums)
    out = []
    last = len(SPARK_BLOCKS) - 1
    for v in nums:
        idx = int((v - lo) / span * last + 0.5)
        out.append(SPARK_BLOCKS[max(0, min(last, idx))])
    return "".join(out)


def _cpu_bit(windows_fracs, avg_frac=None):
    """Format a per-window CPU headline: 'cpu 400% 200% 50%' (one figure per
    window; None -> '—'). Tint level = max _tint_level across the non-None
    windows. In --once mode, avg_frac adds a trailing '(Y avg)' lifetime bit.
    Returns (text, level)."""
    parts = [(fmt_pct(f) or "—") for f in windows_fracs]
    level = max((_tint_level(f, CPU_TINT_ANCHORS)
                 for f in windows_fracs if f is not None), default=0)
    text = "cpu " + " ".join(parts)
    if avg_frac is not None:
        a = fmt_pct(avg_frac)
        if a is not None:
            text += " (%s avg)" % a
    return (text, level)


def _compose_dim(bits, color):
    """Join (text, level) bits into one detail line. Without color, plain text.
    With color, each bit is wrapped in its tint level's SGR code (TINT_SGR);
    level 0 is dim, so all-baseline lines look exactly as before while heavy
    cpu/rss bits gain a graduated warm tint."""
    if not color:
        return "  ".join(t for t, _ in bits)
    segs = ["\x1b[%sm%s\x1b[0m" % (TINT_SGR[lvl], t) for t, lvl in bits]
    return "\x1b[2m  \x1b[0m".join(segs)


def _descendants_bit(node, sysinfo):
    """A '(text, tint level)' bit summarizing the subtree below `node`: live
    descendant count plus the descendants' lifetime-average CPU (which folds in
    children that already died). None when `node` has no descendants. The CPU
    figure is tinted by load like the per-process cpu bit."""
    count = subtree_descendant_count(node)
    if not count:
        return None
    frac = subtree_descendant_cpu(node, sysinfo.uptime, sysinfo.clk_tck)
    text = "desc:%d" % count
    level = 0
    cpu = fmt_pct(frac)
    if cpu is not None:
        text += " " + cpu
        level = _tint_level(frac, CPU_TINT_ANCHORS)
    return (text, level)


def _trend_arrow(now, avg):
    """A glyph comparing the latest value to the series' own average: ▲ when
    notably busier than usual, ▼ when notably quieter, ≈ when steady. Returns
    '' when there is nothing to compare against."""
    if avg is None or avg <= 0:
        return ""
    if now >= avg * 1.3:
        return "▲"
    if now <= avg * 0.7:
        return "▼"
    return "≈"


def _focus_line(count, samples, color):
    """Core of the bright 'zoomed-in' line: descendant `count`, a sparkline of
    recent subtree-CPU `samples` (cores, oldest..newest), the current total, and
    a trend arrow vs the series' own average. None when count is 0 or there is
    no history."""
    if not count or not samples:
        return None
    nums = [s for s in samples if s is not None]
    spark = sparkline(samples)
    now = samples[-1] if samples[-1] is not None else 0.0
    avg = (sum(nums) / len(nums)) if nums else None
    bits = ["desc:%d" % count]
    if spark:
        bits.append(spark)
    bits.append("now %s" % (fmt_pct(now) or "0%"))
    arrow = _trend_arrow(now, avg)
    if arrow:
        bits.append(arrow)
    text = "  ".join(bits)
    if color:                           # normal intensity (brighter than dim)
        return "\x1b[0m" + text + "\x1b[0m"
    return text


def _focus_detail(node, sysinfo, samples, color):
    """The bright, 'zoomed-in' detail line shown in place of the dim detail on
    the row the cursor sits on, for a single process `node`. See _focus_line."""
    return _focus_line(subtree_descendant_count(node), samples, color)


def _detail(node, color, sysinfo=None, show_avg=False):
    bits = []   # (text, tint level)
    if node.cwd and node.cwd not in ("?", ""):
        bits.append(("cwd:" + compress_path(node.cwd), 0))
    if node.exe and node.exe not in ("?", ""):
        bits.append(("exe:" + compress_path(node.exe), 0))
    if node.sockets_str:
        bits.append((node.sockets_str, 0))
    if sysinfo is not None:
        if node.cpu_windows and any(f is not None for f in node.cpu_windows):
            avg = None
            if show_avg:
                life = lifetime_secs(node.starttime, sysinfo.uptime,
                                     sysinfo.clk_tck)
                avg = cpu_fraction(node.utime + node.stime, life,
                                   sysinfo.clk_tck)
            bits.append(_cpu_bit(node.cpu_windows, avg))
        desc = _descendants_bit(node, sysinfo)
        if desc is not None:
            bits.append(desc)
        rss = node.rss_pages * sysinfo.page_size
        if rss > 0:
            bits.append(("rss:" + fmt_bytes(rss),
                         _tint_level(rss, RSS_TINT_ANCHORS)))
        bits.append(("up:" + fmt_duration(
            lifetime_secs(node.starttime, sysinfo.uptime, sysinfo.clk_tck)), 0))
    if node.num_threads > 1:
        bits.append(("%d threads" % node.num_threads, 0))
    if not bits:
        return None
    return _compose_dim(bits, color)


def _visible_children(node, suppressed):
    return [c for c in node.children if c.kept and c.pid not in suppressed]


def _group_label(members, width):
    return brace_summary([m.cmdline for m in members])[:width]


def _group_detail(members, color, sysinfo, show_avg=False):
    """Aggregated detail line for a merged group: shared/braced cwd & exe,
    member pids, and cpu/rss/up ranges (when sysinfo is given)."""
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
        nwin = max((len(m.cpu_windows) for m in members if m.cpu_windows),
                   default=0)
        if nwin:
            parts = []
            sums = []
            for w in range(nwin):
                vals = [m.cpu_windows[w] for m in members
                        if m.cpu_windows and m.cpu_windows[w] is not None]
                parts.append(range_str(vals, fmt_pct) if vals else "—")
                sums.append(sum(vals))
            # tint tracks the heaviest window's summed load across members
            level = max(_tint_level(s, CPU_TINT_ANCHORS) for s in sums)
            text = "cpu " + " ".join(parts)
            if show_avg:
                avgs = [a for a in (
                    cpu_fraction(m.utime + m.stime,
                                 lifetime_secs(m.starttime, sysinfo.uptime,
                                               sysinfo.clk_tck), sysinfo.clk_tck)
                    for m in members) if a is not None]
                if avgs:
                    text += " (%s avg)" % range_str(avgs, fmt_pct)
            bits.append((text, level))
        desc_count = sum(subtree_descendant_count(m) for m in members)
        if desc_count:
            fracs = [f for f in (
                subtree_descendant_cpu(m, sysinfo.uptime, sysinfo.clk_tck)
                for m in members) if f is not None]
            text = "desc:%d" % desc_count
            dlevel = 0
            cpu = fmt_pct(sum(fracs)) if fracs else None
            if cpu is not None:
                text += " " + cpu
                dlevel = _tint_level(sum(fracs), CPU_TINT_ANCHORS)
            bits.append((text, dlevel))
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
    return _compose_dim(bits, color)


_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


def visible_truncate(s, width):
    """Truncate `s` to `width` VISIBLE characters, counting through SGR escapes
    (\\x1b[..m) without splitting them. If a non-reset colour is still active at
    the end of the kept text, a reset (\\x1b[0m) is appended so it doesn't bleed
    into the rest of the screen."""
    if width <= 0:
        return ""
    out = []
    vis = 0
    has_color = False
    i = 0
    n = len(s)
    while i < n:
        m = _SGR_RE.match(s, i)
        if m:
            esc = m.group()
            out.append(esc)
            has_color = esc != "\x1b[0m"
            i = m.end()
            continue
        if vis >= width:
            break
        out.append(s[i])
        vis += 1
        i += 1
    res = "".join(out)
    if has_color:           # a non-reset colour is still active -> stop the bleed
        res += "\x1b[0m"
    return res


def clip_frame(lines, rows, cols):
    """Clip a list of rendered lines to a `rows` x `cols` terminal: every line
    is column-truncated (ANSI-aware); if there are more than `rows` lines, keep
    rows-1 and replace the rest with a '… +K more' footer."""
    clipped = [visible_truncate(ln, cols) for ln in lines]
    if len(clipped) <= rows:
        return clipped
    keep = clipped[:rows - 1]
    more = len(clipped) - (rows - 1)
    keep.append(visible_truncate("… +%d more" % more, cols))
    return keep


def subtree_window_cpu(node, widx):
    """Sum of the window `widx` CPU rate over `node` and ALL its descendants
    (suppressed/collapsed included). None rates count as 0. Used to order
    top-level subtrees by their true total load."""
    total = 0.0
    nodes = [node] + _descendants(node)
    for n in nodes:
        if n.cpu_windows:
            v = n.cpu_windows[widx]
            if v is not None:
                total += v
    return total


def subtree_descendant_count(node):
    """Number of descendants under `node` (live, in this snapshot), excluding
    `node` itself. Counts collapsed/suppressed descendants too."""
    return len(_descendants(node))


def _sum_rings(rings):
    """Element-wise sum of several sample rings, aligned on their newest samples
    (right-aligned); rings shorter than the longest contribute 0 to the older
    slots. Used to derive a group's combined CPU series from its members'."""
    rings = [r for r in rings if r]
    if not rings:
        return []
    n = max(len(r) for r in rings)
    out = [0.0] * n
    for r in rings:
        offset = n - len(r)
        for i, v in enumerate(r):
            out[offset + i] += v
    return out


def update_focus_history(focus_hist, roots, widx, cap):
    """Append this frame's subtree CPU (window `widx`, cores) to a per-process
    ring keyed by proc_id, for every node reachable from `roots` (kept or not).
    Evict rings for pids absent this frame; cap each ring at `cap` samples.
    Mutates `focus_hist`. Groups derive their series by summing member rings at
    render time, so only individual processes are tracked here."""
    seen = set()
    stack = list(roots)
    while stack:
        node = stack.pop()
        key = proc_id(node)
        seen.add(key)
        ring = focus_hist.setdefault(key, [])
        ring.append(subtree_window_cpu(node, widx))
        if len(ring) > cap:
            del ring[:len(ring) - cap]
        stack.extend(node.children)
    for key in list(focus_hist):
        if key not in seen:
            del focus_hist[key]


def subtree_descendant_cpu(node, uptime, clk_tck):
    """Average CPU (cores) consumed over `node`'s lifetime by its DESCENDANTS,
    INCLUDING descendants that have already died and been reaped. `node`'s own
    utime/stime is excluded (that is the per-process 'cpu' bit); its
    cutime/cstime IS included, since those are the cpu of children it reaped.

    The kernel folds a reaped child's entire utime+stime+cutime+cstime into its
    parent's cutime/cstime, so node.cutime+node.cstime plus the four counters of
    every live descendant counts each dead descendant exactly once (no double
    counting). Divided by `node`'s wall-clock lifetime. None when that lifetime
    is non-positive."""
    life = lifetime_secs(node.starttime, uptime, clk_tck)
    if life <= 0:
        return None
    ticks = node.cutime + node.cstime
    for n in _descendants(node):
        ticks += n.utime + n.stime + n.cutime + n.cstime
    return cpu_fraction(ticks, life, clk_tck)


def build_rows(roots, suppressed, width=CMD_WIDTH, color=None, sysinfo=None,
               dedup_min=None, never_merge=frozenset(), top_sort_key=None,
               show_avg=False, expanded=frozenset(), collapsible=frozenset(),
               breakout_map=None, focus_history=None):
    """Build the tree as Row records (text, item_id, expandable, selectable).
    Head rows (Proc/Group) are selectable; detail/collapse-note rows are
    continuation lines. A Group whose id is in `expanded` renders a header row
    followed by its members individually (so you can re-collapse it); otherwise
    it renders the merged ×N line and recurses over the union of children. A
    Proc head is expandable iff its id is in `collapsible`.

    `focus_history` (optional) maps a head row's item_id to a list of recent
    subtree-CPU samples (cores, oldest..newest). When given, each detail row is
    tagged with its head's item_id and a baked `focus` string (the bright
    zoomed-in line: descendant count + CPU sparkline + trend) that
    present_viewport swaps in when the cursor lands on that head."""
    if color is None:
        color = False
    rows = []

    def emit(text, item_id=None, expandable=False, selectable=False,
             focus=None):
        rows.append(Row(text, item_id, expandable, selectable, focus))

    def focus_for(node, item_id, prefix):
        """Baked focus string (with `prefix` indentation) for a detail row, or
        None when there is no history or nothing to show."""
        if focus_history is None or sysinfo is None:
            return None
        samples = focus_history.get(item_id)
        if not samples:
            return None
        line = _focus_detail(node, sysinfo, samples, color)
        return (prefix + line) if line is not None else None

    def focus_for_group(members, gid, prefix):
        """Baked focus string for a merged group's detail row: descendant count
        summed across members, with a combined sparkline derived from the
        members' own per-process histories."""
        if focus_history is None or sysinfo is None:
            return None
        samples = _sum_rings([focus_history.get(proc_id(m)) for m in members])
        if not samples:
            return None
        count = sum(subtree_descendant_count(m) for m in members)
        line = _focus_line(count, samples, color)
        return (prefix + line) if line is not None else None

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
                    emit(child_prefix + detail, item_id=gid,
                         focus=focus_for_group(item.members, gid, child_prefix))
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
                    emit(child_prefix + detail, item_id=pid_id,
                         focus=focus_for(item, pid_id, child_prefix))
                kids = _visible_children(item, suppressed)
                walk_items(group_siblings(kids, dedup_min, never_merge),
                           child_prefix, pid_id)
                if item.collapsed and item.collapse_note:
                    emit(child_prefix + item.collapse_note)
                # Breakout rows: hot procs that poke through the collapse
                if (item.collapsed and breakout_map
                        and proc_id(item) in breakout_map):
                    for bp in breakout_map[proc_id(item)]:
                        bp_line = "%s>> %d %s" % (
                            child_prefix, bp.pid,
                            compress_cmdline(bp.cmdline, width))
                        emit(bp_line, item_id=proc_id(bp),
                             expandable=False, selectable=True)
                        bp_detail = _detail(bp, color, sysinfo, show_avg)
                        if bp_detail is not None:
                            emit(child_prefix + "   " + bp_detail)

    top_items = group_siblings(list(roots), dedup_min, never_merge)
    if top_sort_key is not None:
        # group_siblings already orders by min-pid (stable); a stable sort by
        # descending key therefore gives "load desc, pid asc" tiebreak.
        top_items.sort(key=top_sort_key, reverse=True)
    walk_items(top_items, "", ROOT_ID)
    return rows


def render(roots, suppressed, width=CMD_WIDTH, color=None, sysinfo=None,
           dedup_min=None, never_merge=frozenset(), top_sort_key=None,
           show_avg=False, expanded=frozenset(), collapsible=frozenset(),
           breakout_map=None):
    """Backward-compatible string view of build_rows (used by the once/piped
    path and by tests)."""
    return [r.text for r in build_rows(
        roots, suppressed, width=width, color=color, sysinfo=sysinfo,
        dedup_min=dedup_min, never_merge=never_merge, top_sort_key=top_sort_key,
        show_avg=show_avg, expanded=expanded, collapsible=collapsible,
        breakout_map=breakout_map)]


# --- live UI state & viewport -----------------------------------------------


@dataclass
class UIState:
    expanded: set = field(default_factory=set)
    cursor: tuple = None
    scroll_top: int = 0
    frozen: bool = False
    sort_idx: int = 0
    vmstat_on: bool = True
    disk_on: bool = True


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
        body = r.text
        # On the cursored head's detail row, swap in the bright "zoomed-in"
        # focus line (descendant count + CPU sparkline + trend).
        if (not r.selectable and r.focus is not None and r.item_id == cursor):
            body = r.focus
        text = gutter + body
        if idx == cur_idx and color:
            text = "\x1b[7m" + text + "\x1b[0m"
        out.append(text)

    if top > 0:                           # content hidden above
        out[0] = dim("▲ %d more above" % top)
    if top + height < n:                  # content hidden below
        out[-1] = dim("▼ %d more below" % (n - (top + height)))
    return out, cursor, top


def split_regions(rows, cols, vmstat_on, vmstat_rows_cap, sample_rows,
                  disk_on=False, disk_rows_cap=0, disk_mounts_count=0):
    """Divide the screen height into (tree_region, vmstat_pane_rows, disk_pane_rows,
    show_vmstat, show_disk). One row is the pinned header. The vmstat pane
    (separator + header + k sample rows) is shown only when the terminal clears
    the size thresholds, the user hasn't toggled it off, and there is room.
    The disk pane (separator + header + k mount rows) is shown below the vmstat
    pane if toggled on and there is room. If terminal is too small, disk hides
    first (lower priority than vmstat)."""
    body = rows - 2  # header (2-line: system summary + task/key hint)
    vmstat_pane = 0
    show_vmstat = False
    disk_pane = 0
    show_disk = False

    # vmstat pane
    if vmstat_on and rows >= MIN_ROWS_FOR_VMSTAT and cols >= MIN_COLS_FOR_VMSTAT:
        k = min(vmstat_rows_cap, max(sample_rows, MIN_VMSTAT_SAMPLE_ROWS))
        k = min(k, body - MIN_TREE_ROWS - 2)    # 2 = separator + pane header
        if k >= MIN_VMSTAT_SAMPLE_ROWS:
            vmstat_pane = 2 + k
            show_vmstat = True

    remaining = body - vmstat_pane

    # disk pane (below vmstat, lower priority)
    if (disk_on and rows >= MIN_ROWS_FOR_DISK and cols >= MIN_COLS_FOR_DISK
            and disk_mounts_count > 0):
        dk = min(disk_rows_cap, max(disk_mounts_count, 2))
        dk = min(dk, remaining - MIN_TREE_ROWS - 2)
        if dk >= 2:
            disk_pane = 2 + dk
            show_disk = True

    tree_rows = body - vmstat_pane - disk_pane
    return tree_rows, vmstat_pane, disk_pane, show_vmstat, show_disk


def lifecycle_section(prev, cur, sysinfo, frame_dt, color):
    """The born/died lines (empty list when nothing changed). Shared by the
    once frame and the live loop."""
    if prev is None:
        return []
    born, died = diff_snapshots(prev, cur)
    return format_lifecycle(born, died, _parents_map(prev, cur), sysinfo,
                            frame_dt, color=color)


def glossary(color):
    """A short legend printed at the head of the output explaining the
    annotations (notably what '+N est' means). Returns a list of lines."""
    lines = [
        "topf — interesting & heavy process subtrees; the dim line under each "
        "process annotates it.",
        "  sockets: LISTEN :PORT = listening   "
        "+N est = N established TCP connections   unix:PATH = named socket",
        "  stats:   cpu A% B% C% = CPU over the short/med/long windows "
        "(cores; 100% = 1 core)   rss = resident memory   up = time since start",
        "  subtree: desc:N X% = N live descendants + the cpu their whole subtree "
        "(incl. already-exited children) averaged over this node's lifetime",
        "  groups:  ×N = N near-identical siblings merged (pids/ranges on the "
        "detail line)   lifecycle = procs born/died during the sample window",
    ]
    if color:
        lines = ["\x1b[2m%s\x1b[0m" % ln for ln in lines]
    return lines


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


def collect_printed(roots, suppressed):
    """The nodes render() will actually print -> the only ones to deep-probe."""
    out = []

    def walk(node):
        if not node.kept or node.pid in suppressed:
            return
        out.append(node)
        for c in node.children:
            walk(c)
    for r in roots:
        walk(r)
    return out


def _draw_frame(out, lines):
    """Home the cursor, write each line with clear-to-EOL, then clear to end of
    screen so a shorter frame doesn't leave stale rows behind. No trailing
    newline on the last line — writing \\n on the bottom row would scroll the
    terminal and push the top line off-screen."""
    buf = ["\x1b[H"]
    for ln in lines[:-1]:
        buf.append(ln + "\x1b[K\r\n")
    if lines:
        buf.append(lines[-1] + "\x1b[K")
    buf.append("\x1b[J")
    out.write("".join(buf))
    out.flush()


def _read_key(fd):
    """Decode one logical key from `fd` (unbuffered). Returns a plain char, or
    one of 'up'/'down'/'pgup'/'pgdn'/'home'/'end'/'esc'/'enter'.  Uses os.read
    to avoid the stdio-buffering mismatch where select sees an empty fd but
    sys.stdin still has buffered bytes."""
    def _read1():
        b = os.read(fd, 1)
        return b.decode() if b else ""
    ch = _read1()
    if ch == "\r" or ch == "\n":
        return "enter"
    if ch != "\x1b":
        return ch
    if not _select.select([fd], [], [], 0.01)[0]:
        return "esc"
    nxt = _read1()
    if nxt not in ("[", "O"):
        return "esc"
    seq = ""
    while True:
        c = _read1()
        if not c:
            break
        seq += c
        if c.isalpha() or c == "~":
            break
    if nxt == "O":
        return {"A": "up", "B": "down", "H": "home", "F": "end"}.get(seq, "esc")
    return {"A": "up", "B": "down", "H": "home", "F": "end",
            "5~": "pgup", "6~": "pgdn", "1~": "home", "4~": "end"}.get(seq, "esc")


def _select_timeout(frozen, deadline, now):
    """How long the live loop should wait for a keypress before resampling.

    Frozen means "don't resample, just wait for a key", so block forever (None)
    instead of polling. Returning a finite timeout here is the "f freezes my
    CPU" bug: while frozen the `deadline` never advances, so a 0.0 timeout makes
    select() return instantly every iteration and the loop spins a core flat.
    Unfrozen, wait out whatever is left of the sample interval -- clamped at 0,
    never negative (select rejects a negative timeout)."""
    if frozen:
        return None
    return max(0.0, deadline - now)


def run_live(args):
    """Full-screen live loop with a pinned header, a scrolling/cursored process
    tree, and pinned vmstat + disk panes. Keys: q/Ctrl-C quit, f freeze, w sort
    window, v toggle vmstat, d toggle disk, E expand all groups, ↑/k ↓/j move
    cursor, PgUp/PgDn page, g/G top/bottom, Space/Enter expand-collapse the
    selected group/subtree. Terminal state is always restored."""
    fd = sys.stdin.fileno()
    out = sys.stdout
    old_attr = termios.tcgetattr(fd)
    windows = args.windows
    longest = max(windows)
    history = {}
    focus_hist = {}     # proc_id -> ring of subtree-CPU samples for sparklines
    vmring = []
    ui = UIState(vmstat_on=not args.no_vmstat, disk_on=not args.no_disk)
    sysinfo_cores = cores_count()
    vmhist = (vmstat_hist_new() if args.no_history
              else vmstat_hist_load(vmstat_hist_path(args)))
    vmd = 0.5 ** (1.0 / max(1, args.vmstat_halflife))
    vmcolored = []          # ring of (rate_row, levels), frozen at sample
    vm_write_ctr = 0
    prev, t_prev = None, None
    cur, rows, sysinfo = {}, [], None
    _last_live_keys = set()
    disk_sample = None

    def repaint():
        """Re-present the current `rows` + vmstat ring without resampling (used
        after navigation/expand/freeze so input feels instant)."""
        cols, term_rows = os.get_terminal_size()
        disk_mounts = disk_sample if disk_sample is not None else []
        region_h, vmstat_h, disk_h, show_vmstat, show_disk = split_regions(
            term_rows, cols, ui.vmstat_on, args.vmstat_rows, len(vmcolored),
            disk_on=ui.disk_on, disk_rows_cap=args.disk_rows,
            disk_mounts_count=len(disk_mounts))
        body, ui.cursor, ui.scroll_top = present_viewport(
            rows, ui, region_h, color=not args.no_color)
        if show_vmstat or show_disk:
            body += [""] * (region_h - len(body))   # pad so panes pin to bottom
        n_run, n_sleep, n_zombie = count_states(cur)
        hidden = sum(1 for p in cur.values() if not p.kept)
        latest = vmring[-1] if vmring else None
        mem_total = latest.mem_total if latest else None
        mem_used = (mem_total - (latest.free or 0) - (latest.buff or 0)
                    - (latest.cache or 0)) if mem_total and latest else None
        swap_total = latest.swap_total if latest else None
        swap_free = latest.swap_free if latest else None
        frame = header_line((t_prev and (time.monotonic() - t_prev)) or 0.0,
                            sysinfo, len(cur), hidden, args.sample_interval,
                            ui.frozen,
                            loadavg=read_loadavg(),
                            mem_total=mem_total, mem_used=mem_used,
                            swap_total=swap_total, swap_free=swap_free,
                            n_running=n_run, n_sleeping=n_sleep,
                            n_zombie=n_zombie).split('\n')
        frame += body
        if show_vmstat:
            swap_on = any(s.swap_total for s in vmring if s.swap_total)
            frame.append("─" * cols)
            frame += format_vmstat_pane(vmcolored, swap_on, cols, vmstat_h - 1,
                                        color=not args.no_color)
        if show_disk:
            frame.append("─" * cols)
            frame += format_disk_pane(disk_mounts, cols, disk_h - 1,
                                      color=not args.no_color)
        _draw_frame(out, [visible_truncate(ln, cols) for ln in frame[:term_rows]])

    def sample_and_build():
        nonlocal prev, t_prev, cur, rows, sysinfo, vm_write_ctr, _last_live_keys, disk_sample
        cur = scan()
        t_now = time.monotonic()
        update_history(history, cur, t_now, longest)
        compute_windows(cur, history, windows, CLK_TCK)
        vmring.append(read_vmstat_sample(t_now))
        if len(vmring) > args.vmstat_rows + 1:
            del vmring[0]
        if len(vmring) >= 2:
            prev_s, cur_s = vmring[-2], vmring[-1]
            dt = cur_s.t - prev_s.t
            if dt > 0:
                vmcolored.append(vmstat_colored_row(prev_s, cur_s, dt, vmhist,
                                                    vmd, sysinfo_cores))
                if len(vmcolored) > args.vmstat_rows * 2 + 2:
                    del vmcolored[0]
                vm_write_ctr += 1
                if not args.no_history and \
                        vm_write_ctr % VMSTAT_WRITE_EVERY == 0:
                    vmstat_hist_save(vmstat_hist_path(args), vmhist)
        disk_sample = read_disk_sample()
        sysinfo = SysInfo(clk_tck=CLK_TCK, page_size=PAGE_SIZE,
                          uptime=read_uptime(), cores=sysinfo_cores)
        visible_roots, suppressed, collapsible, breakout_map = prepare_frame(
            cur, args, sysinfo, expanded=ui.expanded)
        dedup_min = None if args.no_dedup else args.dedup_min
        update_focus_history(focus_hist, visible_roots, ui.sort_idx,
                             FOCUS_SPARK_SAMPLES)
        key = lambda item: subtree_window_cpu(
            item.members[0] if isinstance(item, Group) else item, ui.sort_idx)
        rows = build_rows(visible_roots, suppressed, width=args.width,
                          color=not args.no_color, sysinfo=sysinfo,
                          dedup_min=dedup_min, never_merge=NEVER_MERGE,
                          top_sort_key=key, expanded=ui.expanded,
                          collapsible=collapsible, breakout_map=breakout_map,
                          focus_history=focus_hist)
        _last_live_keys = {(p.pid, p.starttime) for p in cur.values()}
        prev, t_prev = cur, t_now

    try:
        tty.setcbreak(fd)
        out.write("\x1b[?1049h")
        out.flush()
        sample_and_build()
        repaint()
        deadline = time.monotonic() + args.sample_interval
        while True:
            remaining = _select_timeout(ui.frozen, deadline, time.monotonic())
            r, _w, _e = _select.select([fd], [], [], remaining)
            if r:
                key = _read_key(fd)
                ids = selectable_ids(rows)
                if key in ("q", "\x03"):
                    break
                elif key == "f":
                    ui.frozen = not ui.frozen
                elif key == "w":
                    ui.sort_idx = (ui.sort_idx + 1) % len(windows)
                elif key == "v":
                    ui.vmstat_on = not ui.vmstat_on
                elif key == "E":
                    # Expand all group rows in the current view
                    for r in rows:
                        if r.expandable and r.item_id[0] == "g":
                            ui.expanded.add(r.item_id)
                    sample_and_build()
                    deadline = time.monotonic() + args.sample_interval
                elif key == "d":
                    ui.disk_on = not ui.disk_on
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
                        deadline = time.monotonic() + args.sample_interval
                repaint()                               # instant feedback
            elif not ui.frozen:
                sample_and_build()
                deadline = time.monotonic() + args.sample_interval
                repaint()
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\x1b[?1049l")
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
        if not args.no_history:
            vmstat_hist_save(vmstat_hist_path(args), vmhist)
        if not args.no_cache:
            Cache(cache_path(), boot_id=read_boot_id(),
                  now=time.time()).save(live_keys=_last_live_keys)


def header_line(frame_dt, sysinfo, nprocs, hidden, interval, frozen=False,
                loadavg=None, mem_total=None, mem_used=None,
                swap_total=None, swap_free=None,
                n_running=0, n_sleeping=0, n_zombie=0):
    """Top-style status line (two lines): system summary + procs/key hint."""
    state = "  FROZEN" if frozen else ""
    parts = ["topf — %.2gs, %d cores" % (frame_dt, sysinfo.cores)]
    if loadavg and loadavg[0] is not None:
        parts.append("load %.2f/%.2f/%.2f" % (loadavg[0], loadavg[1], loadavg[2]))
    if mem_total is not None:
        mem_t = fmt_bytes(mem_total)
        mem_u = fmt_bytes(mem_used) if mem_used is not None else "?"
        parts.append("Mem: %s/%s" % (mem_u, mem_t))
    if swap_total and swap_total > 0:
        sw_used = swap_total - (swap_free or 0)
        parts.append("Swap: %s/%s" % (fmt_bytes(sw_used), fmt_bytes(swap_total)))
    line1 = "  ".join(parts)
    task_parts = ["%d procs" % nprocs]
    if n_running:
        task_parts.append("%d run" % n_running)
    if n_sleeping:
        task_parts.append("%d sleep" % n_sleeping)
    if n_zombie:
        task_parts.append("%d zombie" % n_zombie)
    task_parts.append("%d hidden" % hidden)
    line2 = "(%s)  every %.2gs  [q]uit [f]reeze [w]in [v]mstat [d]isk [E]groups [↑↓]nav [␣]expand%s" \
            % (", ".join(task_parts), interval, state)
    return line1 + "\n" + line2


def prepare_frame(cur, args, sysinfo, expanded=frozenset()):
    """Shared pipeline: build the tree, select interesting/heavy nodes, collapse
    (honoring expanded), probe the printed nodes. Returns
    (visible_roots, suppressed, collapsible)."""
    roots = build_tree(cur)
    select(cur, DEFAULT_MATCHERS, sysinfo.page_size, args.promote_level,
           args.rss_needs_cpu)
    suppressed, collapsible = collapse(cur, threshold=args.threshold,
                                       expanded=expanded)
    breakout_pids, breakout_map = find_breakouts(
        cur, suppressed, collapsible, sysinfo.page_size,
        args.promote_level, args.rss_needs_cpu)
    suppressed -= breakout_pids
    visible_roots = [r for r in roots if r.kept]
    printed = [n for n in collect_printed(visible_roots, suppressed) if n.kept]
    if args.no_cache:
        cache = Cache(os.devnull, boot_id="", now=time.time())
    else:
        cache = Cache(cache_path(), boot_id=read_boot_id(), now=time.time())
    probe(printed, cache)
    if not args.no_cache:
        cache.save(live_keys={(p.pid, p.starttime) for p in cur.values()})
    return visible_roots, suppressed, collapsible, breakout_map


def build_frame(prev, cur, history, t_prev, t_now, args, color, sysinfo,
                sort_idx, show_avg, frozen=False, expanded=frozenset(),
                vmring=None):
    """Pure-ish assembly of one frame's lines (no clipping, no terminal I/O):
    tree (ordered) + optional lifecycle, with a header on top. `prev` may be
    None (first frame / once-mode primes). `history` already updated & windows
    already computed for `cur`. Returns a list of lines. `vmring` supplies the
    latest meminfo for the header (read fresh if None)."""
    visible_roots, suppressed, collapsible, breakout_map = prepare_frame(
        cur, args, sysinfo, expanded=expanded)
    dedup_min = None if args.no_dedup else args.dedup_min
    key = lambda item: subtree_window_cpu(
        item.members[0] if isinstance(item, Group) else item, sort_idx)

    frame_dt = (t_now - t_prev) if prev is not None else 0.0
    hidden = sum(1 for p in cur.values() if not p.kept)
    n_run, n_sleep, n_zombie = count_states(cur)
    # meminfo for header: prefer latest vmstat sample, else read fresh
    if vmring:
        latest = vmring[-1]
    else:
        latest = read_vmstat_sample(time.monotonic())
    mem_total = latest.mem_total
    mem_used = (mem_total - (latest.free or 0) - (latest.buff or 0)
                - (latest.cache or 0)) if mem_total else None
    out = [header_line(frame_dt, sysinfo, len(cur), hidden,
                       args.sample_interval, frozen,
                       loadavg=read_loadavg(),
                       mem_total=mem_total, mem_used=mem_used,
                       swap_total=latest.swap_total,
                       swap_free=latest.swap_free,
                       n_running=n_run, n_sleeping=n_sleep,
                       n_zombie=n_zombie)]
    if not args.no_glossary:
        out += [""] + glossary(color)
    out += [""]
    out += render(visible_roots, suppressed, width=args.width, color=color,
                  sysinfo=sysinfo, dedup_min=dedup_min, never_merge=NEVER_MERGE,
                  top_sort_key=key, show_avg=show_avg, collapsible=collapsible,
                  breakout_map=breakout_map)
    if prev is not None and not args.no_lifecycle:
        born, died = diff_snapshots(prev, cur)
        section = format_lifecycle(born, died, _parents_map(prev, cur),
                                   sysinfo, frame_dt, color=color)
        if section:
            out += [""] + section
    return out


def render_once(interval, args):
    """Take two samples `interval` apart and return one frame's lines (no alt
    screen). Shortest window is real; longer windows show '—'; a lifetime avg is
    appended (show_avg=True). This is the piped / --once path."""
    windows = args.windows
    longest = max(windows)
    history = {}
    s_a = scan()
    t_a = time.monotonic()
    update_history(history, s_a, t_a, longest)
    time.sleep(interval)
    s_b = scan()
    t_b = time.monotonic()
    update_history(history, s_b, t_b, longest)
    compute_windows(s_b, history, windows, CLK_TCK)
    sysinfo = SysInfo(clk_tck=CLK_TCK, page_size=PAGE_SIZE,
                      uptime=read_uptime(), cores=cores_count())
    color = sys.stdout.isatty() and not args.no_color
    return build_frame(s_a, s_b, history, t_a, t_b, args, color, sysinfo,
                       sort_idx=0, show_avg=True)


def _once_defaults():
    """A defaults namespace for render_once in tests. Derived from _parse_args
    so new flags appear automatically; only overrides test-oriented defaults."""
    ns = _parse_args([])
    ns.no_cache = True
    ns.no_color = True
    return ns


def parse_windows(text):
    """Parse a '2,10,60' window spec into a tuple of positive floats (ascending
    order is the caller's responsibility). Raises ValueError on empty/garbage."""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        raise ValueError("no windows given")
    vals = tuple(float(p) for p in parts)   # float() raises ValueError on garbage
    if any(v <= 0 for v in vals):
        raise ValueError("windows must be positive")
    return vals


def _parse_args(argv):
    ap = argparse.ArgumentParser(prog="topf",
                                 description="Focused live process viewer.")
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
                    default=REFRESH_INTERVAL,
                    help="sample == redraw cadence in seconds (default %.2g)"
                         % REFRESH_INTERVAL)
    ap.add_argument("--no-dedup", action="store_true",
                    help="do not merge near-identical sibling subtrees")
    ap.add_argument("--dedup-min", type=int, default=DEDUP_MIN,
                    help="min identical siblings to merge (default %d)"
                         % DEDUP_MIN)
    ap.add_argument("--no-lifecycle", action="store_true",
                    help="suppress the born/died section")
    ap.add_argument("--once", action="store_true",
                    help="take a single plain frame and exit (auto when piped)")
    ap.add_argument("--windows", type=parse_windows, default=DEFAULT_WINDOWS,
                    metavar="A,B,C",
                    help="CPU window seconds, shortest first (default 2,10,60)")
    ap.add_argument("--promote-level", type=int, default=PROMOTE_LEVEL,
                    help="tint-anchor level to promote a heavy proc (default %d)"
                         % PROMOTE_LEVEL)
    ap.add_argument("--rss-needs-cpu", dest="rss_needs_cpu",
                    action="store_true", default=True,
                    help="RSS-only promotion also needs some CPU (default on)")
    ap.add_argument("--no-rss-needs-cpu", dest="rss_needs_cpu",
                    action="store_false",
                    help="allow promotion by large RSS alone")
    ap.add_argument("--no-vmstat", action="store_true",
                    help="start with the bottom vmstat pane hidden (toggle: v)")
    ap.add_argument("--vmstat-rows", type=int, default=VMSTAT_ROWS_DEFAULT,
                    help="max vmstat sample rows in the pane (default %d)"
                         % VMSTAT_ROWS_DEFAULT)
    ap.add_argument("--history-file", default=None,
                    help="vmstat coloring history file (default: XDG state dir)")
    ap.add_argument("--no-history", action="store_true",
                    help="do not load or save vmstat coloring history")
    ap.add_argument("--vmstat-halflife", type=int,
                    default=VMSTAT_HALFLIFE_DEFAULT,
                    help="samples for a vmstat coloring weight to halve "
                         "(default %d)" % VMSTAT_HALFLIFE_DEFAULT)
    ap.add_argument("--no-disk", action="store_true",
                    help="start with the disk space pane hidden (toggle: d)")
    ap.add_argument("--disk-rows", type=int, default=DISK_ROWS_DEFAULT,
                    help="max mount rows in the disk pane (default %d)"
                         % DISK_ROWS_DEFAULT)
    args = ap.parse_args(argv)
    if args.vmstat_halflife < 1:
        ap.error("--vmstat-halflife must be a positive integer")
    return args


def main(argv=None):
    args = _parse_args(argv)
    use_once = args.once or not sys.stdout.isatty()
    if use_once:
        lines = render_once(args.sample_interval, args)
        print("\n".join(lines))
        return
    run_live(args)


if __name__ == "__main__":
    main()
