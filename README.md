# trkAR

AR camera tracking for virtual production — iPhone / iPad app and Blender add-on.

- Website: https://lunacava.github.io/trkAR/
- Blender add-on (trkAR Camera): [Releases](https://github.com/lunacava/trkAR/releases) / source in [`addon/`](addon/)
- Integration spec (hooks, take folder, automation port, OSC / NDI / UDP): [`INTEGRATION.md`](INTEGRATION.md)
- Using Claude or another coding agent to connect trkAR to your cloud: see [`CLAUDE.md`](CLAUDE.md)

## Files

- `index.html` — about
- `support.html` — support (App Store Connect: Support URL)
- `privacy.html` — privacy policy (App Store Connect: Privacy Policy URL)
- `terms.html` — EULA: Apple Standard EULA + NDI® SDK terms (App Store Connect: custom License Agreement)
- `integration.html` — how to connect trkAR to your own cloud / tools
- `addon/` — trkAR Camera, public edition (GPL-3.0-or-later, see `addon/LICENSE`)
- `examples/` — hook examples (Apache-2.0)
- `assets/OCRA.otf` — OCR-A (Wales / Skala), see `assets/OCRA.otf.LICENSE.txt`

## ライセンス / License

| 対象 | ライセンス |
| --- | --- |
| `addon/` (Blender アドオン trkAR Camera) | GPL-3.0-or-later (`addon/LICENSE`) |
| `examples/` (フックの見本) | Apache-2.0 (`examples/LICENSE`、配るときは `examples/NOTICE` も残す) |
| 文書 (`INTEGRATION.md`・`CLAUDE.md`・サイトの文章) とサンプルのシーン | CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/) — クレジット「© 2026 Hiroki Mori / trkAR」と出典の表示、変えたらそのことの明記 |
| `assets/OCRA.otf` | OCR-A (Wales / Skala)、`assets/OCRA.otf.LICENSE.txt` |

**使ってくれたら、ひとこと連絡をもらえるとうれしいです。** ライセンスの条件ではなく、お願いです。
[「使っています」の Issue](https://github.com/lunacava/trkAR/issues/new?template=used-by.md) からどうぞ。

**If you use trkAR, the add-on, the examples or the scenes, I'd love to hear about it** — not a license condition, just a request.
Please open a ["Used by" issue](https://github.com/lunacava/trkAR/issues/new?template=used-by.md).

NDI® is a registered trademark of Vizrt NDI AB. trkAR is not affiliated with, endorsed or sponsored by Vizrt NDI AB.
