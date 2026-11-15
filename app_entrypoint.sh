#!/usr/bin/env bash
set -eu

echo "Running simple app entrypoint."

exec uv run --frozen --no-dev app
