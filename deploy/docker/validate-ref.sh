#!/bin/sh
# Reject a MOVING ref for either game's tooling pin.
#
# The pin is the reproducibility boundary: the same ER_REF/BB_REF must always produce the same
# image. `main` here once promoted the development wizard to /er/ while /downloads still named
# v0.4.6 (#863), which is the whole reason this script exists.
#
# Accepted spellings:
#   vV.R.M[.F]          Elden Ring release tags, with the optional fourth "fixpack" segment
#                       (er-archipelago tools/vrmf.py): v0.6.0, v0.6.0.2
#   vV.R.M-beta.N       Bloodborne beta tags: v0.1.0-beta.5. Immutable tags like any other -- the
#                       GitHub "prerelease" checkbox is a UI flag, not a mutability property.
#   40 hex characters   a full commit SHA
set -eu

ref=${1:-}
var=${2:-REF}
if printf '%s\n' "$ref" | grep -Eq '^(v[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+|-beta\.[0-9]+)?|[0-9a-f]{40})$'; then
    exit 0
fi

echo "$var must be an immutable vV.R.M[.F] tag (v0.6.0, v0.6.0.2), a vV.R.M-beta.N tag (v0.1.0-beta.5), or a 40-character commit SHA, got: $ref" >&2
exit 1
