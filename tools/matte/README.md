# trkar_matte (RVM で人物を切り抜いて NDI で送り直す)

> trkAR Camera (Blender アドオン) と組み合わせて使う、AI で人物を切り抜く中継です。ライセンスは GPL-3.0-or-later
> (`LICENSE`、アドオンと同じ)。RVM ([Robust Video Matting](https://github.com/PeterL1n/RobustVideoMatting)) のモデルは
> このリポジトリには入れていません。下の手順で配布元から落としてください (RVM も GPL-3.0 です)。
> English: an NDI relay that cuts out people with RVM and sends RGBA NDI for trkAR Camera. Set up below (macOS: Core ML,
> Windows / Linux: ONNX Runtime with a GPU).

trkAR の実写 NDI を [Robust Video Matting](https://github.com/PeterL1n/RobustVideoMatting) (RVM) で切り抜き、
アルファ付き (RGBA) の NDI `trkAR Matte` として送り直す中継プログラム。
ARKit の人物マスク (256×192) より細かく、髪の毛まで半透明で抜ける。

```
iPhone ─NDI "trkAR"─▶ trkar_matte.py (RVM) ─NDI "trkAR Matte" (RGBA)─▶ Blender / OBS
```

- 色は受けた映像のまま、アルファに RVM のマットを入れる
- timecode は受けた映像のものを引き継ぐので、Blender の `映像に合わせる` (深度との同期) はそのまま効く
- Blender アドオンの `ndi.py` (ctypes) を使うので NDI ランタイムが必要 (アドオンと同じ)
- 推論エンジンは 2 通り。`--backend` を省略すると macOS は Core ML、それ以外 (Windows / Linux) は
  ONNX Runtime になる

### macOS (Core ML)

```sh
cd tools/matte
uv venv -p 3.12 .venv
uv pip install -p .venv numpy coremltools pillow
mkdir -p models && cd models
curl -LO https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/rvm_mobilenetv3_1280x720_s0.375_fp16.mlmodel
curl -LO https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/rvm_mobilenetv3_1920x1080_s0.25_fp16.mlmodel
```

### Windows / Linux (ONNX Runtime)

GPU 版は NVIDIA (CUDA) か DirectML (Windows、NVIDIA 以外の GPU でも動く) のどちらかを選ぶ。
`--onnx-provider` (既定 `auto`) で `cuda` / `dml` / `cpu` を明示することもできる。

```sh
cd tools/matte
uv venv -p 3.12 .venv

REM Windows + NVIDIA (CUDA)
uv pip install -p .venv numpy onnxruntime-gpu pillow
REM Windows (DirectML、NVIDIA 以外の GPU も含めて広く動く)
uv pip install -p .venv numpy onnxruntime-directml pillow
REM CPU だけ (GPU なし、または動作確認用)
uv pip install -p .venv numpy onnxruntime pillow

mkdir models
curl -LO https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/rvm_mobilenetv3_fp32.onnx
move rvm_mobilenetv3_fp32.onnx models\
```

ONNX 版は Core ML 版と違って解像度固定のモデルが 2 つあるわけではなく、1 つのモデル
(`rvm_mobilenetv3_fp32.onnx`) を `downsample_ratio` (720 = 0.375 / 1080 = 0.25) で
使い分ける。`--model` オプションの意味は Core ML 版と同じ。

## 使い方

Blender のアドオン trkAR Camera からは、`trkAR 受信` の実写 (NDI) の段にある `RVM で切り抜く` ボタンで
起動・停止できる (Blender の NDI 受信も自動で `trkAR Matte` に切り替わる。公開版は 0.10.22 から)。
このフォルダが見つからないときは、Blender の `編集 > プリファレンス > アドオン > trkAR Camera` の
`RVM 中継のフォルダ` にこのフォルダを指定する。`受信開始で切り抜きも始める` を入れると、`trkAR 受信` の ▶ で
切り抜きも一緒に始まる。ターミナルから動かすときは:

```sh
.venv/bin/python trkar_matte.py --list                    # NDI ソース一覧
.venv/bin/python trkar_matte.py                           # 名前に "(trkAR)" を含む最初のソース
.venv/bin/python trkar_matte.py --source "IPHONE (trkAR)" --model 1080
```

| オプション | 内容 |
|---|---|
| `--source` | 受ける NDI ソース (一部だけでも可) |
| `--name` | 送り出す NDI の名前 (既定 `trkAR Matte`) |
| `--model` | `720` (既定、速い) / `1080` (細かい) |
| `--backend` | `auto` (既定、macOS は `coreml` / それ以外は `onnx`) / `coreml` / `onnx` |
| `--units` | Core ML の計算装置 (既定 `CPU_AND_NE`。M4 ではこれが一番速い。`--backend coreml` のときだけ) |
| `--onnx-provider` | ONNX Runtime の実行先 `auto` (既定、CUDA → DirectML → CPU) / `cuda` / `dml` / `cpu` |
| `--parent-pid` | そのプロセスが終わったら一緒に終わる (Blender から起動するときに使う) |

2 秒ごとに `受信 fps / 送出 fps / RVM ms / 受信→送出 ms / 間引き / 受け手` を出す。
処理が追いつかないときは古いフレームを捨てて最新を処理する (`間引き` が増える)。

Windows では `.venv\Scripts\python.exe trkar_matte.py ...` (`.venv/bin/python` の代わり)。

Blender では `NDI 映像をシーンに取り込む` のソースに `<PC名> (trkAR Matte)` を選ぶ。
深度スクリーンは映像のアルファを見つけると、人の輪郭を RVM のアルファで切る
(パネルに `輪郭は実写のアルファ (RVM) で切り抜き中` と出る)。

## 遅延 (Mac mini M4 / 24GB、同じ Mac 内、1080p 30fps、Core ML)

| | 遅延 |
|---|---|
| NDI 直接 | 約 12ms |
| この中継を通す | 約 60ms (+約 48ms、30fps で 1.5 フレームほど) |
| RVM 単体 (Core ML、CPU_AND_NE) | 720 モデル 約 17ms / 1080 モデル 約 25ms |

中継の中 (受信 → 送出) は約 40ms で、そのうち RVM と前後の縮小・拡大が約 33ms。
30fps は落とさずに回る。深度 (UDP) は `映像に合わせる` で映像の時刻に揃うので、
遅れが増えても輪郭と映像はずれない (OBS で CG と重ねるときは CG 側を 1〜2 フレーム遅らせる)。

- 縦位置 (9:16) の映像は横長のモデルの中央に小さく入るので、横位置より粗くなる
- RVM は前のフレームの状態を持ち越して安定させるので、動き始めの数フレームは甘い
- 中継を 2 つ同時に動かすと Neural Engine を取り合って両方遅くなる (1 つで 17ms → 2 つで約 50ms)

## Windows (ONNX Runtime) の実測と注意

720 モデル・1080p 入力で実測 (I/O 名 `r1i`/`r2i`/`r3i`/`r4i` ⇄ `r1o`/`r2o`/`r3o`/`r4o` は
公式 ONNX モデルと一致することを確認済み):

| GPU | 実行 provider | RVM 1 フレーム |
|---|---|---|
| NVIDIA GeForce MX150 (Blade Stealth、旧世代の省電力モバイル GPU) | DirectML | 約 300〜350ms |
| 同上 | CPU | 約 470〜510ms |

いずれも 30fps に必要な 33ms/フレームを大きく超えており、**この程度の GPU では
ONNX Runtime 版 RVM はリアルタイムに向かない** (2〜3fps 程度)。実行中に RVM が
100ms/フレームを超えると中継のログに警告が出る。

- そのクラスの GPU (古い/省電力なモバイル GPU、内蔵 GPU のみなど) では、
  RVM の切り抜きは使わず、iPhone の人物マスク (trkAR の `MASK` か `DPT`。ARKit の人物マスクで、
  Blender の深度の `人の輪郭で切る`) を使う。
  こちらは iPhone 側 (ARKit) で計算済みのマスクを UDP で受けるだけなので、
  PC 側の GPU 性能に依存しない
- CUDA を使う場合は `onnxruntime-gpu` + 対応する CUDA Toolkit / cuDNN の導入が別途必要
  (今回は未検証。GPU が新しい/VRAM が大きいマシンならこちらや fp16 モデルで改善する余地はある)
