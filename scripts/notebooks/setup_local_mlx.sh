#!/usr/bin/env bash
# Local fine tuning setup for the weave agent, on Apple Silicon via MLX.
#
# Unsloth needs Python 3.11 to 3.13. This Mac has 3.10 and 3.14, so 3.12 gets
# installed with uv, which is faster and does not need brew.
#
# Unsloth on a Mac goes through MLX, not the CUDA path, so mlx and mlx-lm are
# required rather than optional.
set -euo pipefail

VENV="${WEAVE_VENV:-$HOME/.venvs/weave-mlx}"
MODEL="${WEAVE_MODEL:-Qwen/Qwen3-4B}"

say() { printf '\n=== %s\n' "$1"; }

say "installing uv if it is not there"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
uv --version

say "installing python 3.12"
uv python install 3.12
uv python find 3.12

say "creating the venv at $VENV"
uv venv --python 3.12 --clear "$VENV" 2>/dev/null || uv venv --python 3.12 "$VENV"
"$VENV/bin/python" --version

say "installing unsloth, mlx and friends"
# uv venv does not create pip, so uv pip is used with an explicit interpreter.
# This is the long pole: several GB, quiet for minutes at a time.
uv pip install --python "$VENV/bin/python" unsloth unsloth_zoo mlx mlx-lm "psycopg[binary]"

say "what got installed"
uv pip list --python "$VENV/bin/python" 2>/dev/null | grep -iE "^(torch|mlx|mlx-lm|unsloth|unsloth-zoo|transformers|peft|trl|datasets|bitsandbytes|numpy|python) " || true

say "verifying mlx sees the GPU"
"$VENV/bin/python" - <<'PY'
import mlx.core as mx
print("mlx ok, default device:", mx.default_device())
try:
    import torch
    print("torch", torch.__version__, "mps available:", torch.backends.mps.is_available())
except ImportError:
    print("torch not installed, which is fine: the MLX path does not use it")
PY

say "done"
echo "venv: $VENV"
echo "next: WEAVE_VENV=$VENV WEAVE_MODEL=$MODEL $VENV/bin/python 09_train_mlx.py"
