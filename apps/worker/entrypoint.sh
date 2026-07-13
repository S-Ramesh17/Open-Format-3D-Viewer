#!/bin/sh
# apps/worker/entrypoint.sh
#

set -eu

MULTIPROC_DIR="${PROMETHEUS_MULTIPROC_DIR:-}"

if [ -n "$MULTIPROC_DIR" ]; then
    # mkdir -p is a no-op if the tmpfs mount already created the mount
    # point, and a real safety net if this ever runs without the tmpfs
    # mount (e.g. a plain bind-mounted directory, or no mount at all in
    # some other environment).
    mkdir -p "$MULTIPROC_DIR"

    # Prometheus client's own multiprocess-mode guidance: stale *.db files
    # left behind by a previous container run (crash, restart, image
    # rebuild) will otherwise be picked up by MultiProcessCollector and
    # reported forever as metrics from PIDs that no longer exist. Safe to
    # do unconditionally at startup since this directory only ever holds
    # regenerable metric shards, never anything that needs to survive a
    # restart.
    find "$MULTIPROC_DIR" -maxdepth 1 -name '*.db' -delete 2>/dev/null || true

    # Least-privilege ownership: appuser-only, no group/world access.
    # (Not 777 — appuser is the only identity that will ever touch this
    # directory, since the whole process tree runs as appuser after this
    # script hands off below.)
    chown -R appuser:appuser "$MULTIPROC_DIR"
    chmod 0770 "$MULTIPROC_DIR"
fi

exec gosu appuser "$@"