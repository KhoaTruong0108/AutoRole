#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SKIP_INSTALL=0
RUN_TESTS=1

usage() {
  cat <<'EOF'
Usage: scripts/bootstrap_and_run_e2e.sh [options]

Options:
  --python <path>   Python executable to use (default: python3 or $PYTHON_BIN)
  --skip-install    Skip package installation step
  --no-tests        Do not run tests after setup
  -h, --help        Show this help

What this script does:
1. Upgrades pip/setuptools/wheel
2. Installs required libraries for AutoRole
3. Installs project in editable mode
4. Exports PYTHONPATH=src
5. Runs full-flow E2E integration test
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      PYTHON_BIN="$2"
      shift 2
      ;;
    --skip-install)
      SKIP_INSTALL=1
      shift
      ;;
    --no-tests)
      RUN_TESTS=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

cd "$ROOT_DIR"

echo "[1/5] Using Python: $PYTHON_BIN"
"$PYTHON_BIN" --version

if [[ "$SKIP_INSTALL" -eq 0 ]]; then
  echo "[2/5] Installing build tooling..."
  "$PYTHON_BIN" -m pip install --upgrade pip setuptools wheel

  echo "[3/5] Installing runtime/test dependencies (prefer wheels)..."
  PIP_PREFER_BINARY=1 "$PYTHON_BIN" -m pip install \
    pydantic \
    pydantic-settings \
    orjson \
    structlog \
    aiosqlite \
    httpx \
    playwright \
    beautifulsoup4 \
    keyring \
    typer \
    textual \
    rich \
    openai \
    anthropic \
    markdown \
    pytest \
    pytest-asyncio \
    pytest-cov

  if "$PYTHON_BIN" -c "import playwright" >/dev/null 2>&1; then
    echo "[3.5/5] Playwright already installed; skipping browser install"
  else
    echo "[3.5/5] Installing Playwright + Chromium browser..."
    PIP_PREFER_BINARY=1 "$PYTHON_BIN" -m pip install playwright
    "$PYTHON_BIN" -m playwright install chromium
  fi

  echo "[4/5] Installing SnapFlow from GitHub (avoids old PyPI constraints)..."
  PIP_PREFER_BINARY=1 "$PYTHON_BIN" -m pip install \
    "snapflow @ git+https://github.com/KhoaTruong0108/SnapFlow.git"

  echo "[5/5] Installing AutoRole package (editable mode, no deps)..."
  "$PYTHON_BIN" -m pip install -e . --no-deps
else
  echo "[2-5/5] Skipping install steps (--skip-install)"
fi

export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
echo "PYTHONPATH set to: $PYTHONPATH"

if [[ "$RUN_TESTS" -eq 1 ]]; then
  echo "Running E2E integration test..."
  "$PYTHON_BIN" -m pytest tests/integration/test_pipeline_e2e.py -v
else
  echo "Skipping test execution (--no-tests)."
fi

echo "Done."
