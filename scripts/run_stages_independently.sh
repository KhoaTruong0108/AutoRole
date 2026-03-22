#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
STAGE="all"

ALL_STAGES=(
  exploring
  scoring
  tailoring
  packaging
  session
  form_intelligence
  form_submission
  concluding
  gate
)

RESULT_EXPLORING="-"
RESULT_SCORING="-"
RESULT_TAILORING="-"
RESULT_PACKAGING="-"
RESULT_SESSION="-"
RESULT_FORM_INTELLIGENCE="-"
RESULT_FORM_SUBMISSION="-"
RESULT_CONCLUDING="-"
RESULT_GATE="-"

usage() {
  cat <<'EOF'
Usage: scripts/run_stages_independently.sh [options]

Options:
  --python <path>   Python executable to use (default: python3 or $PYTHON_BIN)
  --stage <name>    Stage to run (default: all)
  -h, --help        Show this help

Valid stage names:
  exploring
  scoring
  tailoring
  packaging
  session
  form_intelligence
  form_submission
  concluding
  gate
  all

Examples:
  scripts/run_stages_independently.sh
  scripts/run_stages_independently.sh --stage scoring
  scripts/run_stages_independently.sh --python /Users/me/.pyenv/versions/3.12.0/bin/python --stage session
EOF
}

run_pytest() {
  local test_path="$1"
  echo "Running: $test_path"
  PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m pytest "$test_path" -v
}

stage_test_path() {
  local stage_name="$1"
  case "$stage_name" in
    exploring) echo "tests/unit/test_exploring.py" ;;
    scoring) echo "tests/unit/test_scoring.py" ;;
    tailoring) echo "tests/unit/test_tailoring.py" ;;
    packaging) echo "tests/unit/test_packaging.py" ;;
    session) echo "tests/unit/test_session.py" ;;
    form_intelligence) echo "tests/unit/test_form_intelligence.py" ;;
    form_submission) echo "tests/unit/test_form_submission.py" ;;
    concluding) echo "tests/unit/test_concluding.py" ;;
    gate) echo "tests/unit/test_best_fit_gate.py" ;;
    *) return 1 ;;
  esac
}

set_stage_result() {
  local stage_name="$1"
  local value="$2"
  case "$stage_name" in
    exploring) RESULT_EXPLORING="$value" ;;
    scoring) RESULT_SCORING="$value" ;;
    tailoring) RESULT_TAILORING="$value" ;;
    packaging) RESULT_PACKAGING="$value" ;;
    session) RESULT_SESSION="$value" ;;
    form_intelligence) RESULT_FORM_INTELLIGENCE="$value" ;;
    form_submission) RESULT_FORM_SUBMISSION="$value" ;;
    concluding) RESULT_CONCLUDING="$value" ;;
    gate) RESULT_GATE="$value" ;;
    *) return 1 ;;
  esac
}

get_stage_result() {
  local stage_name="$1"
  case "$stage_name" in
    exploring) echo "$RESULT_EXPLORING" ;;
    scoring) echo "$RESULT_SCORING" ;;
    tailoring) echo "$RESULT_TAILORING" ;;
    packaging) echo "$RESULT_PACKAGING" ;;
    session) echo "$RESULT_SESSION" ;;
    form_intelligence) echo "$RESULT_FORM_INTELLIGENCE" ;;
    form_submission) echo "$RESULT_FORM_SUBMISSION" ;;
    concluding) echo "$RESULT_CONCLUDING" ;;
    gate) echo "$RESULT_GATE" ;;
    *) echo "-" ;;
  esac
}

run_stage() {
  local stage_name="$1"
  local test_path
  test_path="$(stage_test_path "$stage_name")"
  if run_pytest "$test_path"; then
    set_stage_result "$stage_name" "PASS"
    return 0
  fi
  set_stage_result "$stage_name" "FAIL"
  return 1
}

print_summary() {
  echo
  echo "Stage                Status"
  echo "-------------------- -------"
  for stage_name in "${ALL_STAGES[@]}"; do
    local status
    status="$(get_stage_result "$stage_name")"
    printf "%-20s %s\n" "$stage_name" "$status"
  done
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      PYTHON_BIN="$2"
      shift 2
      ;;
    --stage)
      STAGE="$2"
      shift 2
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

echo "Using Python: $PYTHON_BIN"
"$PYTHON_BIN" --version

if [[ "$STAGE" == "all" ]]; then
  had_failure=0
  for stage_name in "${ALL_STAGES[@]}"; do
    if ! run_stage "$stage_name"; then
      had_failure=1
    fi
  done
  print_summary
  if [[ "$had_failure" -eq 1 ]]; then
    echo "One or more stage checks failed."
    exit 1
  fi
  echo "All stage-level checks passed."
  exit 0
fi

case "$STAGE" in
  exploring|scoring|tailoring|packaging|session|form_intelligence|form_submission|concluding|gate)
    run_stage "$STAGE"
    ;;
  *)
    echo "Invalid --stage value: $STAGE" >&2
    usage
    exit 1
    ;;
esac

print_summary
echo "Stage '$STAGE' check passed."
