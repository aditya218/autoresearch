#!/usr/bin/env bash
set -euo pipefail
SPOOL="${FAKEJOB_SPOOL:?}"
XID="${1:-${AUTORESEARCH_JOB_ID:?}}"
[ -f "$SPOOL/$XID/status" ] && cat "$SPOOL/$XID/status" || echo "UNKNOWN"
