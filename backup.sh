#!/usr/bin/env bash
# Simple fast-compression backup that respects hard links.
# Edit ROOTS and EXCLUDES below, then run: ./backup.sh
#
# Incremental: uses a GNU tar snapshot ($SNAR). The first run (no snapshot)
# is a level-0 FULL; every later run is a level-1 INCREMENTAL capturing only
# files added/changed/deleted since the previous run.
#
# Restore (extract archives IN CHRONOLOGICAL ORDER -- full first, then each
# incremental; --listed-incremental=/dev/null also applies deletions):
#   for f in $(ls -1 ~/backups/backup-*-full.tar.zst ~/backups/backup-*-incr.tar.zst | sort); do
#       tar -I zstd --listed-incremental=/dev/null -xpf "$f" -C /restore/target
#   done
#
set -euo pipefail

# --- config -----------------------------------------------------------------

# Where the tarball lands.
DEST_DIR="${DEST_DIR:-$HOME/backups}"

# Roots to back up (absolute paths). Paths are stored relative to / so the
# archive restores cleanly anywhere.
ROOTS=(
    /home/shemer/mss
    /home/shemer/armproj
    /home/shemer/sakura-firmware
    /home/shemer/.claude
    /home/shemer/clones
)

# Patterns to skip (repeatable, glob-style, matched against the stored path).
EXCLUDES=(
    '*/.cache'
    '*/node_modules'
    '*/__pycache__'
    '*.tmp'
)

# Compressor: zstd level 1 (fast) across all cores. Bump -1 to -3 for a bit
# more ratio at near-zero cost. Swap for 'lz4 -1' or 'pigz -1' if you prefer.
COMPRESS='zstd -3 -T0'

# ---------------------------------------------------------------------------

if [ "${#ROOTS[@]}" -eq 0 ]; then
    echo "error: no ROOTS configured -- edit $0 and add paths." >&2
    exit 1
fi

mkdir -p "$DEST_DIR"

stamp="$(date +%Y%m%d-%H%M%S)"

# Persistent GNU tar snapshot. Present => incremental; absent => full baseline.
SNAR="${SNAR:-$DEST_DIR/backup.snar}"
if [ -f "$SNAR" ]; then
    kind=incr
else
    kind=full
fi
out="$DEST_DIR/backup-$stamp-$kind.tar.zst"

exclude_args=()
for pat in "${EXCLUDES[@]}"; do
    exclude_args+=(--exclude="$pat")
done

echo "Backing up to $out ($kind, snapshot $SNAR) ..."
tar -I "$COMPRESS" \
    --numeric-owner \
    --listed-incremental="$SNAR" \
    "${exclude_args[@]}" \
    -cf "$out" \
    -C / \
    "${ROOTS[@]#/}"

# Keep a per-run copy of the snapshot so the chain can be inspected/rewound.
cp -p "$SNAR" "$DEST_DIR/backup-$stamp-$kind.snar"

echo "Done: $(du -h "$out" | cut -f1)  $out"
