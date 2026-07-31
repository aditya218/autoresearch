#!/usr/bin/env bash
# End-to-end: create a project and campaign, seed ideas, drive it to completion.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD"
export AUTORESEARCH_DSN="${AUTORESEARCH_DSN:-host=/var/run/postgresql user=postgres dbname=autoresearch}"
export FAKEJOB_DIR="$PWD/examples/fakejob"
export FAKEJOB_SPOOL="${FAKEJOB_SPOOL:-/tmp/fakejob-spool}"
export FAKEJOB_DURATION="${FAKEJOB_DURATION:-3}"
export AUTORESEARCH_ARTIFACTS="${AUTORESEARCH_ARTIFACTS:-/tmp/ar-artifacts}"

PID=$(python3 -m autoresearch.cli project-create --name v4-latency)
CID=$(python3 -m autoresearch.cli campaign-create --project "$PID" --config examples/campaign.yaml)
python3 -m autoresearch.cli campaign-start "$CID"
python3 -m autoresearch.cli idea-add --campaign "$CID" --file examples/ideas.yaml
echo "campaign $CID"
python3 -m autoresearch.cli run-start --campaign "$CID" --tick 1
python3 -m autoresearch.cli status "$CID"
python3 -m autoresearch.cli exp-list "$CID"
