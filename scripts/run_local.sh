#!/usr/bin/env bash
# Start PeopleGuard AI locally on http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")/.."
python -m uvicorn app.main:app --reload --port "${PORT:-8000}"
