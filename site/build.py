#!/usr/bin/env python3
"""trkAR のサイトを組み立てる。

site/src/<lang>/<page>.html (本文だけ) を共通のひな形で包み、
ja/<page>.html と en/<page>.html に書き出す。根元の <page>.html は、
前に選んだ言語 (localStorage) か、ブラウザの言語で ja/ か en/ へ案内する
(分からないときは英語)。App Store Connect に登録した URL
(support.html / privacy.html / terms.html) とアプリの Guide (app.html) は根元のまま使える。

    python3 site/build.py
"""
from __future__ import annotations

import html
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "site" / "src"
LANGS = ("ja", "en")
BASE_URL = "https://lunacava.github.io/trkAR/"

# ページの順はナビの順
PAGES = {
    "index": {
        "nav": {"ja": "ホーム", "en": "Home"},
        "title": {"ja": "trkAR — バーチャルプロダクションの AR カメラトラッキング",
                  "en": "trkAR — AR camera tracking for virtual production"},
        "desc": {"ja": "trkAR — iPhone / iPad をバーチャルプロダクション用のカメラトラッカーにするアプリ",
                 "en": "trkAR turns your iPhone or iPad into a camera tracker for virtual production"},
    },
    "start": {
        "nav": {"ja": "はじめかた", "en": "Start"},
        "title": {"ja": "trkAR — はじめかた", "en": "trkAR — Getting started"},
        "desc": {"ja": "trkAR と Blender アドオン trkAR Camera のはじめかた",
                 "en": "Getting started with trkAR and the Blender add-on trkAR Camera"},
    },
    "app": {
        "nav": {"ja": "アプリ", "en": "App"},
        "title": {"ja": "trkAR — アプリの使い方", "en": "trkAR — App guide"},
        "desc": {"ja": "iPhone / iPad アプリ trkAR の画面と使い方",
                 "en": "The screen and how to use the trkAR iPhone / iPad app"},
    },
    "generation": {
        "nav": {"ja": "生成用の素材", "en": "Generation"},
        "title": {"ja": "trkAR — 生成用の素材", "en": "trkAR — Files for video generation"},
        "desc": {"ja": "trkAR のテイクから、参照を入れられる動画生成モデルに渡す中間ファイルを作る",
                 "en": "Make intermediate files from a trkAR take for video generation models that accept references"},
    },
    "integration": {
        "nav": {"ja": "連携", "en": "Integration"},
        "title": {"ja": "trkAR — 連携", "en": "trkAR — Integration"},
        "desc": {"ja": "trkAR を自分のクラウドやツールにつなぐ",
                 "en": "Connect trkAR to your own cloud or tools"},
    },
    "support": {
        "nav": {"ja": "サポート", "en": "Support"},
        "title": {"ja": "trkAR — サポート", "en": "trkAR — Support"},
        "desc": {"ja": "trkAR のサポートとよくある質問", "en": "trkAR support and FAQ"},
    },
    "privacy": {
        "nav": {"ja": "プライバシー", "en": "Privacy"},
        "title": {"ja": "trkAR — プライバシーポリシー", "en": "trkAR — Privacy Policy"},
        "desc": {"ja": "trkAR のプライバシーポリシー", "en": "trkAR privacy policy"},
    },
    "terms": {
        "nav": {"ja": "規約", "en": "Terms"},
        "title": {"ja": "trkAR — 利用規約", "en": "trkAR — Terms of Use"},
        "desc": {"ja": "trkAR の利用規約 (概要)", "en": "trkAR terms of use (EULA)"},
    },
}

FOOTER = {
    "ja": (
        "<p>trkAR は lunacava の個人のプロジェクトです。所属する会社・団体の製品やサービスではなく、"
        "その見解を示すものでもありません。</p>\n"
        "<p>© 2026 lunacava. NDI® は Vizrt NDI AB の登録商標です。trkAR は Vizrt NDI AB と提携しておらず、"
        "承認・後援も受けていません。</p>"
    ),
    "en": (
        "<p>trkAR is a personal project by lunacava. It is not a product or service of, and does not "
        "represent the views of, any company or organization the author is affiliated with.</p>\n"
        "<p>© 2026 lunacava. NDI® is a registered trademark of Vizrt NDI AB. trkAR is not affiliated with, "
        "endorsed or sponsored by Vizrt NDI AB.</p>"
    ),
}

# 切り替えたときに選んだ言語を覚え、ページ内の位置 (#...) も持っていく
SWITCH_JS = (
    "<script>document.querySelectorAll('.lang-switch a').forEach(function(a){"
    "a.addEventListener('click',function(e){"
    "try{localStorage.setItem('trkar-lang',a.dataset.lang)}catch(_){}"
    "if(location.hash){e.preventDefault();location.href=a.getAttribute('href')+location.hash}"
    "})})</script>"
)


def head_links(page: str, prefix: str) -> str:
    out = []
    for lang in LANGS:
        out.append(f'<link rel="alternate" hreflang="{lang}" href="{BASE_URL}{lang}/{page}.html">')
    out.append(f'<link rel="alternate" hreflang="x-default" href="{BASE_URL}{page}.html">')
    out.append(f'<link rel="icon" href="{prefix}assets/icon.png">')
    out.append(f'<link rel="apple-touch-icon" href="{prefix}assets/apple-touch-icon.png">')
    out.append(f'<link rel="stylesheet" href="{prefix}assets/style.css">')
    return "\n".join(out)


def render(page: str, lang: str) -> str:
    meta = PAGES[page]
    body = (SRC / lang / f"{page}.html").read_text(encoding="utf-8").strip()
    # 本文は根元からの相対で書いてあるので、assets だけ 1 つ上を指す
    body = body.replace('src="assets/', 'src="../assets/').replace('href="assets/', 'href="../assets/')
    nav = "".join(
        f'<a href="{p}.html"' + (' aria-current="page"' if p == page else "") + f'>{PAGES[p]["nav"][lang]}</a>'
        for p in PAGES
    )
    switch = []
    for other in LANGS:
        label = other.upper()
        if other == lang:
            switch.append(f'<span aria-current="true">{label}</span>')
        else:
            switch.append(f'<a href="../{other}/{page}.html" hreflang="{other}" lang="{other}" data-lang="{other}">{label}</a>')
    return f"""<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(meta["title"][lang])}</title>
<meta name="description" content="{html.escape(meta["desc"][lang])}">
{head_links(page, "../")}
</head>
<body>
<header>
  <img src="../assets/icon.png" alt="trkAR icon">
  <div><h1>trkAR</h1><p class="tagline">AR camera tracking for virtual production</p></div>
  <div class="lang-switch">{' / '.join(switch)}</div>
</header>
<nav>{nav}</nav>

{body}

<footer>
{FOOTER[lang]}
</footer>
{SWITCH_JS}
</body>
</html>
"""


def redirect(page: str) -> str:
    """根元の URL。前に選んだ言語 → ブラウザの言語 (ja なら ja、ほかは en) の順で決める。"""
    meta = PAGES[page]
    title = f'{meta["title"]["en"]} / {meta["title"]["ja"].removeprefix("trkAR — ")}'
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(meta["desc"]["en"])}">
<script>(function(){{var l;try{{l=localStorage.getItem('trkar-lang')}}catch(e){{}}
if(l!=='ja'&&l!=='en'){{var n=(navigator.languages&&navigator.languages[0])||navigator.language||'';l=/^ja\\b/i.test(n)?'ja':'en'}}
location.replace(l+'/{page}.html'+location.search+location.hash)}})()</script>
<noscript><meta http-equiv="refresh" content="0; url=en/{page}.html"></noscript>
{head_links(page, "")}
</head>
<body>
<p><a href="en/{page}.html" hreflang="en">English</a> / <a href="ja/{page}.html" hreflang="ja" lang="ja">日本語</a></p>
</body>
</html>
"""


def main() -> None:
    for lang in LANGS:
        (ROOT / lang).mkdir(exist_ok=True)
        for page in PAGES:
            (ROOT / lang / f"{page}.html").write_text(render(page, lang), encoding="utf-8")
    for page in PAGES:
        (ROOT / f"{page}.html").write_text(redirect(page), encoding="utf-8")
    print(f"built {len(PAGES)} pages x {len(LANGS)} languages + {len(PAGES)} redirects")


if __name__ == "__main__":
    main()
