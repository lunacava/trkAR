# trkAR

AR camera tracking for virtual production — iPhone / iPad app and Blender add-on.

> trkAR は lunacava の個人のプロジェクトです。所属する会社・団体の製品やサービスではなく、その見解を示すものでもありません。
> trkAR is a personal project by lunacava. It is not a product or service of, and does not represent the views of, any company or organization the author is affiliated with.

- Website: https://lunacava.github.io/trkAR/
- Blender add-on (trkAR Camera): [Releases](https://github.com/lunacava/trkAR/releases) / source in [`addon/`](addon/)
- Integration spec (hooks, take folder, automation port, OSC / NDI / UDP): [`INTEGRATION.md`](INTEGRATION.md)
- Using Claude or another coding agent to connect trkAR to your cloud: see [`CLAUDE.md`](CLAUDE.md)

## Files

Website pages are built from `site/src/ja/*.html` and `site/src/en/*.html` (body only, same structure in both languages) with `python3 site/build.py`, which writes `ja/<page>.html` and `en/<page>.html`. Edit the sources, not the built pages. The root `<page>.html` files only send visitors to `ja/` or `en/` (the language chosen with the JA / EN switch, otherwise the browser language; English when unknown), so the URLs registered in App Store Connect and the app keep working.

- `index` — about
- `start` — getting started (app → add-on → record → export)
- `app` — app guide (screen, buttons, settings, troubleshooting; opened from the app's Settings > Guide)
- `generation` — files for reference-based video generation models (`for_generation/`)
- `support` — support (App Store Connect: Support URL `support.html`)
- `privacy` — privacy policy (App Store Connect: Privacy Policy URL `privacy.html`)
- `terms` — EULA: Apple Standard EULA + NDI® SDK terms (App Store Connect: custom License Agreement; the English text is authoritative)
- `integration` — how to connect trkAR to your own cloud / tools
- `addon/` — trkAR Camera, public edition (GPL-3.0-or-later, see `addon/LICENSE`)
- `tools/screen_ndi/` — source of the add-on's screen-capture NDI helper `addon/bin/trkar-screen-ndi` (GPL-3.0-or-later, build with `build.sh`)
- `examples/` — hook examples (Apache-2.0)
- `tools/matte/` — AI person cut-out relay (RVM → RGBA NDI) for the add-on (GPL-3.0-or-later, see `tools/matte/LICENSE`)
- `assets/OCRA.otf` — OCR-A (Wales / Skala), see `assets/OCRA.otf.LICENSE.txt`

## ライセンス / License

このリポジトリは部分ごとにライセンスが違います (全体で 1 つのライセンスではありません。一覧は [`LICENSE`](LICENSE))。iOS / iPadOS アプリ本体はオープンソースではなく、App Store の EULA (`terms.html`) で配布しています。

Parts of this repository are under different licenses (no single license for the whole repo; summary in [`LICENSE`](LICENSE)). The iOS / iPadOS app itself is not open source; it is distributed under the App Store EULA (`terms.html`).

**ざっくり言うと**: どれも商用を含めて自由に使えます。配り直すときに作者の表示 (© 2026 lunacava / trkAR) を残してください。
アドオンだけは Blender の決まりで GPL なので、アドオンそのものを改変して配るときはソースも一緒に公開してください。

**In short**: everything here can be used freely, including commercially. Keep the credit (© 2026 lunacava / trkAR)
when you redistribute. Only the add-on is GPL (as Blender requires): if you distribute a modified add-on, publish its source too.

| 対象 | ライセンス |
| --- | --- |
| `addon/` (Blender アドオン trkAR Camera) | GPL-3.0-or-later (`addon/LICENSE`) |
| `tools/matte/` (AI の人物の切り抜きの中継) | GPL-3.0-or-later (`tools/matte/LICENSE`)。RVM のモデルは含まない (配布元から落とす) |
| `tools/screen_ndi/` (アドオンの `bin/trkar-screen-ndi` のソース) | GPL-3.0-or-later (`addon/LICENSE` と同じ) |
| `examples/` (フックの見本) | Apache-2.0 (`examples/LICENSE`、配るときは `examples/NOTICE` も残す) |
| 文書 (`INTEGRATION.md`・`CLAUDE.md`・サイトの文章) とサンプルのシーン | CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/) — クレジット「© 2026 lunacava / trkAR」と出典の表示、変えたらそのことの明記 |
| `assets/OCRA.otf` | OCR-A (Wales / Skala)、`assets/OCRA.otf.LICENSE.txt` |

**使ってくれたら、ひとこと連絡をもらえるとうれしいです。** ライセンスの条件ではなく、お願いです。
[「使っています」の Issue](https://github.com/lunacava/trkAR/issues/new?template=used-by.md) からどうぞ。

**If you use trkAR, the add-on, the examples or the scenes, I'd love to hear about it** — not a license condition, just a request.
Please open a ["Used by" issue](https://github.com/lunacava/trkAR/issues/new?template=used-by.md).

NDI® is a registered trademark of Vizrt NDI AB. trkAR is not affiliated with, endorsed or sponsored by Vizrt NDI AB.
