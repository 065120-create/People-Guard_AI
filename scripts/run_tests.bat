@echo off
REM Windows: run tests and regenerate the evaluation report
cd /d "%~dp0\.."
python -m pytest -q || exit /b 1
set LLM_PROVIDER=mock
python scripts\evaluate.py > nul
echo Evaluation report written to docs\EVALUATION.md
