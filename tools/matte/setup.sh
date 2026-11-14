#!/bin/bash
# Set up the trkAR Matte relay on macOS / Linux: Python 3.12 and the libraries go into .venv in this folder.
# Everything is downloaded on this computer from the official sources (uv from astral.sh, Python builds and the
# libraries from PyPI); nothing is redistributed by trkAR. The trkAR Camera add-on runs this from "Set Up RVM Relay".
#
#   bash setup.sh
#
# - uv (https://docs.astral.sh/uv/) is installed into ~/.local/bin if it is not found
# - macOS: numpy, coremltools, pillow (Core ML). Linux: onnxruntime-gpu if nvidia-smi works, otherwise onnxruntime
# - The RVM models are downloaded into models/ only if they are missing (the Releases zip already has them)
# - The last line is SETUP_OK on success
set -euo pipefail
cd "$(dirname "$0")"
UV="$(command -v uv || true)"
[ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"
if [ -z "$UV" ]; then
  echo "installing uv (https://astral.sh/uv)"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
  UV="$HOME/.local/bin/uv"
fi
if [ ! -x .venv/bin/python ]; then
  echo "creating .venv (Python 3.12)"
  "$UV" venv -p 3.12 .venv
fi
RVM=https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0
mkdir -p models
if [ "$(uname)" = "Darwin" ]; then
  PACKAGES="numpy coremltools pillow"
  MODELS="rvm_mobilenetv3_1280x720_s0.375_fp16.mlmodel rvm_mobilenetv3_1920x1080_s0.25_fp16.mlmodel"
else
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    PACKAGES="numpy onnxruntime-gpu pillow"
  else
    PACKAGES="numpy onnxruntime pillow"
  fi
  MODELS="rvm_mobilenetv3_fp32.onnx"
fi
echo "installing $PACKAGES"
"$UV" pip install -q -p .venv $PACKAGES
for name in $MODELS; do
  if [ ! -s "models/$name" ]; then
    echo "downloading models/$name"
    curl -sfL -o "models/$name" "$RVM/$name"
  fi
done
echo SETUP_OK
