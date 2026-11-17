#!/usr/bin/env bash
# Builds the "patchgate-core" Lambda layer: the cloud-agnostic plan library, JSON Schemas and
# hash-pinned dependencies (Linux x86_64 wheels for the python3.12 runtime).
# Output: providers/aws/functions/layer/.build/python/...
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../../.." && pwd)"
OUT="$HERE/.build"
PY="${PYTHON:-$ROOT/.venv/bin/python}"

rm -rf "$OUT"
mkdir -p "$OUT/python/patchgate/schemas"
"$PY" -m pip install --quiet --require-hashes --no-deps --only-binary=:all: \
  --platform manylinux2014_x86_64 --implementation cp --python-version 3.12 \
  --target "$OUT/python" -r "$HERE/requirements.txt"
cp "$ROOT"/core/lib/patchgate/{__init__,plan,schema}.py "$OUT/python/patchgate/"
mkdir -p "$OUT/python/patchgate_aws"
cp "$ROOT"/providers/aws/lib/patchgate_aws/{__init__,store}.py "$OUT/python/patchgate_aws/"
cp "$ROOT"/core/schemas/*.schema.json "$OUT/python/patchgate/schemas/"
find "$OUT" -name '__pycache__' -prune -exec rm -rf {} +
echo "layer built in $OUT ($(du -sh "$OUT" | cut -f1))"
