#!/bin/sh
# trkar-screen-ndi を作り、アドオンの bin/ に置く (arm64 と x86_64 をまとめた 1 つのファイル)。
# 要るもの: Xcode のコマンドラインツール、NDI SDK for Apple のヘッダー (/Library/NDI SDK for Apple/include)。
# libndi はリンクせず実行時に読むので、配る先には NDI Tools か NDI SDK のランタイムがあればよい。
set -e
cd "$(dirname "$0")"
OUT=../../addon/bin
mkdir -p "$OUT" build
for arch in arm64 x86_64; do
    swiftc -O -swift-version 5 -target "$arch-apple-macos14.0" \
        -import-objc-header ndi_bridge.h -I "/Library/NDI SDK for Apple/include" \
        main.swift -o "build/trkar-screen-ndi-$arch"
done
lipo -create build/trkar-screen-ndi-arm64 build/trkar-screen-ndi-x86_64 -output "$OUT/trkar-screen-ndi"
echo "built $OUT/trkar-screen-ndi"
