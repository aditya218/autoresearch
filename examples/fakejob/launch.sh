#!/usr/bin/env bash
# Submit a job. Prints the xid on the last line of stdout.
# Writes the receipt BEFORE exiting, so a controller crash in the launch window
# is recoverable without a lookup tool (D11 tier 1).
set -euo pipefail
SPOOL="${FAKEJOB_SPOOL:?}"
DURATION="${FAKEJOB_DURATION:-4}"
FAIL="${FAKEJOB_FAIL:-}"
METRIC="${FAKEJOB_METRIC:-120.5}"
ARTDIR="${AUTORESEARCH_ARTIFACT_DIR:-/tmp}"
XID="xid-$(date +%s%N | tail -c 8)-$$"
JOB="$SPOOL/$XID"

mkdir -p "$JOB"
echo RUNNING > "$JOB/status"
printf '%s' "${AUTORESEARCH_IDEM_KEY:-none}" > "$JOB/tag"

# The job body is a file, not a nested -c string: quoting bugs in a launcher
# look exactly like infrastructure flakiness and are miserable to debug.
cat > "$JOB/run.sh" <<INNER
#!/usr/bin/env bash
sleep $DURATION
if [ -n "$FAIL" ]; then
  echo "training diverged at step 400" > "$JOB/log"
  echo "$FAIL" > "$JOB/status"
else
  mkdir -p "$ARTDIR"
  printf '{"p50_latency_ms": %s, "eval_quality": 0.94}' "$METRIC" > "$ARTDIR/metrics.json"
  echo "step 1000 done" > "$JOB/log"
  echo SUCCEEDED > "$JOB/status"
fi
INNER
chmod +x "$JOB/run.sh"
setsid nohup "$JOB/run.sh" >/dev/null 2>&1 < /dev/null &

if [ -n "${AUTORESEARCH_RECEIPT:-}" ]; then
  mkdir -p "$(dirname "$AUTORESEARCH_RECEIPT")"
  printf '%s' "$XID" > "$AUTORESEARCH_RECEIPT"
fi
echo "$XID"
