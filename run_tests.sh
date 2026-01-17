#!/bin/bash
# Gentle test runner - one file at a time with sleeps and nice

set -e

SLEEP_BETWEEN=2  # seconds between test files
NICE_LEVEL=10    # lower priority (higher number = nicer)

cd "$(dirname "$0")"

# Activate venv if exists
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
fi

TEST_FILES=(tests/test_*.py)
TOTAL=${#TEST_FILES[@]}
PASSED=0
FAILED=0

echo "=== Gentle Test Runner ==="
echo "Files: $TOTAL | Sleep: ${SLEEP_BETWEEN}s | Nice: $NICE_LEVEL"
echo ""

for i in "${!TEST_FILES[@]}"; do
    file="${TEST_FILES[$i]}"
    num=$((i + 1))

    echo "[$num/$TOTAL] $(basename "$file")"

    if nice -n $NICE_LEVEL python -m pytest "$file" -v --tb=short 2>&1; then
        ((PASSED++))
        echo "  ✓ PASSED"
    else
        ((FAILED++))
        echo "  ✗ FAILED"
    fi

    # Sleep between files (except after last)
    if [ $num -lt $TOTAL ]; then
        echo "  (sleeping ${SLEEP_BETWEEN}s...)"
        sleep $SLEEP_BETWEEN
    fi
    echo ""
done

echo "=== Summary ==="
echo "Passed: $PASSED / $TOTAL"
echo "Failed: $FAILED"

[ $FAILED -eq 0 ] && exit 0 || exit 1
