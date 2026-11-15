# trkar_matte — AI Mask (RVM)

> trkAR Camera (Blender アドオン) の **AI Mask (RVM)** です。実写の中の人を AI で見分け、人だけを残します。ライセンスは GPL-3.0-or-later
> (`LICENSE`、アドオンと同じ)。**[Releases の `trkar-matte-<版>.zip`](https://github.com/lunacava/trkAR/releases/tag/matte-v1.2) に RVM のモデルを同梱しています**
> (RVM ([Robust Video Matting](https://github.com/PeterL1n/RobustVideoMatting)) も GPL-3.0。ライセンスと出どころは
> zip の `THIRD_PARTY/`)。zip を広げて Python の準備をすれば、モデルを落とす手順は要りません。
> リポジトリの `tools/matte` から使うときは、下の手順で配布元からモデルを落としてください (git にはモデルを入れていません)。
> English: **AI Mask (RVM)** for trkAR Camera: masks people in the live video with AI (RVM) and sends RGBA NDI. The zip on Releases includes
> the RVM models. See "English" at the end for setup.

trkAR の実写 NDI を [Robust Video Matting](https://github.com/PeterL1n/RobustVideoMatting) (RVM) で切り抜き、
アルファ付き (RGBA) の NDI `trkAR Matte` として送り直すプログラム (アドオンの画面では「AI Mask (RVM)」)。
ARKit の人物マスク (256×192) より細かく、髪の毛まで半透明で抜ける。

```
iPhone ─NDI "trkAR"─▶ trkar_matte.py (RVM) ─NDI "trkAR Matte" (RGBA)─▶ Blender / OBS
```

- 色は受けた映像のまま、アルファに RVM のマットを入れる
- timecode は受けた映像のものを引き継ぐので、Blender の `映像に合わせる` (深度との同期) はそのまま効く
- Blender アドオンの `ndi.py` (ctypes) を使うので NDI ランタイムが必要 (アドオンと同じ)
- 推論エンジンは 2 通り。`--backend` を省略すると macOS は Core ML、それ以外 (Windows / Linux) は
  ONNX Runtime になる

### Releases の zip から使う (かんたん)

1. [Releases](https://github.com/lunacava/trkAR/releases/tag/matte-v1.2) から `trkar-matte-<版>.zip` を落として、好きな場所に広げる (`trkar_matte/` ができる。`models/` にモデルが入っている)
2. Blender の `編集 > プリファレンス > アドオン > trkAR Camera` の `AI Mask フォルダ` (`AI Mask Folder`) に、広げた `trkar_matte/` を指定する
3. `trkAR 受信` の実写の段に出る **`AI Mask Setup`** (`Set Up AI Mask`) を押す (最初の 1 回だけ。アドオン 0.11.10〜)。
   この PC に Python 3.12 とライブラリ (Core ML か ONNX Runtime、100〜300MB ほど) を、それぞれの配布元から `.venv` に落とす。
   数分かかり、終わると `AI Mask 適用` が押せるようになる

ターミナルで準備するときは、`trkar_matte/` で `bash setup.sh` (macOS / Linux) か
`powershell -ExecutionPolicy Bypass -File setup.ps1` (Windows)。中身は下の手順と同じ
(uv が無ければ入れ、`.venv` を作ってライブラリを入れ、モデルが無ければ落とす)。
Python とライブラリは trkAR からは配らず、使う PC で配布元から落とすので、それぞれのライセンスはそのまま。

以下は手で準備するときの手順。

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

Blender のアドオン trkAR Camera からは、`trkAR 受信` の実写 (NDI) の段にある `AI Mask 適用` ボタンで
起動・停止できる (Blender の NDI 受信も自動で `trkAR Matte` に切り替わる。公開版は 0.10.22 から)。
このフォルダが見つからないときは、Blender の `編集 > プリファレンス > アドオン > trkAR Camera` の
`AI Mask フォルダ` にこのフォルダを指定する。`受信開始で AI Mask ON` を入れると、`trkAR 受信` の ▶ で
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

## English

**AI Mask (RVM)** in the add-on: a program that masks people in trkAR's live video with [Robust Video Matting](https://github.com/PeterL1n/RobustVideoMatting)
(RVM) and sends it again as an RGBA NDI source, `trkAR Matte`. It is finer than ARKit's person mask (256×192) and
keeps hair semi-transparent.

**Setup from Releases (easiest)**

1. Download `trkar-matte-<version>.zip` from [Releases](https://github.com/lunacava/trkAR/releases/tag/matte-v1.2) and unzip it anywhere. It contains `trkar_matte/` with the
   RVM models in `models/` (RVM is GPL-3.0; its license and source are in `THIRD_PARTY/`)
2. In Blender, set `Edit > Preferences > Add-ons > trkAR Camera > AI Mask Folder` to the unzipped `trkar_matte/`
3. Press **`Set Up AI Mask`** in the live video section of `trkAR Receive` (once; add-on 0.11.10 or later). It
   downloads Python 3.12 and the libraries (Core ML or ONNX Runtime, about 100–300 MB) from their official sources
   into `.venv`. It takes a few minutes; then `Apply AI Mask` starts it and `Stop AI Mask` stops it, and Blender's NDI input
   switches to `trkAR Matte`. With `AI Mask On at Receive Start` on, it starts with ▶ on `trkAR Receive`

To set up from a terminal instead, run `bash setup.sh` (macOS / Linux) or
`powershell -ExecutionPolicy Bypass -File setup.ps1` (Windows) in `trkar_matte/`. It installs
[uv](https://docs.astral.sh/uv/) if needed, creates `.venv`, installs the libraries (macOS: `numpy coremltools pillow`;
NVIDIA: `onnxruntime-gpu`; Windows without NVIDIA: `onnxruntime-directml`; Linux without NVIDIA: `onnxruntime`), and
downloads the models if they are missing. Python and the libraries are not redistributed by trkAR: they are
downloaded on your computer from their sources, under their own licenses.

When you use `tools/matte` from a clone of this repository instead, download the models into `models/` as in the
Japanese steps above (the `curl -LO …` lines); git does not contain them.

From a terminal: `.venv/bin/python trkar_matte.py --list` (NDI sources), `.venv/bin/python trkar_matte.py --source "IPHONE (trkAR)" --model 1080`
(`.venv\Scripts\python.exe` on Windows). Options are in the table above (`--source`, `--name`, `--model 720|1080`,
`--backend auto|coreml|onnx`, `--units`, `--onnx-provider auto|cuda|dml|cpu`, `--parent-pid`).

Latency on a Mac mini M4 (1080p 30 fps, Core ML): about +48 ms over direct NDI, staying at 30 fps. On a weak GPU
(for example an NVIDIA MX150 on Windows) ONNX Runtime takes 300 ms or more per frame, too slow for real time.
