#!/usr/bin/env bash
set -euo pipefail

# Replace this command with the real workload.
# Print one METRIC line for the primary metric and any secondary metrics.
start_ns=$(date +%s%N)

uv run python -m unittest discover -s tests/unit

end_ns=$(date +%s%N)
elapsed_seconds=$(awk "BEGIN { printf \"%.3f\", ($end_ns - $start_ns) / 1000000000 }")
printf 'METRIC seconds=%s\n' "$elapsed_seconds"
