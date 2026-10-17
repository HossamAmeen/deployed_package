#!/usr/bin/env bash
# Shell entry point: checks prerequisites, then runs deploy.py.
set -euo pipefail
cd "$(dirname "$0")"

[ -f config.yml ] || { echo "config.yml not found. Copy config.yml.example to config.yml and fill it in." >&2; exit 1; }
for cmd in python3 ssh; do
    command -v "$cmd" >/dev/null || { echo "Missing required command: $cmd" >&2; exit 1; }
done
python3 -c "import yaml" 2>/dev/null || { echo "PyYAML missing. Run: pip install -r requirements.txt" >&2; exit 1; }

exec python3 deploy.py "$@"
