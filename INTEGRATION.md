# trkAR 連携の仕様 (Integration spec)

trkAR (iPhone / iPad アプリ) と trkAR Camera (Blender アドオン) を、自分のクラウドやツールにつなぐための仕様です。
trkAR 自身はクラウドに何も送りません。S3・Google Drive・Frame.io・社内の NAS などへの連携は、ここに書いた
**つなぎ目** を使って、利用者が自分で用意します。Claude などのコーディングエージェントにこのファイルを読ませて、
「自分の ○○ につなぐスクリプトを作って」と頼めば作れる粒度で書いています (`CLAUDE.md` も参照)。

- 仕様の版: 1 (フックの `TRKAR_HOOK_VERSION` と同じ)。項目は足すことはあっても、意味は変えません
- 対象: trkAR Camera 0.9.11 以降、trkAR 1.1 以降 (組み合わせは [5 章](#5-アプリとアドオンの組み合わせ))

つなぎ目は 4 つあります。

| つなぎ目 | 向き | 使いどころ |
| --- | --- | --- |
| [1. テイクのあとのコマンド (フック)](#1-テイクのあとのコマンド-フック) | Blender → 自分のスクリプト | 録ったテイク・書き出した動画をどこかに上げる、通知する |
| [2. テイクのフォルダ](#2-テイクのフォルダ) | ファイル | フックや別のツールが読むデータの形 |
| [3. 通信ポート (自動化)](#3-通信ポート-自動化) | 自分のツール → Blender | シーンを開く・テイクを選ぶ・書き出すを外から操作する |
| [4. iPhone から出るデータ](#4-iphone-から出るデータ-oscndiudp) | iPhone → PC | Blender を使わずに TouchDesigner などで直接受ける |

アプリとアドオンの版の組み合わせは [5 章](#5-アプリとアドオンの組み合わせ) にまとめています。

---

## 1. テイクのあとのコマンド (フック)

Blender の `編集 > プリファレンス > アドオン > trkAR Camera` の「テイクのあとに流すコマンド (連携)」、または
サイドバーの `録画・テイク > 録画・書き出しの設定` で設定します。

| 設定 | 既定 | 意味 |
| --- | --- | --- |
| コマンド | (空) | 流すコマンド。空なら何もしない |
| 録画を止めたら | オン | `recorded` のときに流す |
| 書き出したら | オン | `exported` のときに流す |
| 打ち切り (分) | 30 | これを過ぎたら止める |

### いつ流れるか

| event | タイミング | その時点であるもの |
| --- | --- | --- |
| `recorded` | 録画 (REC) を止めたとき。iPhone の REC ボタンで止めたときも同じ | `take.json`・`actions.json`・`video/`・(あれば) `alpha/`・`depth/` |
| `exported` | テイクの書き出しが最後まで終わったとき (途中で止めたときは流れない) | 上に加えて `render.mp4` か `render/`。`take.json` に `render` が足してある |

パネルの「コマンドを流す」で、選んでいるテイクにもう一度流せます (書き出してあれば `exported`、なければ `recorded`)。

### コマンドの書き方

- 1 行の文字列です。次のプレースホルダーを置き換えてから、空白で引数に分けて実行します (シェルは通しません。
  パイプ・リダイレクト・`&&` を使うならスクリプトの中に書いてください)。空白を含むパスは `"..."` で囲みます

  | プレースホルダー | 置き換わるもの |
  | --- | --- |
  | `{take_dir}` | テイクのフォルダの絶対パス |
  | `{event}` | `recorded` か `exported` |
  | `{render}` | `render.mp4` のパス。PNG 連番なら `render/` のパス。なければ空文字 |
  | `{take_json}` | `take.json` のパス |

- `~` は自分のホームフォルダに展開します
- 例: `python3 ~/trkar_upload.py {take_dir} {event}`

### 渡すもの

作業フォルダはテイクのフォルダです。親 (Blender) の環境変数に、次を足して渡します。

| 環境変数 | 内容 |
| --- | --- |
| `TRKAR_HOOK_VERSION` | この仕様の版 (`1`) |
| `TRKAR_EVENT` | `recorded` / `exported` |
| `TRKAR_TAKE_DIR` | テイクのフォルダ |
| `TRKAR_TAKE_JSON` | `take.json` のパス |
| `TRKAR_RENDER` | `render.mp4` か `render/` のパス。なければ空 |
| `TRKAR_BLEND` | 開いている .blend のパス (保存していなければ空) |
| `TRKAR_ADDON_VERSION` | アドオンの版 (例 `0.9.11`) |

Finder や Dock から起動した Blender は `PATH` が短いので、`/opt/homebrew/bin`・`/usr/local/bin`・`~/.local/bin` を
`PATH` の後ろに足しています。それ以外の場所のコマンドはフルパスで書いてください。

### 結果の返し方

- **終了コード 0 が成功**、それ以外は失敗としてパネルに赤く出します
- **標準出力の最後の行が JSON のオブジェクト**なら、その `message` (短い説明) と `url` (結果を開く URL) をパネルに出します。
  `url` があればパネルに「開く」ボタンが出ます
  ```json
  {"message": "S3 に 231 ファイル", "url": "https://example.com/takes/take_20261021_120000"}
  ```
- 標準出力・標準エラーは `<テイク>/hook.log` に追記されます (パネルの「ログ」で開けます)
- 結果は `<テイク>/hook.json` に残ります

  ```json
  {"event": "exported", "exit_code": 0, "started_utc": "2026-10-21T12:14:23Z",
   "finished_utc": "2026-10-21T12:14:31Z", "seconds": 8.1,
   "message": "S3 に 231 ファイル", "url": "https://example.com/takes/take_20261021_120000"}
  ```

### 守ってほしいこと

- 一度に 1 つずつ、来た順に流れます。Blender の操作は止めません。長い処理でもかまいませんが、打ち切りの時間を合わせてください
- 同じテイクに `recorded` と `exported` の 2 回流れます。**2 回目は増えたファイル (render.mp4 など) だけを足す**作りにしてください
- 認証情報 (キーやパスワード) をコマンドやスクリプトに直接書かないでください。AWS ならプロファイル (`AWS_PROFILE`) や
  `~/.aws/credentials`、ほかのサービスならキーチェーンや環境変数を使います
- テイクのフォルダの中身は書き換えないでください (`hook.json`・`hook.log` は trkAR が書きます)。作業ファイルは別の場所に

---

## 2. テイクのフォルダ

テイクは `録画・テイク > 録画・書き出しの設定` の「テイクのフォルダ」に、1 テイク 1 フォルダで置かれます
(既定は .blend の隣の `trkar_takes/`。保存していなければ macOS は `~/Movies/trkAR Takes`、Windows は `~/Videos/trkAR Takes`。
アドオン 0.10.0 より前の既定は `artrk_takes/`・`ARtrk Takes` で、そのフォルダがあって新しい方が無ければ続けてそちらを使います)。
フォルダ名は `take_YYYYMMDD_HHMMSS` (録り始めの日時、PC の現地時刻) です。

```
take_20261021_120000/
  take.json            テイクの情報と来歴 (下)
  actions.json         カメラの動き (Blender のアクションの F カーブ)
  video/000001.jpg     実写の色 (フレームごと、1 始まり 6 桁)
  alpha/000001.png     実写のアルファ (白黒 8bit)。アルファ付きの映像を受けていたときだけ
  depth/000001.npz     深度スクリーンの形 (深度を受けていたときだけ)
  render.mp4           書き出し (MP4 H.264)。PNG 連番を選んだときは render/0001.png …
  render_<.blend の名前>.mp4   書き出しの控え (別の背景で書き出すたびに増える)
  hook.json, hook.log  フックの結果とログ (1 章)
```

### take.json

値が分からない項目は `null` です。項目は増えることがあります (知らない項目は読み飛ばしてください)。

| 項目 | 型 | 意味 |
| --- | --- | --- |
| `name` | string | フォルダ名と同じ |
| `fps` | number | 録ったときのフレームレート |
| `frame_start`, `frame_end` | int | Blender のタイムライン上の範囲 |
| `frames` | int | 書いたフレーム数 (`video/` の枚数) |
| `dropped` | int | 書き込みが追いつかずに前のフレームで埋めた数 |
| `video`, `alpha` | bool | `video/`・`alpha/` があるか |
| `camera` | string | 動かしたカメラのオブジェクト名 |
| `action`, `lens_action` | string | カメラの動き・レンズのアクション名 (`actions.json` のキーと対応) |
| `recorded_at` | string | 録り始め (ISO 8601、PC の現地時刻とオフセット) |
| `slate` | object | `production`・`scene`・`cut`・`take` (int、0 は未設定)・`label` (例 `S01C01T01`) |
| `location` | object | `blend` (背景の .blend の名前)・`bpy_scene` (Blender のシーン名) |
| `environment` | object | `site` (撮影環境)・`host` (PC の呼び名)・`hostname`・`os`・`blender`・`addon` |
| `device` | object | 撮った端末。`system` (`trkAR`。0.10.0 より前は `ARtrk`)・`model`・`identifier`・`os`・`app`・`device_name`・`client_id`・`fps`・`hfov` (水平画角、度)・`tc_source` (`internal` か `ltc`) |
| `people` | object | `creator`・`operator` |
| `render` | object | 書き出したあとだけ。`format` (`MP4`/`PNG`)・`width`・`height`・`fps`・`frame_start`・`frame_end`・`blend`・`kept` (控えのファイル名) |
| `camera_offsets` | object | 0.9.12 以降。録り始めにカメラに乗っていたずれ。`mode` (`RELATIVE`/`ABSOLUTE`)・`scale`・`blender` (Blender 側のずれ: `location`・`pan_deg`・`tilt_deg`・`roll_deg`・`lens_mm`)・`iphone_rig` (iPhone の CTL: `position`・`yaw_deg`・`tilt_deg`・`roll_deg`・`focal_mm`・`fstop`・`focus_distance`、無ければ null)・`camera_parent`。**どれもカメラの動き (`actions.json`) に焼き込み済み**で、再生で足し直すものではない (どのずれで録ったかの記録) |
| `placement` | object | 0.9.12 以降。再生中に「背景のオフセット」で置いた場所 (テイクごと)。`matrix` (置き場の Empty の 4×4 のワールド行列)・`front_distance` (人の居る所を決めた「手前の距離」)。無ければ録ったときの位置で再生・書き出しする |

### actions.json

```json
{"action": {"name": "trkAR take_…", "curves": [
   {"data_path": "location", "array_index": 0,
    "points": [{"co": [frame, value], "interpolation": "BEZIER", "easing": "AUTO",
                "handle_left": [x, y], "handle_right": [x, y], ...}, ...]}, ...]},
 "lens_action": {"name": "trkAR take_… Lens", "curves": [{"data_path": "lens", ...}]}}
```

`data_path` は Blender のカメラの `location` (m、Blender のワールド座標、Z 上)・`rotation_euler` (ラジアン)・`lens` (mm) などです。

### depth/NNNNNN.npz

NumPy の `np.load` で読めます。`co` (頂点の位置、float32。`co.reshape(-1, 3)` で x y z の並びにする)、
`face_valid` (面が有効か、uint8)、`mask` (人物マスク、uint8、0〜255)、`key` (深度の格子の `[幅, 高さ, 間引き]`)、
`meta` (JSON の文字列) です。
深度から自分で点群を作るなら、4 章の UDP を直接受けるほうが扱いやすいです。

---

## 3. 通信ポート (自動化)

外のツール (Claude などのエージェント、自作のスクリプト、Griptape Nodes のノード) から Blender を操作する口です。
サイドバーの `録画・テイク > 通信ポート (自動化)` で開きます。**127.0.0.1 だけで待ち受け**、決まった命令だけを
受けます (任意の Python は受けません)。既定のポートは 9310 です。

```
POST http://127.0.0.1:9310/command   {"command": "<命令>", "args": {...}}
  → {"ok": true, "result": {...}}  /  {"ok": false, "error": "..."}
GET  http://127.0.0.1:9310/state     state の短縮
```

| 命令 | args | すること |
| --- | --- | --- |
| `state` | なし | 今の状態 (開いている .blend、選んでいるテイク、再生・録画中か、書き出しの進み具合、フックの結果) |
| `open_scene` | `path` | .blend を開く (録画・書き出しの最中は断る) |
| `list_takes` | なし | テイクのフォルダの一覧 (`name`・`path`・`frames`・`fps`・`playable`・`render`・`kept`) |
| `select_take` | `name` (空なら最新) | テイクを選んで再生できる状態にする |
| `place_anchor` | `location` [x, y, z]・`heading_deg` (どちらも省略可) | 背景の中でのテイクの置き場 (床の高さと向き) |
| `remove_anchor` | なし | 置き場を消す |
| `configure` | 書き出しの設定 | `take_render_mode` (`VIEWPORT`/`RENDER`)・`take_render_format` (`MP4`/`PNG`)・`take_render_size`・`take_use_range`・`take_range_start`・`take_range_end`・`take_range_mode`・`take_range_seconds`・`take_render_samples` だけ変えられる |
| `export_take` | なし | 書き出しを始めてすぐ返す。終わりは `state` の `export.running` が false になるのを見る |

書き出しが終わればフック (1 章) も流れるので、「通信ポートで書き出す → フックで上げる」とつなげられます。

---

## 4. iPhone から出るデータ (OSC・NDI・UDP)

Blender を使わずに、TouchDesigner や自作のツールで直接受けるときの形です。送り先は iPhone の設定画面の
`PC IP Address` / `Port` (OSC、既定 8000) と `Server IP Address` / `Port` (HTTP、既定 8081) です。

### 座標系

ARKit の座標系です。右手系・Y 上・カメラの前方は -Z、単位はメートル。回転はオイラー角 (度) で、
roll (Z) → pitch (X) → yaw (Y) の順にかかります (R = Ry · Rx · Rz)。
Blender に入れるときはワールドを X 軸まわりに +90° 回します ((x, y, z) → (x, −z, y))。

### OSC (UDP)

| アドレス | 引数 | 周期 |
| --- | --- | --- |
| `/client/current` | `clientID` (s)・`appMode` (i、常に 0)・`cameraID` (i)・`px py pz` (f、m)・`rx ry rz` (f、度)・`tcH tcM tcS tcF` (i、タイムコード)・`fps` (i、FPS の種類)・`floorHeight` (f、SET GND からの高さ m、未設定は 0)・`hFOV` (f、水平画角 度)・`frameTimestamp` (d、ARFrame.timestamp 秒)・`tcSeconds` (d、0 時からの秒)・`tcSource` (i、0 内部時計 / 1 LTC) | 毎フレーム |
| `/client/rig` | `clientID`・`cameraID`・`tx ty tz` (m)・`yaw tilt roll` (度)・`focalMM` (0 = AR の画角に連動)・`fStop` (0 = 被写界深度なし)・`focusDistance` | 毎フレーム |
| `/client/rec` | `clientID`・`start` (i、1 開始 / 0 停止) | REC ボタン |
| `/client/info` | `clientID`・機種の識別子 (例 `iPhone17,1`)・OS 名・OS の版・アプリの版・ビルド・端末名 (すべて s) | 送信開始時と数秒おき |
| `/worldanchor/match` | `clientID` | ANC ボタン |

引数は末尾に足すことがあります (前からの並びは変えません)。`frameTimestamp` は NDI 映像の timecode
(= ARFrame.timestamp × 10⁷、100ns 単位) と同じ時計なので、姿勢と映像をフレームより細かく対応付けられます。

### NDI

| ソース名 | 中身 |
| --- | --- |
| `<端末名> (trkAR)` | カメラの映像 (CAM)。人物マスクを選んでいればアルファ付き |
| `<端末名> (trkAR-Depth)` | 深度 (DPT、16bit)。メタデータは `<trkARDepth …/>`。NDI の圧縮がかかるので、正確な値が要るなら下の UDP を使う |

アプリ 1.1.1 までは `(ARtrk)`・`(ARtrk-Depth)`・`<ARtrkDepth …/>` (名前を trkAR に揃える前)。受ける側はどちらも受けてください。

NDI® is a registered trademark of Vizrt NDI AB. 詳しくは [ndi.video](https://ndi.video/) を参照してください。

### 深度 (UDP、無圧縮)

既定のポートは 9100 (送り先は `PC IP Address`)。1 フレームを行ごとのパケットに分けて送ります (リトルエンディアン)。

| オフセット | 大きさ | 内容 |
| --- | --- | --- |
| 0 | 4 | magic `ARD3` |
| 4 | 4 | `frame_id` (UInt32) |
| 8 | 2 | `width` (UInt16) |
| 10 | 2 | `height` (UInt16) |
| 12 | 2 | `row_start` (UInt16) |
| 14 | 2 | `row_count` (UInt16) |
| 16 | 2 | `packet_index` (UInt16) |
| 18 | 2 | `packet_count` (UInt16) |
| 20 | 16 | `fx fy cx cy` (Float32 × 4、深度の解像度で) |
| 36 | 4 | `flags` (bit 0 縦位置、bit 1 信頼度なし、bit 2 人物マスクだけ) |
| 40 | 8 | `timecode` (Int64、100ns。NDI 映像と同じ時計) |
| 48 | … | `row_count × width` 個の UInt16。bit 0–12 深度 (mm、0 は無効)、bit 13 人物、bit 14–15 信頼度 (0 low / 1 medium / 2 high) |

`packet_count` 枚がそろわなかったフレームは捨ててください。

---

## 5. アプリとアドオンの組み合わせ

iPhone アプリ (trkAR) と Blender アドオン (trkAR Camera) は、4 章の OSC・NDI・UDP の形式でだけつながっています。
**この形式が同じなら、アプリとアドオンの版はどう組み合わせても動きます。** どちらを先に更新してもかまいません。

| 形式の版 | アプリ | アドオン | 変わったこと |
| --- | --- | --- | --- |
| 1 | 1.1、1.1.1 | 0.9.11、0.9.12 | (最初の版) |
| 1 (名前を trkAR に) | 1.1.2 | 0.10.0 | NDI のソース名とメタデータ・テイクの名前・アドオンの id (`artrk_camera` → `trkar_camera`) を trkAR に揃えた。OSC と UDP は同じ |

- 1.1 → 1.1.1 で変わったのは画面 (REC ボタンなど) だけ、0.9.11 → 0.9.12 で変わったのはテイクの置き場と記録だけで、どちらも形式 1 のまま
- アドオン 0.10.0 は古い名前 (`(ARtrk)`、`<ARtrkDepth>`、`artrk_takes/`、0.9.x で保存した .blend の設定) も読む。
  アドオン 0.9.x とアプリ 1.1.2 の組み合わせでは、深度の **NDI** だけ読めない (メタデータの名前が変わったため。深度の UDP は読める)
- アドオンは id が変わったので、0.9.x を入れている場合は 0.10.0 を入れてから 0.9.x (`trkAR Camera`、id `artrk_camera`) を外す
- 形式を変えるときの決まり: OSC の引数は**末尾に足すだけ**で、前からの並びは変えない。受ける側は、知らない末尾の引数を読み飛ばし、
  足した引数が来ない古い送り手でもこれまでどおり動く。深度の UDP は magic (`ARD1`〜`ARD3`) で見分けて、どれも読める
- 形式を変えたら、この表に行を足し、アプリとアドオンの最低の組み合わせをここに書く
- どの版で録ったかは、テイクの `take.json` の `device.app` (アプリの版) と `environment.addon` (アドオンの版) に残る

## 見本

- [`examples/s3_upload.py`](examples/s3_upload.py): テイクを Amazon S3 に上げるフック (aws CLI を使う)

自分の連携を作るときは、見本をコピーして、上げ先と認証の部分を書き換えるのが近道です。

---

この文書は CC BY 4.0 (© 2026 Hiroki Mori / trkAR)、見本のコードは Apache-2.0 です。
**使ってくれたら、ひとこと連絡をもらえるとうれしいです** (任意): [「使っています」の Issue](https://github.com/lunacava/trkAR/issues/new?template=used-by.md)
