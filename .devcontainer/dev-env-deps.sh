#!/usr/bin/env bash
set -eu
set -o pipefail

# Debian-based base image: only uv has to be installed here.
if ! uv --version; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

uv sync --frozen
uv tree

echo "Ready. Inside the container: make help / make test / make verify"
