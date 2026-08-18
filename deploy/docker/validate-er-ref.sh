#!/bin/sh
set -eu

ref=${1:-}
if printf '%s\n' "$ref" | grep -Eq '^(v[0-9]+\.[0-9]+\.[0-9]+|[0-9a-f]{40})$'; then
    exit 0
fi

echo "ER_REF must be an immutable vX.Y.Z tag or 40-character commit SHA, got: $ref" >&2
exit 1
