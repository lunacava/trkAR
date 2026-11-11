"""画面の言語。元の文は英語、日本語は i18n_ja.JA の辞書から。

Blender はパネル・操作・設定の名前と説明を、描くときに bpy.app.translations で訳す (Blender の言語の
設定 = locale ごとの辞書を引く)。アドオンの設定の「表示言語」に合わせて、日本語の辞書を入れる locale を変える:

    AUTO  Blender が日本語 (ja_JP) のときだけ日本語、ほかは英語
    JA    Blender が英語でも日本語 (en_US などにも同じ辞書を入れる)
    EN    いつも英語 (辞書を入れない)

実行中に作る文 (% で数字を埋める・つなげる・状態として出す) は、埋める前に _t() を通して同じ辞書で訳す。
"""

import bpy

from .i18n_ja import JA

# Blender が引く文脈: "*" (ふつう) と "Operator" (操作の名前)
CONTEXTS = ("*", "Operator")
# JA のとき ja_JP のほかに入れる locale (英語の画面と、よく使われる代わり)
FORCE_LOCALES = ("ja_JP", "en_US", "en_GB", "")
_registered = {"on": False}


def _dictionary(mode):
    if mode == "EN":
        return {}
    table = {(ctx, en): ja for en, ja in JA.items() for ctx in CONTEXTS}
    if mode == "JA":
        locales = set(FORCE_LOCALES) | {bpy.app.translations.locale}
        return {loc: table for loc in locales}
    return {"ja_JP": table}


def register(mode="AUTO"):
    unregister()
    translations = _dictionary(mode)
    if translations:
        bpy.app.translations.register(__package__, translations)
        _registered["on"] = True


def unregister():
    if _registered["on"]:
        try:
            bpy.app.translations.unregister(__package__)
        except (ValueError, RuntimeError):
            pass
        _registered["on"] = False


def _t(message):
    """実行中に作る文を訳す (% で値を埋める前に呼ぶ)"""
    return bpy.app.translations.pgettext_iface(message)


def missing(messages):
    """日本語の訳がない文 (テスト用)"""
    return [m for m in messages if m not in JA]
