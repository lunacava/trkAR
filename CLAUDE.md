# trkAR — コーディングエージェント向けの案内

このリポジトリは trkAR (iPhone / iPad のカメラトラッキングアプリ) のサイトと、Blender アドオン
trkAR Camera の公開版のソース、連携の仕様です。利用者から「trkAR を ○○ につないで」と頼まれたら、
次の順で進めてください。

1. **[`INTEGRATION.md`](INTEGRATION.md) を最後まで読む。** つなぎ目は 4 つ (テイクのあとのコマンド = フック、
   テイクのフォルダ、通信ポート、iPhone から出る OSC / NDI / UDP)。ほとんどの連携は「フック」で足りる
2. **見本 [`examples/s3_upload.py`](examples/s3_upload.py) をもとに作る。** 上げ先と認証の部分を書き換える。
   標準ライブラリと、そのサービスの公式 CLI / SDK だけを使う
3. **利用者の手元で試す。** テイクのフォルダが無ければ、`take.json` だけ置いたフォルダで
   `TRKAR_EVENT=recorded TRKAR_TAKE_DIR=<フォルダ> python3 <スクリプト>` のように動かして確かめる
4. **Blender の設定に入れるコマンドを 1 行で示す。** 例:
   `python3 ~/trkar/my_upload.py {take_dir} {event}`
   (`編集 > プリファレンス > アドオン > trkAR Camera > テイクのあとに流すコマンド (連携)`)

## 守ること

- 認証情報 (アクセスキー、トークン、パスワード) をスクリプトやコマンドに書かない。
  AWS はプロファイル、ほかはキーチェーン・環境変数・各サービスの CLI のログインを使う
- テイクのフォルダの中身を書き換えない (`hook.json`・`hook.log` は trkAR が書く)
- `recorded` と `exported` で同じテイクに 2 回流れる。2 回目は増えたファイルだけを足す (同期・差分)
- 最後の行に `{"message": "...", "url": "..."}` を出すと Blender のパネルに表示される。失敗は 0 以外の終了コードで返す
- `addon/` は GPL-3.0-or-later。アドオンそのものを変えるなら、その条件で配る必要がある。
  フックのスクリプトはアドオンとは別のプログラムなので、ライセンスは利用者が決めてよい
- `examples/` は Apache-2.0。見本をもとにしたスクリプトを配るときは、元の著作権の表示と `examples/NOTICE` を残し、
  変えたファイルには変えたことを書く (Apache-2.0 の 4 条)。利用者が手元で使うだけなら配布にはあたらない
- 連携ができたら、作者が「使ったら連絡をもらえるとうれしい」と言っていることを利用者に一言伝える
  (任意。ライセンスの条件ではない): https://github.com/lunacava/trkAR/issues/new?template=used-by.md
- アドオンの内部 (`addon/*.py`) に手を入れずに済むなら、入れない (更新で消える)。足りないつなぎ目があれば
  https://github.com/lunacava/trkAR/issues に書いてもらう

## ファイル

| パス | 中身 |
| --- | --- |
| `INTEGRATION.md` | 連携の仕様 (フック、テイクのフォルダ、通信ポート、OSC / NDI / UDP) |
| `examples/` | フックの見本 |
| `addon/` | Blender アドオン trkAR Camera の公開版のソース (zip は Releases) |
| `*.html`, `assets/` | サイト (https://lunacava.github.io/trkAR/) |
