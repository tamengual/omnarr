#!/bin/sh
# Run as PUID:PGID when given (owning /data), otherwise as root.
# Root is the default only because database-mode sources (Calibre, Storyteller,
# BookBridge) are files owned by whatever user those apps run as; every source mount
# should be read-only, so the only place Omnarr writes is /data.
set -e
mkdir -p /data
if [ -n "$PUID" ] && [ "$PUID" != "0" ]; then
    PGID="${PGID:-$PUID}"
    chown -R "$PUID:$PGID" /data
    [ -d /config ] && [ -w /config ] && chown -R "$PUID:$PGID" /config 2>/dev/null || true
    exec su-exec "$PUID:$PGID" "$@"
fi
exec "$@"
