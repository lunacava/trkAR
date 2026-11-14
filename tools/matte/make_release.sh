#!/bin/bash
# Build trkar-matte-<version>.zip for GitHub Releases: the relay, its license, and the RVM models (so users do not
# have to download the models themselves). RVM (Robust Video Matting, https://github.com/PeterL1n/RobustVideoMatting)
# is GPL-3.0; its license and source location are included in the zip (THIRD_PARTY/).
#
#   bash tools/matte/make_release.sh <version>      e.g. 1.0  ->  dist/trkar-matte-1.0.zip
set -euo pipefail
VERSION="${1:?usage: make_release.sh <version>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
WORK="$(mktemp -d)"
PKG="$WORK/trkar_matte"
RVM="https://github.com/PeterL1n/RobustVideoMatting"
MODELS=(
  "rvm_mobilenetv3_1280x720_s0.375_fp16.mlmodel"
  "rvm_mobilenetv3_1920x1080_s0.25_fp16.mlmodel"
  "rvm_mobilenetv3_fp32.onnx"
)
mkdir -p "$PKG/models" "$PKG/THIRD_PARTY"
cp "$HERE/trkar_matte.py" "$HERE/README.md" "$HERE/LICENSE" "$HERE/setup.sh" "$HERE/setup.ps1" "$PKG/"
# NDI の呼び出し (アドオンの ndi.py。bpy なし・同じ GPL-3.0)。zip だけで動くように同梱する
cp "$ROOT/addon/ndi.py" "$PKG/"
for name in "${MODELS[@]}"; do
  curl -sfL -o "$PKG/models/$name" "$RVM/releases/download/v1.0.0/$name"
done
curl -sfL -o "$PKG/THIRD_PARTY/RVM_LICENSE" "https://raw.githubusercontent.com/PeterL1n/RobustVideoMatting/master/LICENSE"
{
  echo "# Third-party: Robust Video Matting (RVM)"
  echo
  echo "The files in models/ are the pretrained RVM models, unchanged, from"
  echo "$RVM/releases/tag/v1.0.0"
  echo
  echo "RVM is by Shanchuan Lin et al. (ByteDance Inc.), licensed under GPL-3.0 (THIRD_PARTY/RVM_LICENSE)."
  echo "Source code: $RVM"
  echo "Paper: Robust High-Resolution Video Matting with Temporal Guidance (WACV 2022)"
  echo
  echo "| File | SHA-256 |"
  echo "| --- | --- |"
  for name in "${MODELS[@]}"; do
    echo "| models/$name | $(shasum -a 256 "$PKG/models/$name" | cut -d' ' -f1) |"
  done
} > "$PKG/THIRD_PARTY/RVM_NOTICE.md"
echo "$VERSION" > "$PKG/VERSION"
mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/trkar-matte-$VERSION.zip"
rm -f "$OUT"
(cd "$WORK" && COPYFILE_DISABLE=1 zip -qr -X "$OUT" trkar_matte)
rm -rf "$WORK"
echo "$OUT ($(du -h "$OUT" | cut -f1))"
