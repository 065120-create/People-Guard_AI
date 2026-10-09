#!/usr/bin/env bash
# Run the automated test suite, then regenerate the evaluation report.
set -euo pipefail
cd "$(dirname "$0")/.."
python -m pytest -q
LLM_PROVIDER=mock python scripts/evaluate.py > /dev/null
echo "Evaluation report written to docs/EVALUATION.md"
