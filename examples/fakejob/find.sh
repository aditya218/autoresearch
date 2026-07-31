#!/usr/bin/env bash
# Look a job up by the tag the launcher stamped on it (D11 tier 2).
set -euo pipefail
SPOOL="${FAKEJOB_SPOOL:?}"; KEY="${AUTORESEARCH_IDEM_KEY:?}"
for d in "$SPOOL"/*/; do
  [ -f "$d/tag" ] || continue
  if [ "$(cat "$d/tag")" = "$KEY" ]; then basename "$d"; exit 0; fi
done
exit 0
