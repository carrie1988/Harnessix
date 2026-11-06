#!/bin/sh
set -eu
mkdir -p /logs/verifier
if printf 'harnessix-harbor-smoke\n' | cmp -s - /app/result.txt; then
    printf '1\n' > /logs/verifier/reward.txt
else
    printf '0\n' > /logs/verifier/reward.txt
fi
