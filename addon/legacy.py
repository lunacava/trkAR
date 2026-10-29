"""名前を ARtrk から trkAR に変える前 (アドオン 0.9.20 まで、id artrk_camera) に保存した .blend を、そのまま使えるようにする。

開いたとき (load_post) とアドオンを有効にしたときに 1 回ずつ:
- アドオンが名前で探すデータ (画像 "ARtrk NDI"、オブジェクト "ARtrk Depth Screen"、"ARtrk テイクの置き場" など) と、
  深度スクリーンのマテリアルのノード ("ARtrk Mask" など) の名前を "trkAR …" に付け替える
  (マテリアルやカメラ背景は名前ではなくデータそのものを指しているので、付け替えてもつながったまま)
- シーンの設定 (scene.artrk) を scene.trkar へ写す (scene.trkar をまだ触っていないシーンだけ)。古い設定は消さない
- オブジェクト・メッシュの印 (artrk_key / artrk_grid) を trkar_… に写す

アクション ("ARtrk take_…") は付け替えない。テイクの take.json がその名前で探すため。
"""

import bpy

OLD, NEW = "ARtrk ", "trkAR "
SETTINGS_OLD, SETTINGS_NEW = "artrk", "trkar"
DATA = ("objects", "meshes", "images", "materials", "cameras", "collections", "node_groups")
MARKS = (("objects", "artrk_key", "trkar_key"), ("meshes", "artrk_grid", "trkar_grid"))


def _system(idb):
    """bpy.props の値の入れ物。Blender 5 はシステム用のプロパティ (keys() に出ない)、それより前は ID のプロパティ"""
    getter = getattr(idb, "bl_system_properties_get", None)
    if getter is not None:
        try:
            system = getter()
        except TypeError:
            system = None
        if system is not None:
            return system
    return idb


def rename_data():
    renamed = 0
    for name in DATA:
        blocks = getattr(bpy.data, name)
        for block in list(blocks):
            if block.library is None and block.name.startswith(OLD):
                new = NEW + block.name[len(OLD):]
                if blocks.get(new) is None:
                    block.name = new
                    renamed += 1
    for material in bpy.data.materials:
        if material.library is None and material.node_tree is not None:
            for node in material.node_tree.nodes:
                if node.name.startswith(OLD):
                    node.name = NEW + node.name[len(OLD):]
                    renamed += 1
    for name, old, new in MARKS:
        for block in getattr(bpy.data, name):
            if block.library is None and old in block.keys() and new not in block.keys():
                block[new] = block[old]
                renamed += 1
    return renamed


def copy_settings():
    copied = 0
    for scene in bpy.data.scenes:
        if scene.library is not None:
            continue
        props = _system(scene)
        if SETTINGS_OLD not in props.keys() or SETTINGS_NEW in props.keys():
            continue
        try:
            props[SETTINGS_NEW] = props[SETTINGS_OLD].to_dict()
            copied += 1
        except Exception as exc:  # noqa: BLE001  写せなければ既定の設定のまま
            print("trkAR: %s の古い設定 (scene.artrk) を写せません: %s" % (scene.name, exc))
    return copied


def migrate():
    renamed, copied = rename_data(), copy_settings()
    if renamed or copied:
        print("trkAR: 名前を変える前 (ARtrk) のデータを引き継ぎました (名前 %d、シーンの設定 %d)" % (renamed, copied))
    return renamed, copied
