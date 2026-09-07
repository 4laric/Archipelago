#!/bin/sh
set -eu

ref=${1:-}
if printf '%s\n' "$ref" | grep -Eq '^(v[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?|[0-9a-f]{40})$'; then
    exit 0
fi

echo "ER_REF must be an immutable vV.R.M[.F] tag (v0.6.0, v0.6.0.2) or 40-character commit SHA, got: $ref" >&2
exit 1
