"""trkAR Camera — trkAR (iPhone) の OSC でカメラを動かし、そのカメラ視点を NDI で返す。"""

import importlib
import json
import math
import os
import subprocess
import sys
import time

# 上書きインストール時、Blender は __init__ だけを読み直してサブモジュールは
# 古いまま使い回す。依存の浅い順に読み直して新旧が混ざらないようにする
for _name in ("osc", "ndi", "ndi_output", "ndi_input", "depth_input", "matte", "take", "awscli", "griptape", "ledger",
              "look", "hooks", "tracking", "bridge"):
    _module = sys.modules.get("%s.%s" % (__name__, _name))
    if _module is not None:
        importlib.reload(_module)

import bpy
from bpy.app.handlers import persistent
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from mathutils import Matrix, Vector

from . import bridge, depth_input, hooks, ndi_input, osc, take, tracking

STUDIO = False
_DEFAULT_PRODUCTION = ""
from .depth_input import DepthInput
from .ndi_input import NDIInput
from .ndi_output import NDIOutput

ndi_output = NDIOutput()
ndi_in = NDIInput()
depth_in = DepthInput()
depth_in.video = ndi_in
tracking.video = ndi_in
recorder = take.Recorder(ndi_in, depth_in)
player = take.Player(ndi_in, depth_in)


def _addon_version():
    """blender_manifest.toml の version (take.json の environment.addon に書く)。読めなければ None"""
    try:
        import tomllib

        with open(os.path.join(os.path.dirname(__file__), "blender_manifest.toml"), "rb") as f:
            return tomllib.load(f).get("version")
    except (ImportError, OSError, ValueError):
        return None


ADDON_VERSION = _addon_version()


def _is_camera(_self, obj):
    return obj.type == "CAMERA"


def _reset_smoothing(_self, _context):
    tracking.reset_smoothing()


def _reanchor_if_relative(self, context):
    # 相対モードでスケールやモードを変えたとき、今のカメラ位置から続けて動かす (飛ばさない)
    if self.mode == "RELATIVE" and tracking.server.running:
        tracking.reanchor(self.camera or context.scene.camera)
    else:
        tracking.reset_smoothing()


def _update_depth_edge(self, _context):
    depth_input.update_edge(self)


def _reset_take(self, _context):
    # Scene / Cut が変わったら Take は 1 から。どちらも 0 (スレートを設定していない) なら 0 のまま (S00C00T00)
    self.slate_take = 1 if (self.slate_scene or self.slate_cut) else 0


class ARTRK_Preferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    ndi_library_path: StringProperty(
        name="NDI ライブラリ",
        description="libndi.dylib / Processing.NDI.Lib.x64.dll のパス。空なら自動検出",
        subtype="FILE_PATH",
    )


    # --- 撮影の来歴 (take.json の slate / environment / people)。.blend ごとではなくこの PC の設定に持つ
    # (撮影中に背景の .blend を開き替えても、スレートの番号が続くように) ---
    slate_production: StringProperty(name="Production", default=_DEFAULT_PRODUCTION,
                                     description="作品の名前 (take.json の slate.production)。空なら記録しない (null)")
    slate_scene: IntProperty(name="Scene", default=0, min=0, update=_reset_take,
                             description="シーン番号。0 は未設定 (S00C00T00)。変えると Take は 1 に戻る")
    slate_cut: IntProperty(name="Cut", default=0, min=0, update=_reset_take,
                           description="カット番号。0 は未設定。変えると Take は 1 に戻る")
    slate_take: IntProperty(name="Take", default=0, min=0,
                            description="次に録るテイクの番号。Scene か Cut を設定していれば、REC を始めるたびに 1 つ増える")
    shot_site: StringProperty(name="撮影環境", default="",
                              description="撮影している場所・イベント (例 InterBee)。空なら記録しない (null)")
    shot_host: StringProperty(name="PC の表示名", default="",
                              description="この PC の呼び名 (例 Mac mini)。空なら記録しない (null)。"
                                          "ホスト名 (hostname) は別に自動で記録する")
    shot_creator: StringProperty(name="製作者", default="", description="空なら記録しない (null)")
    shot_operator: StringProperty(name="撮影者", default="", description="空なら記録しない (null)")

    # --- テイクのあとに流すコマンド (クラウドなどへの連携の口。hooks.py / INTEGRATION.md) ---
    hook_command: StringProperty(
        name="コマンド",
        description="テイクを録ったあと・書き出したあとに流すコマンド。{take_dir} {event} {render} {take_json} を"
                    "置き換える (例: python3 ~/trkar_upload.py {take_dir} {event})。空なら何もしない")
    hook_on_record: BoolProperty(name="録画を止めたら", default=True,
                                 description="録画を止めたとき (event = recorded) にコマンドを流す")
    hook_on_export: BoolProperty(name="書き出したら", default=True,
                                 description="書き出しが終わったとき (event = exported) にコマンドを流す")
    hook_timeout: IntProperty(name="打ち切り (分)", default=30, min=1, max=24 * 60,
                              description="コマンドがこの時間で終わらなければ打ち切る")


    def draw(self, _context):
        self.layout.prop(self, "ndi_library_path")
        box = self.layout.box()
        box.label(text="撮影の来歴 (take.json)", icon="FILE_MOVIE")
        _draw_slate_settings(box, self)
        box = self.layout.box()
        box.label(text="テイクのあとに流すコマンド (連携)", icon="SCRIPT")
        _draw_hook_settings(box, self)


class ARTRK_Settings(bpy.types.PropertyGroup):
    # --- 受信 (OSC) ---
    port: IntProperty(name="OSC ポート", default=8000, min=1, max=65535,
                      description="trkAR の SystemPC OSC Port と合わせる")
    camera: PointerProperty(name="カメラ", type=bpy.types.Object, poll=_is_camera,
                            description="動かすカメラ。空ならシーンのアクティブカメラ")
    camera_id_filter: IntProperty(name="Camera ID", default=-1, min=-1,
                                  description="この trackingCameraID だけ受ける (-1 = すべて)")
    client_id_filter: StringProperty(name="Client ID", default="",
                                     description="この clientID だけ受ける (空 = すべて)")
    mode: EnumProperty(
        name="基準",
        items=(
            ("RELATIVE", "カメラの現在位置", "受信開始時のカメラ位置・向きを基準に、iPhone が動いた分だけ動かす"),
            ("ABSOLUTE", "iPhone の原点", "iPhone の座標をそのまま使う (オフセットで合わせる)"),
        ),
        default="RELATIVE",
        update=_reanchor_if_relative,
    )
    scale: FloatProperty(name="スケール", default=1.0, min=0.001, soft_max=100.0,
                         description="移動量の倍率 (1 = 実寸 1m が Blender の 1m)",
                         update=_reanchor_if_relative)
    location_offset: FloatVectorProperty(name="位置オフセット", subtype="TRANSLATION", size=3,
                                         update=_reset_smoothing)
    yaw_offset: FloatProperty(name="向きオフセット (Z)", default=0.0, soft_min=-180.0, soft_max=180.0,
                              description="ワールド Z 軸まわりの回転 (度)", update=_reset_smoothing)
    roll_offset: FloatProperty(name="ロール補正", default=0.0, soft_min=-180.0, soft_max=180.0,
                               description="視線軸まわりの回転 (度)。trkAR の縦位置はアプリ側で補正済み",
                               update=_reset_smoothing)
    smoothing: FloatProperty(name="スムージング", default=0.0, min=0.0, max=0.95,
                             description="0 = 生データ。上げるほど滑らかだが遅れる")
    pose_sync: BoolProperty(name="映像に合わせる", default=True,
                            description="実写 (NDI 受信) を受けているとき、表示中の映像と同じ撮影時刻の姿勢を使う。"
                                        "姿勢の方が先に届くので、そのままだと CG のカメラが実写より先に動く。"
                                        "カメラは映像の遅れの分だけ遅れる")
    pose_sync_offset: FloatProperty(name="ずれ補正", default=0.0, min=-300.0, max=300.0, subtype="NONE",
                                    description="+ でカメラをさらに遅らせる / − で早める (ms)。"
                                                "揃えても CG が先に動くなら +")
    sync_fov: BoolProperty(name="画角を同期", default=True,
                           description="iPhone から届く水平 FOV をカメラに反映する")
    use_rig: BoolProperty(name="iPhone のコントローラー", default=True,
                          description="trkAR のジョイスティックで動かしたオフセット (/client/rig) を足す",
                          update=_reanchor_if_relative)
    use_rig_lens: BoolProperty(name="iPhone のレンズ設定", default=True,
                               description="trkAR の LENS パネルの焦点距離・F 値・フォーカス距離を反映する")
    # --- Blender 側のオフセット (キーフレーム可) ---
    ctl_location: FloatVectorProperty(name="移動", subtype="TRANSLATION", size=3,
                                      description="カメラ位置に足す移動量 (ワールド座標)")
    ctl_pan: FloatProperty(name="パン", default=0.0, soft_min=-180.0, soft_max=180.0,
                           description="カメラ位置を軸にしたワールド Z まわりの回転 (度)")
    ctl_tilt: FloatProperty(name="チルト", default=0.0, soft_min=-90.0, soft_max=90.0,
                            description="カメラの左右軸まわりの回転 (度)。+ で上を向く")
    ctl_roll: FloatProperty(name="ロール", default=0.0, soft_min=-180.0, soft_max=180.0,
                            description="視線軸まわりの回転 (度)")
    ctl_lens: FloatProperty(name="焦点距離", default=0.0, min=0.0, soft_max=300.0,
                            description="35mm 判換算 (mm)。0 = 上書きしない (iPhone のレンズ設定 / 画角の同期に従う)")
    record: BoolProperty(name="再生中にキーフレーム記録", default=False,
                         description="タイムライン再生中、受信した姿勢を毎フレーム記録する")
    last_client_id: StringProperty(name="受信中の Client ID", default="")
    rec_from_remote: BoolProperty(
        name="iPhone の REC で録画", default=True,
        description="ARtrk (iPhone) の REC ボタンで、この REC の開始・停止をする。"
                    "iPhone 側で 3・2・1 のカウントダウンが終わったときに開始の指示が届く")

    # --- 送出 (NDI) ---
    ndi_name: StringProperty(name="NDI 名", default="Blender (ARtrk)")
    ndi_width: IntProperty(name="幅", default=1280, min=16, max=7680)
    ndi_height: IntProperty(name="高さ", default=720, min=16, max=4320)
    ndi_fps: FloatProperty(name="FPS", default=30.0, min=1.0, max=60.0)
    ndi_transparent: BoolProperty(name="背景を透過", default=False,
                                  description="背景を描かずアルファ付きで送る (OBS で実写に重ねる用)")


    # --- 受信 (NDI → シーン) ---
    ndi_in_source: StringProperty(name="ソース", default="",
                                  description="受ける NDI ソース名 (例: IPHONE (ARtrk))")
    ndi_in_lens_crop: BoolProperty(
        name="レンズに合わせて切り出す", default=True,
        description="実写の板・カメラ背景の大きさを iPhone の実際の画角で決める。CTL の LENS や Blender 側の"
                    "焦点距離で望遠にすると、実写も CG と同じだけ中央が切り出される (広角にすると実写の外は映らない)。"
                    "切ると今までどおりカメラの枠いっぱいに貼る")
    ndi_in_front_distance: FloatProperty(
        name="手前の距離", default=1.0, min=0.01, soft_max=20.0, unit="LENGTH",
        description="`カメラの前に置く` の板をカメラからどれだけ前に置くか。これより奥の CG は板 (人) に隠れる")
    ndi_in_low: BoolProperty(name="低解像度で受ける", default=False,
                             description="NDI のプレビュー用ストリームを受ける (軽いが解像度が下がる)")


    # --- 受信 (LiDAR 深度 → 立体スクリーン) ---
    depth_transport: EnumProperty(
        name="受け方",
        items=(("UDP", "UDP (無圧縮)", "trkAR が SystemPC IP の 9100 番へ送る無圧縮の深度。綺麗"),
               ("NDI", "NDI", "NDI の ARtrk-Depth。NDI の圧縮で荒れやすい")),
        default="UDP")
    depth_udp_port: IntProperty(name="UDP ポート", default=depth_input.UDP_DEFAULT_PORT, min=1, max=65535)
    depth_source: StringProperty(name="深度ソース", default="",
                                 description="trkAR の深度 NDI ソース (例: IPHONE (ARtrk-Depth))")
    depth_step: EnumProperty(
        name="間引き",
        items=(("1", "1 (最細)", "LiDAR の全画素 (256×144 頂点)"),
               ("2", "1/2", "縦横 1/2"),
               ("4", "1/4", "縦横 1/4 (軽い)")),
        default="2",
    )
    depth_max_distance: FloatProperty(name="最大距離", default=5.0, min=0.1, max=6.5, unit="LENGTH",
                                      description="これより遠い画素は透明にする (実寸 m)")
    depth_confidence: EnumProperty(
        name="信頼度",
        items=(("0", "すべて", "信頼度 low も使う (ノイズが多い)"),
               ("1", "中以上", "信頼度 medium 以上"),
               ("2", "高のみ", "信頼度 high だけ")),
        default="1",
    )
    depth_people: EnumProperty(
        name="人物",
        items=(("OFF", "使わない", "人物マスクを使わない (深度の段差だけで切る)"),
               ("EDGE", "人の輪郭で切る", "全部表示し、人と背景の境目は ARKit の人物マスクで切る"),
               ("ONLY", "人だけ", "ARKit が人と判定した所だけ表示する")),
        default="EDGE",
        description="ARKit の人物セグメンテーション (UDP で受けているときだけ)")
    depth_person_distance: FloatProperty(
        name="人の距離", default=2.0, min=0.1, max=8.0, unit="LENGTH",
        description="深度の取れない機種 (iPad mini など) で人物マスクだけ来ているとき、"
                    "ARKit の推定深度も無い人の画素を置く距離 (実寸 m)")
    depth_flat_cutout: BoolProperty(
        name="人物マスクで平面カットアウト",
        description="深度メッシュ (立体) を使わず、ARKit の人物マスクを実写 NDI 映像 (ARtrk NDI) の"
                    "アルファにそのまま使う。RVM が重すぎる PC でも、深度の奥行きは無視して"
                    "「NDI 映像をシーンに取り込む」の平面 (スクリーンを置く / カメラの前に置く) で"
                    "人物だけ切り抜ける (UDP 受信中のみ、深度スクリーンは不要)",
        default=False)
    depth_background: BoolProperty(
        name="背景の板",
        description="人の後ろに実写を貼った板を置く。人の居ない所 (深度の無い機種では人以外の全部) はこれが見える。"
                    "CG を人と背景の間に入れられる",
        default=False)
    depth_background_distance: FloatProperty(
        name="背景の距離", default=4.0, min=0.1, soft_max=20.0, unit="LENGTH",
        description="背景の板を置くカメラからの距離 (実寸。トラッキングのスケールが掛かる)")
    take_folder: StringProperty(
        name="保存先", default="", subtype="DIR_PATH",
        description="テイクを置くフォルダ (テイクごとにサブフォルダを作る)。空なら .blend の隣の artrk_takes "
                    "(保存していない .blend では ~/Movies/ARtrk Takes)")
    take_path: StringProperty(name="テイク", subtype="DIR_PATH",
                              description="再生・書き出し・生成するテイクのフォルダ (REC すると最新になる)")
    take_fps: EnumProperty(
        name="収録 FPS",
        items=(("24", "24 fps", "動画生成 (LTX / H3 など) に合わせる。実写 (30fps) は 24fps で拾い直す"),
               ("30", "30 fps", "iPhone の実写と同じ"),
               ("SCENE", "シーンのまま", "出力のフレームレートを変えない")),
        default="24")
    take_render_size: EnumProperty(
        name="解像度",
        items=(("SCENE", "シーンの出力", "シーンの出力の解像度で書き出す"),
               ("LTX", "LTX (896×512)", "長辺 896・短辺 512 (128 の倍数)。縦長のシーンなら 512×896"),
               ("H3", "H3 (960×544)", "MiniMax-H3 の生成解像度 (32 の倍数)。縦長のシーンなら 544×960")),
        default="SCENE")
    take_use_range: BoolProperty(
        name="範囲", default=False,
        description="テイクの一部 (開始〜終了フレーム) だけを書き出す。切ると全体")
    take_range_start: IntProperty(name="開始", default=0, min=0,
                                  description="書き出す最初のフレーム (テイクのフレーム番号)")
    take_range_end: IntProperty(name="終了", default=120, min=0,
                                description="書き出す最後のフレーム (この番号も含む)")
    take_range_mode: EnumProperty(
        name="範囲の決め方",
        items=(("END", "終了フレーム", "開始と終了のフレームで決める"),
               ("DURATION", "尺 (秒)", "開始フレームと長さ (秒) で決める。終了はシーンの fps から自動で決まる")),
        default="END")
    take_range_seconds: FloatProperty(
        name="尺", default=5.0, min=0.05, soft_max=60.0, step=50, precision=2,
        description="開始フレームから書き出す長さ (秒)。フレーム数 = 秒 × fps (端数は四捨五入)")
    take_render_samples: IntProperty(
        name="サンプル数", default=0, min=0, max=256,
        description=("ビューポートで書き出すときの EEVEE のサンプル数。0 ならシーンのまま (ビューポートのサンプル数)。"
                     "下げるほど速い (重いシーンで 4〜8 にすると数倍速くなる)"))
    take_render_mode: EnumProperty(
        name="書き出し",
        items=(("VIEWPORT", "ビューポート", "3D ビューの見た目 (シェーディング) のまま書き出す。速い"),
               ("RENDER", "レンダー", "シーンのレンダーエンジン (EEVEE / Cycles) で書き出す")),
        default="VIEWPORT")
    take_render_format: EnumProperty(
        name="形式",
        items=(("MP4", "MP4 (H.264)", "<テイク>/render.mp4 (控えに render_<.blend の名前>.mp4)。動画生成 (Griptape / ComfyUI など) にそのまま渡せる。アルファなし"),
               ("PNG", "PNG 連番", "<テイク>/render/0001.png … (控えに render_<.blend の名前>/)。アルファ付き (背景を透過にしたとき)")),
        default="MP4")
    bridge_port: IntProperty(name="ポート", default=bridge.DEFAULT_PORT, min=1024, max=65535,
                             description="通信ポートの番号 (127.0.0.1 だけで受ける)")
    depth_edge_shift: FloatProperty(name="エッジ位置", default=0.0, min=-1.0, max=1.0,
                                    description="+ で輪郭を外へ広げる / − で内側へ削る (数画素ぶん)",
                                    update=_update_depth_edge)
    depth_edge_feather: FloatProperty(name="エッジのぼかし", default=0.3, min=0.0, max=1.0,
                                      subtype="FACTOR", description="0 = くっきり、上げるほど輪郭が柔らかく透ける",
                                      update=_update_depth_edge)
    depth_denoise: BoolProperty(name="ノイズ除去", default=True,
                                description="深度に 3×3 のメディアンをかける (境目はまたがない)")
    depth_temporal: FloatProperty(name="時間スムージング", default=0.5, min=0.0, max=0.95,
                                  description="前のフレームと混ぜてちらつきを抑える (大きく動いた所は混ぜない)")
    depth_sync: BoolProperty(name="映像に合わせる", default=True,
                             description="深度を実写 (NDI 受信) と同じ撮影時刻のものに揃えて出す。"
                                         "深度の方が先に届くので、そのままだと輪郭が映像より先に動く")
    depth_sync_offset: FloatProperty(name="ずれ補正", default=0.0, min=-300.0, max=300.0, subtype="NONE",
                                     description="+ で深度をさらに遅らせる / − で早める (ms)。"
                                                 "揃えても輪郭が先に動くなら +")
    depth_edge_tolerance: FloatProperty(name="段差の許容", default=0.1, min=0.0, soft_max=1.0,
                                        subtype="FACTOR",
                                        description="面の 4 隅の距離がこの割合以上違う面は透明にする "
                                                    "(手前と奥の間に膜を張らない)")


class ARTRK_OT_tracking_start(bpy.types.Operator):
    bl_idname = "artrk.tracking_start"
    bl_label = "受信開始"
    bl_description = "trkAR からの OSC を受けてカメラを動かす"

    def execute(self, context):
        settings = context.scene.artrk
        camera = settings.camera or context.scene.camera
        if camera is None:
            self.report({"ERROR"}, "カメラを指定するか、シーンにカメラを置いてください")
            return {"CANCELLED"}
        # オイラーだと分解時に ±180° で跳ねてキーフレーム補間が暴れるため
        camera.rotation_mode = "QUATERNION"
        try:
            tracking.start(settings.port, camera)
        except OSError as exc:
            self.report({"ERROR"}, "ポート %d を開けません: %s" % (settings.port, exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class ARTRK_OT_tracking_stop(bpy.types.Operator):
    bl_idname = "artrk.tracking_stop"
    bl_label = "受信停止"

    def execute(self, _context):
        tracking.stop()
        return {"FINISHED"}


class ARTRK_OT_copy_ip(bpy.types.Operator):
    bl_idname = "artrk.copy_ip"
    bl_label = "IP をコピー"
    bl_description = "この IP をクリップボードへ (iPhone の SystemPC IP に入れる)"

    ip: StringProperty()

    def execute(self, context):
        context.window_manager.clipboard = self.ip
        self.report({"INFO"}, "%s をコピーしました" % self.ip)
        return {"FINISHED"}


class ARTRK_OT_reanchor(bpy.types.Operator):
    bl_idname = "artrk.reanchor"
    bl_label = "ここを基準にし直す"
    bl_description = "今のカメラ位置と iPhone の姿勢を基準に取り直す (iPhone を持ち直したときなど)"

    def execute(self, context):
        settings = context.scene.artrk
        tracking.reanchor(settings.camera or context.scene.camera)
        return {"FINISHED"}


class ARTRK_OT_align_to_cursor(bpy.types.Operator):
    bl_idname = "artrk.align_to_cursor"
    bl_label = "現在位置を 3D カーソルへ"
    bl_description = "今の iPhone の水平位置が 3D カーソルに来るよう位置オフセットを決める (高さは維持)"

    def execute(self, context):
        raw = tracking.last_raw_location()
        if raw is None:
            self.report({"ERROR"}, "まだ姿勢を受信していません")
            return {"CANCELLED"}
        settings = context.scene.artrk
        yaw = Matrix.Rotation(math.radians(settings.yaw_offset), 3, "Z")
        moved = yaw @ (raw * settings.scale)
        cursor = context.scene.cursor.location
        offset = Vector(settings.location_offset)
        offset.x = cursor.x - moved.x
        offset.y = cursor.y - moved.y
        settings.location_offset = offset
        return {"FINISHED"}


class ARTRK_OT_ctl_reset(bpy.types.Operator):
    bl_idname = "artrk.ctl_reset"
    bl_label = "オフセットを戻す"
    bl_description = "Blender 側のオフセットを 0 に戻す"
    bl_options = {"REGISTER", "UNDO"}

    target: EnumProperty(items=(("ROTATION", "向き", ""), ("ALL", "すべて", "")), default="ALL")

    def execute(self, context):
        settings = context.scene.artrk
        settings.ctl_pan = settings.ctl_tilt = settings.ctl_roll = 0.0
        if self.target == "ALL":
            settings.ctl_location = (0.0, 0.0, 0.0)
            settings.ctl_lens = 0.0
        return {"FINISHED"}


class ARTRK_OT_ctl_keyframe(bpy.types.Operator):
    bl_idname = "artrk.ctl_keyframe"
    bl_label = "キーを打つ"
    bl_description = "今のフレームに Blender 側のオフセットのキーフレームを打つ"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        for name in ("ctl_location", "ctl_pan", "ctl_tilt", "ctl_roll", "ctl_lens"):
            scene.keyframe_insert('artrk.%s' % name, frame=scene.frame_current)
        return {"FINISHED"}


class ARTRK_OT_ndi_swap_size(bpy.types.Operator):
    bl_idname = "artrk.ndi_swap_size"
    bl_label = "縦横を入れ替え"
    bl_description = "送出解像度の幅と高さを入れ替える (trkAR の縦位置 9:16 に合わせる)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.artrk
        settings.ndi_width, settings.ndi_height = settings.ndi_height, settings.ndi_width
        return {"FINISHED"}


class ARTRK_OT_ndi_start(bpy.types.Operator):
    bl_idname = "artrk.ndi_start"
    bl_label = "NDI 送出開始"
    bl_description = "このビューのシェーディング設定でカメラ視点を描画し NDI で送り出す"

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == "VIEW_3D"

    def execute(self, context):
        settings = context.scene.artrk
        try:
            ndi_output.start(
                context.area,
                settings.ndi_name,
                settings.ndi_width,
                settings.ndi_height,
                settings.ndi_fps,
                settings.ndi_transparent,
                _library_override(context),
            )
        except Exception as exc:
            ndi_output.stop()
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class ARTRK_OT_ndi_stop(bpy.types.Operator):
    bl_idname = "artrk.ndi_stop"
    bl_label = "NDI 送出停止"

    def execute(self, _context):
        ndi_output.stop()
        return {"FINISHED"}


def _library_override(context):
    addon = context.preferences.addons.get(__package__)
    if addon is None:
        return ""
    return bpy.path.abspath(addon.preferences.ndi_library_path)


def _draw_sources(layout, context, target):
    try:
        names = ndi_in.sources(_library_override(context))
    except Exception as exc:
        layout.label(text=str(exc).splitlines()[0], icon="ERROR")
        return
    if not names:
        layout.label(text="見つかりません (数秒待って開き直す)", icon="INFO")
    for name in names:
        op = layout.operator("artrk.ndi_in_set_source", text=name)
        op.name = name
        op.target = target


class ARTRK_MT_ndi_sources(bpy.types.Menu):
    bl_idname = "ARTRK_MT_ndi_sources"
    bl_label = "NDI ソース"

    def draw(self, context):
        _draw_sources(self.layout, context, "ndi_in_source")


class ARTRK_MT_depth_sources(bpy.types.Menu):
    bl_idname = "ARTRK_MT_depth_sources"
    bl_label = "深度 NDI ソース"

    def draw(self, context):
        _draw_sources(self.layout, context, "depth_source")


class ARTRK_OT_ndi_in_set_source(bpy.types.Operator):
    bl_idname = "artrk.ndi_in_set_source"
    bl_label = "NDI ソースを選ぶ"
    bl_options = {"INTERNAL"}

    name: StringProperty()
    target: StringProperty(default="ndi_in_source")

    def execute(self, context):
        if self.target in {"ndi_in_source", "depth_source"}:
            setattr(context.scene.artrk, self.target, self.name)
        return {"FINISHED"}


class ARTRK_OT_ndi_in_start(bpy.types.Operator):
    bl_idname = "artrk.ndi_in_start"
    bl_label = "NDI 受信開始"
    bl_description = "NDI の映像を画像「%s」に流し込む" % ndi_input.IMAGE_NAME

    def execute(self, context):
        settings = context.scene.artrk
        if not settings.ndi_in_source:
            self.report({"ERROR"}, "ソースを選んでください")
            return {"CANCELLED"}
        if player.active:
            player.stop(context.scene)
        try:
            ndi_in.start(settings.ndi_in_source, settings.ndi_in_low, _library_override(context))
        except Exception as exc:
            ndi_in.stop()
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class ARTRK_OT_ndi_in_stop(bpy.types.Operator):
    bl_idname = "artrk.ndi_in_stop"
    bl_label = "NDI 受信停止"

    def execute(self, _context):
        ndi_in.stop()
        return {"FINISHED"}




def _image_aspect(image):
    width, height = ndi_in.resolution
    if width and height:
        return width / height
    if image.size[0] > 16 and image.size[1] > 16:
        return image.size[0] / image.size[1]
    return 16.0 / 9.0


def _video_material(name, image):
    """受信映像を貼った発光マテリアル。アルファ付きの映像 (RVM の "ARtrk Matte" など) は透ける。
    アルファの無い NDI (RGBX) はアルファ 1 で来るので板全体が出る"""
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()
    tex = nodes.new("ShaderNodeTexImage")
    tex.image = image
    tex.location = (-400, 0)
    emission = nodes.new("ShaderNodeEmission")
    emission.location = (-100, -100)
    transparent = nodes.new("ShaderNodeBsdfTransparent")
    transparent.location = (-100, 50)
    mix = nodes.new("ShaderNodeMixShader")
    mix.location = (100, 0)
    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (300, 0)
    links.new(tex.outputs["Color"], emission.inputs["Color"])
    links.new(tex.outputs["Alpha"], mix.inputs["Fac"])
    links.new(transparent.outputs["BSDF"], mix.inputs[1])
    links.new(emission.outputs["Emission"], mix.inputs[2])
    links.new(mix.outputs["Shader"], output.inputs["Surface"])
    if hasattr(material, "surface_render_method"):
        material.surface_render_method = "DITHERED"
    elif hasattr(material, "blend_method"):
        material.blend_method = "HASHED"
    return material


class ARTRK_OT_ndi_in_screen(bpy.types.Operator):
    bl_idname = "artrk.ndi_in_screen"
    bl_label = "スクリーンを置く"
    bl_description = "受信映像を貼った板 (発光マテリアル) を 3D カーソルの位置に置く。アルファ付きの映像は透ける"
    bl_options = {"REGISTER", "UNDO"}

    height: FloatProperty(name="高さ", default=1.0, min=0.01, unit="LENGTH")

    def execute(self, context):
        image = ndi_input.ensure_image()
        half_h = self.height / 2.0
        half_w = half_h * _image_aspect(image)
        # XZ 平面に立てて -Y 向き (正面図から見える向き)
        mesh = bpy.data.meshes.new("ARtrk NDI Screen")
        mesh.from_pydata(
            [(-half_w, 0.0, -half_h), (half_w, 0.0, -half_h), (half_w, 0.0, half_h), (-half_w, 0.0, half_h)],
            [],
            [(0, 1, 2, 3)],
        )
        uv = mesh.uv_layers.new(name="UVMap")
        for loop, coord in zip(uv.data, ((0, 0), (1, 0), (1, 1), (0, 1))):
            loop.uv = coord

        material = _video_material("ARtrk NDI Screen", image)
        mesh.materials.append(material)

        obj = bpy.data.objects.new("ARtrk NDI Screen", mesh)
        obj.location = context.scene.cursor.location
        if hasattr(obj, "visible_shadow"):
            obj.visible_shadow = False
        context.collection.objects.link(obj)
        for other in context.selected_objects:
            other.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        return {"FINISHED"}


CAMERA_SCREEN_NAME = "ARtrk Camera Screen"


def _source_hfov(scene):
    """iPhone の実際の水平画角 (度)。ライブは届いた姿勢の値、テイクの再生中は録ったときの値。分からなければ 0"""
    if not scene.artrk.ndi_in_lens_crop:
        return 0.0
    if player.active and player.info:
        return float((player.info.get("device") or {}).get("hfov") or 0.0)
    if tracking.server.running:
        return float(tracking._state.get("hfov") or 0.0)
    return 0.0


def _camera_screen_corners(scene, camera, distance, aspect, hfov=0.0):
    """カメラの画面の横幅いっぱいに、映像の縦横比で収まる板の 4 隅 (カメラ座標)。
    画角は水平基準で iPhone と合わせているので、横幅を画面に合わせれば実写と重なる。
    hfov (iPhone の水平画角、度) があれば横幅はそれで決める。レンズを望遠にすると板が枠より大きくなり、
    CG と同じだけ実写の中央が切り出される"""
    frame = [Vector(v) for v in camera.data.view_frame(scene=scene)]
    scale = distance / -frame[0].z
    xs = [v.x * scale for v in frame]
    ys = [v.y * scale for v in frame]
    cx, cy = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
    half_w = (max(xs) - min(xs)) / 2.0
    if hfov > 0.0:
        half_w = distance * math.tan(math.radians(hfov) / 2.0)
    half_h = half_w / aspect
    return [(cx - half_w, cy - half_h, -distance), (cx + half_w, cy - half_h, -distance),
            (cx + half_w, cy + half_h, -distance), (cx - half_w, cy + half_h, -distance)]


def _update_camera_background(scene):
    """カメラ背景の実写の拡大率を、iPhone の画角と今のレンズの比に合わせる (望遠ほど中央を拡大)。"""
    camera = scene.artrk.camera or scene.camera
    if camera is None or camera.type != "CAMERA" or camera.data.type != "PERSP":
        return
    image = bpy.data.images.get(ndi_input.IMAGE_NAME)
    background = next((bg for bg in camera.data.background_images if image is not None and bg.image == image), None)
    if background is None:
        return
    hfov = _source_hfov(scene)
    scale = 1.0
    if hfov > 0.0:
        frame = [Vector(v) for v in camera.data.view_frame(scene=scene)]
        depth = -frame[0].z
        half_w = (max(v.x for v in frame) - min(v.x for v in frame)) / 2.0 / depth
        half_h = (max(v.y for v in frame) - min(v.y for v in frame)) / 2.0 / depth
        aspect = _image_aspect(image)
        # FIT で枠に収めたときの実写の半幅 (枠より横長の映像なら横幅いっぱい、縦長なら高さいっぱい)
        fitted = half_w if aspect >= half_w / half_h else half_h * aspect
        scale = math.tan(math.radians(hfov) / 2.0) / fitted
    if abs(background.scale - scale) > 1e-4:
        background.scale = scale


def _update_camera_screen():
    """画角 (iPhone の FOV / レンズ)・解像度・映像の縦横比が変わったら板の大きさを合わせ直す。"""
    scene = bpy.context.scene
    if scene is not None:
        _update_camera_background(scene)
    obj = bpy.data.objects.get(CAMERA_SCREEN_NAME)
    if obj is None or scene is None or obj.type != "MESH" or obj.mode == "EDIT":
        return 1.0 / 30.0
    camera = obj.parent
    if camera is None or camera.type != "CAMERA" or len(obj.data.vertices) != 4:
        return 1.0 / 30.0
    data = camera.data
    render = scene.render
    distance = max(scene.artrk.ndi_in_front_distance, data.clip_start * 1.01)
    aspect = _image_aspect(ndi_input.ensure_image())
    hfov = _source_hfov(scene) if data.type == "PERSP" else 0.0
    key = (data.lens, data.sensor_width, data.sensor_height, data.sensor_fit, data.shift_x, data.shift_y,
           render.resolution_x, render.resolution_y, render.pixel_aspect_x, render.pixel_aspect_y,
           round(aspect, 4), round(distance, 5), data.type, data.ortho_scale, round(hfov, 2))
    if obj.get("artrk_key") != str(key):
        corners = _camera_screen_corners(scene, camera, distance, aspect, hfov)
        obj.data.vertices.foreach_set("co", [c for corner in corners for c in corner])
        obj.data.update()
        obj["artrk_key"] = str(key)
    return 1.0 / 30.0


class ARTRK_OT_ndi_in_front(bpy.types.Operator):
    bl_idname = "artrk.ndi_in_front"
    bl_label = "カメラの前に置く"
    bl_description = ("受信映像を貼った板を追従カメラの子にして、画面いっぱいに `手前の距離` の所へ置く。"
                      "アルファ付きの映像 (RVM) なら人だけが CG より手前に出る")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.artrk
        camera = settings.camera or context.scene.camera
        if camera is None or camera.type != "CAMERA":
            self.report({"ERROR"}, "カメラがありません")
            return {"CANCELLED"}
        image = ndi_input.ensure_image()
        obj = bpy.data.objects.get(CAMERA_SCREEN_NAME)
        if obj is None or obj.type != "MESH":
            mesh = bpy.data.meshes.new(CAMERA_SCREEN_NAME)
            mesh.from_pydata([(0, 0, 0)] * 4, [], [(0, 1, 2, 3)])
            uv = mesh.uv_layers.new(name="UVMap")
            for loop, coord in zip(uv.data, ((0, 0), (1, 0), (1, 1), (0, 1))):
                loop.uv = coord
            mesh.materials.append(_video_material(CAMERA_SCREEN_NAME, image))
            obj = bpy.data.objects.new(CAMERA_SCREEN_NAME, mesh)
            if hasattr(obj, "visible_shadow"):
                obj.visible_shadow = False
            context.collection.objects.link(obj)
        obj.parent = camera
        obj.matrix_parent_inverse.identity()
        obj.matrix_basis.identity()
        obj.hide_viewport = False
        obj.hide_render = False
        obj.hide_set(False)
        if "artrk_key" in obj:
            del obj["artrk_key"]
        _update_camera_screen()
        _use_material_view(self, context)
        return {"FINISHED"}


class ARTRK_OT_ndi_in_camera_bg(bpy.types.Operator):
    bl_idname = "artrk.ndi_in_camera_bg"
    bl_label = "カメラ背景に表示"
    bl_description = "受信映像を追従カメラの背景画像に設定する (3D ビューのカメラ視点でだけ見える)"

    def execute(self, context):
        settings = context.scene.artrk
        camera = settings.camera or context.scene.camera
        if camera is None or camera.type != "CAMERA":
            self.report({"ERROR"}, "カメラがありません")
            return {"CANCELLED"}
        image = ndi_input.ensure_image()
        cam = camera.data
        background = next((bg for bg in cam.background_images if bg.image == image), None)
        if background is None:
            background = cam.background_images.new()
            background.source = "IMAGE"
            background.image = image
        background.alpha = 1.0
        background.display_depth = "BACK"
        background.frame_method = "FIT"
        cam.show_background_images = True
        return {"FINISHED"}


def _needs_material_view(context):
    space = context.space_data
    return space is not None and space.type == "VIEW_3D" and space.shading.type in {"SOLID", "WIREFRAME"}


def _use_material_view(operator, context):
    """ソリッド表示だと実写の色も透明な縁も出ないので、このビューをマテリアルプレビューにする。"""
    if _needs_material_view(context):
        context.space_data.shading.type = "MATERIAL"
        operator.report({"INFO"}, "実写の色を出すため、このビューをマテリアルプレビューにしました")


class ARTRK_OT_view_material(bpy.types.Operator):
    bl_idname = "artrk.view_material"
    bl_label = "マテリアルプレビューにする"
    bl_description = "このビューをマテリアルプレビューにする (ソリッド表示ではスクリーンに実写の色が付かない)"

    def execute(self, context):
        _use_material_view(self, context)
        return {"FINISHED"}


class ARTRK_OT_depth_start(bpy.types.Operator):
    bl_idname = "artrk.depth_start"
    bl_label = "深度受信開始"
    bl_description = "trkAR の LiDAR 深度を受けて「%s」の形を毎フレーム更新する" % depth_input.OBJECT_NAME

    def execute(self, context):
        settings = context.scene.artrk
        if settings.depth_transport == "NDI" and not settings.depth_source:
            self.report({"ERROR"}, "深度ソースを選んでください")
            return {"CANCELLED"}
        if player.active:
            player.stop(context.scene)
        try:
            if settings.depth_transport == "UDP":
                depth_in.start_udp(settings.depth_udp_port)
            else:
                depth_in.start(settings.depth_source, _library_override(context))
        except Exception as exc:
            depth_in.stop()
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        if bpy.data.objects.get(depth_input.OBJECT_NAME) is None:
            bpy.ops.artrk.depth_screen()
        _use_material_view(self, context)
        return {"FINISHED"}


class ARTRK_OT_depth_stop(bpy.types.Operator):
    bl_idname = "artrk.depth_stop"
    bl_label = "深度受信停止"

    def execute(self, context):
        depth_in.stop()
        depth_in.idle_update(context.scene)
        return {"FINISHED"}


class ARTRK_OT_depth_screen(bpy.types.Operator):
    bl_idname = "artrk.depth_screen"
    bl_label = "深度スクリーンを作る"
    bl_description = "追従カメラの子として、深度で立体になるスクリーン (実写テクスチャ付き) を作る"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.artrk
        camera = settings.camera or context.scene.camera
        if camera is None:
            self.report({"ERROR"}, "カメラがありません")
            return {"CANCELLED"}
        obj = bpy.data.objects.get(depth_input.OBJECT_NAME)
        if obj is None:
            mesh = bpy.data.meshes.new(depth_input.OBJECT_NAME)
            mesh.materials.append(depth_input.make_material())
            obj = bpy.data.objects.new(depth_input.OBJECT_NAME, mesh)
            if hasattr(obj, "visible_shadow"):
                obj.visible_shadow = False
            context.collection.objects.link(obj)
        depth_input.ensure_material(obj)
        depth_input.update_edge(settings)
        # カメラ座標のまま頂点を置くので、カメラに重ねて子にする
        obj.parent = camera
        obj.matrix_parent_inverse.identity()
        obj.location = (0.0, 0.0, 0.0)
        obj.rotation_mode = "QUATERNION"
        obj.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        obj.scale = (1.0, 1.0, 1.0)
        _use_material_view(self, context)
        return {"FINISHED"}


class ARTRK_OT_depth_freeze(bpy.types.Operator):
    bl_idname = "artrk.depth_freeze"
    bl_label = "今の形を固定"
    bl_description = "今の深度スクリーンを、その場所・その映像のまま別オブジェクトとして残す"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        source = bpy.data.objects.get(depth_input.OBJECT_NAME)
        if source is None or len(source.data.vertices) == 0:
            self.report({"ERROR"}, "深度スクリーンがまだありません")
            return {"CANCELLED"}
        world = source.matrix_world.copy()
        obj = source.copy()
        obj.data = source.data.copy()
        obj.name = obj.data.name = "ARtrk Depth Snapshot"
        obj.parent = None
        obj.matrix_world = world
        # 映像も今のフレームで止める
        material = source.data.materials[0].copy() if source.data.materials else None
        if material is not None:
            image = ndi_input.ensure_image().copy()
            image.name = "ARtrk Depth Snapshot"
            image.pack()
            mask_node = material.node_tree.nodes.get(depth_input.MASK_NODE)
            if mask_node is not None and mask_node.image is not None:
                mask = mask_node.image.copy()
                mask.name = "ARtrk Depth Snapshot Mask"
                mask.pack()
                mask_node.image = mask
            for node in material.node_tree.nodes:
                if node.type == "TEX_IMAGE" and node != mask_node:
                    node.image = image
            obj.data.materials[0] = material
        context.collection.objects.link(obj)
        return {"FINISHED"}


# --- 録画 → 書き出し ---

# テイク再生を始める前にライブで受けていたもの (ライブに戻るときに受け直す)
_live_before_play = {"ndi": False, "depth": False}
def _take_path(context):
    path = bpy.path.abspath(context.scene.artrk.take_path).rstrip("/\\")
    return path if path and os.path.isfile(os.path.join(path, "take.json")) else None


class ARTRK_OT_take_rec_start(bpy.types.Operator):
    bl_idname = "artrk.take_rec_start"
    bl_label = "REC"
    bl_description = ("タイムラインを再生しながら、カメラの動き (キーフレーム)・実写・深度スクリーンを録る。"
                      "もう一度押すか再生を止めると終わる")

    def execute(self, context):
        if player.active:
            _play_to_live(context)
        folder = take.takes_folder(context.scene.artrk.take_folder)
        render = context.scene.render
        if context.scene.artrk.take_fps != "SCENE":
            render.fps = int(context.scene.artrk.take_fps)
            render.fps_base = 1.0
        prefs = _prefs(context)
        slate_set = bool(prefs.slate_scene or prefs.slate_cut)
        if slate_set and prefs.slate_take == 0:  # スレートを設定した最初のテイクは T01
            prefs.slate_take = 1
        try:
            path = recorder.start(context, folder, _shot_info(context, prefs))
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        context.scene.artrk.take_path = path
        # 次のテイクの番号へ (アドオン設定なので、自動保存なら Blender を閉じるときに保存される)。
        # スレートを設定していなければ S00C00T00 のまま
        if slate_set:
            prefs.slate_take += 1
        context.preferences.is_dirty = True
        return {"FINISHED"}


def _shot_info(context, prefs):
    """録画を始めるときの来歴 (take.json の recorded_at / slate / location / environment / device / people)。
    device は今追従している iPhone (受信していなければ機種などは null)。"""
    pose = tracking.current_pose()
    client = tracking.server.info_for(pose.client_id) if pose is not None else None
    slate = (prefs.slate_production, prefs.slate_scene, prefs.slate_cut, prefs.slate_take)
    people = {"creator": prefs.shot_creator, "operator": prefs.shot_operator}
    info = take.shot_info(context.scene, slate, prefs.shot_site, prefs.shot_host, ADDON_VERSION, people,
                          pose, client)
    info["camera_offsets"] = _camera_offsets(context)
    return info


def _camera_offsets(context):
    """録り始めにカメラに乗っていたずれ (take.json の camera_offsets)。どれもカメラの動きには焼き込み済みで、
    再生で足し直すことはない (あとから「どのずれで録ったか」を確かめるための記録)"""
    settings = context.scene.artrk
    camera = settings.camera or context.scene.camera
    rig = tracking._state.get("rig") if settings.use_rig else None
    return {
        "mode": settings.mode,
        "scale": settings.scale,
        "blender": {"location": [round(v, 4) for v in settings.ctl_location], "pan_deg": settings.ctl_pan,
                    "tilt_deg": settings.ctl_tilt, "roll_deg": settings.ctl_roll, "lens_mm": settings.ctl_lens},
        "iphone_rig": None if rig is None else {
            "position": [round(v, 4) for v in rig.position], "yaw_deg": rig.yaw_deg, "tilt_deg": rig.tilt_deg,
            "roll_deg": rig.roll_deg, "focal_mm": rig.focal_mm, "fstop": rig.fstop,
            "focus_distance": rig.focus_distance},
        "camera_parent": camera.parent.name if camera is not None and camera.parent is not None else None,
    }


def _slate_label(prefs):
    return take.slate_label(prefs.slate_scene, prefs.slate_cut, prefs.slate_take)


def _draw_slate_settings(layout, prefs):
    layout.prop(prefs, "slate_production")
    row = layout.row(align=True)
    row.prop(prefs, "slate_scene")
    row.prop(prefs, "slate_cut")
    row.prop(prefs, "slate_take")
    row.operator("artrk.slate_unset", text="", icon="X")
    col = layout.column(align=True)
    col.prop(prefs, "shot_site")
    col.prop(prefs, "shot_host")
    col = layout.column(align=True)
    col.prop(prefs, "shot_creator")
    col.prop(prefs, "shot_operator")


class ARTRK_OT_slate_unset(bpy.types.Operator):
    bl_idname = "artrk.slate_unset"
    bl_label = "スレートを未設定に戻す"
    bl_description = "Scene / Cut / Take を 0 にする (来歴では S00C00T00 = スレートを設定していないカット)"

    def execute(self, context):
        prefs = _prefs(context)
        prefs.slate_scene = prefs.slate_cut = 0
        prefs.slate_take = 0
        context.preferences.is_dirty = True
        return {"FINISHED"}


class ARTRK_OT_take_rec_stop(bpy.types.Operator):
    bl_idname = "artrk.take_rec_stop"
    bl_label = "録画停止"

    def execute(self, context):
        path = recorder.stop(context)
        if path:
            self.report({"INFO"}, "%s: %d フレーム (間引き %d)" % (os.path.basename(path), recorder.frames,
                                                               recorder.dropped))
            _run_hook(path, "recorded")
        return {"FINISHED"}




def _on_remote_rec(start):
    """iPhone の REC ボタン。パネルの REC / 録画停止と同じ処理を呼ぶ。
    タイマーから呼ばれるので、再生の開始・停止が通るようにウィンドウを指定する。"""
    windows = bpy.context.window_manager.windows
    if not windows:
        return
    window = windows[0]
    with bpy.context.temp_override(window=window, screen=window.screen):
        if start:
            if recorder.recording or _export["running"]:
                return
            bpy.ops.artrk.take_rec_start()
        elif recorder.recording:
            bpy.ops.artrk.take_rec_stop()


def _play_to_live(context):
    player.stop(context.scene)
    if _live_before_play["ndi"] and not ndi_in.running and context.scene.artrk.ndi_in_source:
        bpy.ops.artrk.ndi_in_start()
    if _live_before_play["depth"] and not depth_in.running:
        bpy.ops.artrk.depth_start()
    _live_before_play["ndi"] = _live_before_play["depth"] = False


def _start_player(operator, context):
    path = _take_path(context)
    if path is None:
        operator.report({"ERROR"}, "テイクのフォルダ (take.json のある所) を選んでください")
        return False
    if not player.active:
        _live_before_play["ndi"] = ndi_in.running
        _live_before_play["depth"] = depth_in.running
    # 受信したフレームが録画の再生を上書きしないように止める
    ndi_in.stop()
    depth_in.stop()
    try:
        player.start(context.scene, path)
    except Exception as exc:
        player.stop(context.scene)
        operator.report({"ERROR"}, str(exc))
        return False
    return True


class ARTRK_OT_take_play(bpy.types.Operator):
    bl_idname = "artrk.take_play"
    bl_label = "テイクを再生"
    bl_description = "受信を止め、テイクのカメラの動き・実写・深度をタイムラインで再生できるようにする"

    def execute(self, context):
        if not _start_player(self, context):
            return {"CANCELLED"}
        _use_material_view(self, context)
        return {"FINISHED"}


class ARTRK_OT_take_live(bpy.types.Operator):
    bl_idname = "artrk.take_live"
    bl_label = "ライブに戻る"
    bl_description = "テイクの再生をやめ、止める前に受けていた NDI / 深度を受け直す"

    def execute(self, context):
        _play_to_live(context)
        return {"FINISHED"}


def _anchor_restart(operator, context):
    """背景のオフセット (Empty) を作った・消したのを再生中のカメラに反映する (再生し直す)"""
    if player.active:
        frame = context.scene.frame_current
        _start_player(operator, context)
        context.scene.frame_set(frame)


class ARTRK_OT_take_anchor_add(bpy.types.Operator):
    bl_idname = "artrk.take_anchor_add"
    bl_label = "オフセットを付ける"
    bl_description = ("背景に対してテイク (人とカメラの動き) をずらすための Empty を、テイクの人の居る所 "
                      "(始めのフレームでカメラの「手前の距離」前、床の高さ) に置き、再生中のカメラをその子にする。"
                      "Empty を動かす・Z で回すと、カメラの動きはそのままで背景の中の場所がずれる。"
                      "Empty の Y の矢印が始めのフレームでカメラの見る向き。"
                      "置いた場所はテイクごとに take.json に残り、書き出しにも使う")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if not player.active and not _start_player(self, context):
            return {"CANCELLED"}
        anchor = take.ensure_anchor(context.scene)
        anchor.matrix_world = player.pivot
        _anchor_restart(self, context)
        _use_material_view(self, context)
        return {"FINISHED"}


class ARTRK_OT_take_anchor_cursor(bpy.types.Operator):
    bl_idname = "artrk.take_anchor_cursor"
    bl_label = "3D カーソルへ"
    bl_description = ("オフセットの基準 (テイクの人の居る所) を 3D カーソルの位置へ動かす。"
                      "Shift + 右クリックで床をクリックしてから押す")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        anchor = bpy.data.objects.get(take.ANCHOR_NAME)
        if anchor is None:
            return {"CANCELLED"}
        anchor.location = context.scene.cursor.location
        return {"FINISHED"}


class ARTRK_OT_take_anchor_previous(bpy.types.Operator):
    bl_idname = "artrk.take_anchor_previous"
    bl_label = "前の置き場を使う"
    bl_description = ("このテイクを再生する前に .blend にあった置き場の位置に戻し、このテイクに残す。"
                      "置き場がテイクごとになる前 (0.9.11 まで) に合わせた置き場を引き継ぐときに使う")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, _context):
        return player.active and player.previous_anchor is not None

    def execute(self, context):
        anchor = take.ensure_anchor(context.scene)
        anchor.matrix_world = player.previous_anchor
        _anchor_restart(self, context)
        player.previous_anchor = None
        return {"FINISHED"}


class ARTRK_OT_take_anchor_remove(bpy.types.Operator):
    bl_idname = "artrk.take_anchor_remove"
    bl_label = "オフセットを外す"
    bl_description = "背景のオフセットを外して、テイクを録ったときの位置で再生する"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        anchor = bpy.data.objects.get(take.ANCHOR_NAME)
        if anchor is not None:
            bpy.data.objects.remove(anchor)
            _anchor_restart(self, context)
        return {"FINISHED"}


def _clear_frames(folder):
    """前回の書き出し・生成の画像を消す (枚数が減ったときに古いフレームが混ざらないように)。"""
    for path in take.list_frames(folder):
        os.remove(path)


def _view3d_window(context):
    areas = [context.area] if context.area is not None and context.area.type == "VIEW_3D" else []
    areas += [a for a in context.screen.areas if a.type == "VIEW_3D"]
    for area in areas:
        for region in area.regions:
            if region.type == "WINDOW":
                return area, region
    return None, None


# 書き出しで一時的に変える出力設定 (終わったら戻す)
_OUTPUT_PROPS = (
    ("image_settings", ("media_type", "file_format", "color_mode")),
    ("ffmpeg", ("format", "codec", "constant_rate_factor", "ffmpeg_preset", "audio_codec")),
)


def _save_output(render):
    saved = {"filepath": render.filepath,
             "resolution": (render.resolution_x, render.resolution_y, render.resolution_percentage)}
    for group, names in _OUTPUT_PROPS:
        owner = getattr(render, group)
        saved[group] = [(n, getattr(owner, n)) for n in names if hasattr(owner, n)]
    return saved


def _restore_output(render, saved):
    for group, _names in _OUTPUT_PROPS:
        owner = getattr(render, group)
        for name, value in saved[group]:
            try:
                setattr(owner, name, value)
            except (TypeError, ValueError):
                pass
    render.filepath = saved["filepath"]
    render.resolution_x, render.resolution_y, render.resolution_percentage = saved["resolution"]


def _set_output(render, path, video, size="SCENE"):
    if size in ("LTX", "H3"):
        # 向きはシーンの出力 (カメラ) に合わせる
        long_side, short_side = (896, 512) if size == "LTX" else (960, 544)
        portrait = render.resolution_y > render.resolution_x
        render.resolution_x, render.resolution_y = (short_side, long_side) if portrait else (long_side, short_side)
        render.resolution_percentage = 100
    image = render.image_settings
    if video:
        if hasattr(image, "media_type"):
            image.media_type = "VIDEO"
        image.file_format = "FFMPEG"
        render.ffmpeg.format = "MPEG4"
        render.ffmpeg.codec = "H264"
        render.ffmpeg.constant_rate_factor = "HIGH"
        render.ffmpeg.audio_codec = "NONE"
        render.filepath = path
    else:
        if hasattr(image, "media_type"):
            image.media_type = "IMAGE"
        image.file_format = "PNG"
        image.color_mode = "RGBA"
        render.filepath = path + os.sep


class ARTRK_OT_take_render(bpy.types.Operator):
    bl_idname = "artrk.take_render"
    bl_label = "書き出し"
    bl_description = ("テイクをカメラ視点で <テイク>/render.mp4 (または render/ の連番 PNG) に書き出す。"
                      "render.mp4 は次の書き出しで上書きされるので、render_<.blend の名前>.mp4 にも控えを残す。"
                      "解像度はシーンの出力の設定")

    def execute(self, context):
        if not _start_player(self, context):
            return {"CANCELLED"}
        scene = context.scene
        settings = scene.artrk
        camera = settings.camera or scene.camera
        video = settings.take_render_format == "MP4"
        take_path = player.path
        render = scene.render
        saved = _save_output(render)
        saved_camera = scene.camera
        if video:
            out = take.render_video(player.path)
            if os.path.exists(out):
                os.remove(out)
        else:
            out = take.render_folder(player.path)
            os.makedirs(out, exist_ok=True)
            _clear_frames(out)
        _set_output(render, out, video, settings.take_render_size)
        scene.camera = camera
        take_range = (scene.frame_start, scene.frame_end)
        if settings.take_use_range:
            scene.frame_start, scene.frame_end = _take_range(settings, render, *take_range)

        def restore(*_args):
            _restore_output(render, saved)
            scene.camera = saved_camera
            scene.frame_start, scene.frame_end = take_range
            for handlers, handler in ((bpy.app.handlers.render_complete, rendered),
                                      (bpy.app.handlers.render_cancel, restore)):
                if handler in handlers:
                    handlers.remove(handler)

        def rendered(*_args):
            # 中止 (render_cancel) では記録も控えも残さない
            _note_render(take_path, scene, video)
            restore()

        if settings.take_render_mode == "RENDER":
            bpy.app.handlers.render_complete.append(rendered)
            bpy.app.handlers.render_cancel.append(restore)
            bpy.ops.render.render("INVOKE_DEFAULT", animation=True)
            return {"FINISHED"}

        if _export["running"]:
            restore()
            self.report({"ERROR"}, "前の書き出しがまだ動いています")
            return {"CANCELLED"}
        area, region = _view3d_window(context)
        if area is None:
            restore()
            self.report({"ERROR"}, "3D ビューがありません")
            return {"CANCELLED"}
        view = area.spaces.active.region_3d
        perspective = view.view_perspective
        view.view_perspective = "CAMERA"
        eevee = getattr(scene, "eevee", None)
        samples = eevee.taa_samples if eevee is not None else 0
        if eevee is not None and settings.take_render_samples > 0:
            eevee.taa_samples = settings.take_render_samples

        def finish(completed):
            try:
                view.view_perspective = perspective
            except ReferenceError:
                pass
            if eevee is not None:
                eevee.taa_samples = samples
            if completed:
                _export["kept"] = _note_render(take_path, scene, video)
            restore()

        # 書き出し中も UI を止めない (INVOKE で動かし、終わりはタイマーで見る。Esc で中止できる)
        with context.temp_override(window=context.window, area=area, region=region):
            result = bpy.ops.render.opengl("INVOKE_DEFAULT", animation=True)
        if "RUNNING_MODAL" not in result:
            finish(False)
            self.report({"ERROR"}, "書き出しを始められませんでした")
            return {"CANCELLED"}
        _export.update(running=True, started=False, finish=finish, out=out, video=video, kept="",
                       start=scene.frame_start, end=scene.frame_end, frame=scene.frame_start - 1,
                       t0=time.monotonic(), message="")
        bpy.app.handlers.frame_change_post.append(_export_frame)
        bpy.app.timers.register(_watch_export, first_interval=0.2)
        return {"FINISHED"}


# ビューポートの書き出しの進み具合 (パネルに出す)
_export = {"running": False, "message": ""}


def _export_frame(scene, _depsgraph=None):
    if _export["running"]:
        _export["frame"] = max(_export["frame"], scene.frame_current)


def export_progress():
    """(書き出したフレーム数, 全体, 残り秒) か None。"""
    if not _export["running"]:
        return None
    total = _export["end"] - _export["start"] + 1
    done = max(0, _export["frame"] - _export["start"])
    elapsed = time.monotonic() - _export["t0"]
    remaining = elapsed / done * (total - done) if done else None
    return done, total, remaining


def _watch_export():
    if bpy.app.is_job_running("RENDER"):
        _export["started"] = True
        return 0.25
    if not _export["started"] and time.monotonic() - _export["t0"] < 3.0:
        return 0.25
    if _export_frame in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(_export_frame)
    _export["running"] = False
    completed = _export["frame"] >= _export["end"]
    try:
        _export["finish"](completed)
    except Exception as exc:
        _export["message"] = "書き出しの後始末に失敗: %s" % exc
        return None
    seconds = time.monotonic() - _export["t0"]
    out = _export["out"]
    if not completed:
        _export["message"] = "書き出しを中止しました (%d フレームまで)" % _export["frame"]
    elif _export["video"]:
        size = os.path.getsize(out) / 1e6 if os.path.exists(out) else 0
        _export["message"] = "render.mp4 %.1f MB (%.0f 秒)" % (size, seconds)
    else:
        _export["message"] = "render/ に %d 枚 (%.0f 秒)" % (len(take.list_frames(out)), seconds)
    if completed and _export["kept"]:
        _export["message"] += "  控え %s" % os.path.basename(_export["kept"])
    return None


def _note_render(path, scene, video):
    """書き出しの解像度などを take.json に足し (クラウド側の処理が読む)、render_<.blend の名前> に控えを残す。
    控えのパスを返す (残せなければ空)。"""
    kept = ""
    try:
        kept = take.keep_render(path, video)
    except OSError:
        pass
    try:
        info = take.load_info(path)
        render = scene.render
        info["render"] = {
            "format": "MP4" if video else "PNG",
            "width": render.resolution_x * render.resolution_percentage // 100,
            "height": render.resolution_y * render.resolution_percentage // 100,
            "fps": render.fps / render.fps_base,
            "frame_start": scene.frame_start,
            "frame_end": scene.frame_end,
            "blend": take.blend_label(),
            "kept": os.path.basename(kept),
        }
        with open(os.path.join(path, "take.json"), "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
    except (OSError, ValueError, KeyError):
        pass
    _run_hook(path, "exported")
    return kept


# ---- テイクのあとに流すコマンド (hooks.py) ----

def _run_hook(path, event):
    """設定のコマンドを裏で流す (空なら何もしない)。失敗しても録画・書き出しの後始末は止めない"""
    try:
        prefs = _prefs(bpy.context)
        wanted = prefs.hook_on_record if event == "recorded" else prefs.hook_on_export
        if wanted:
            hooks.run(path, event, prefs.hook_command, ADDON_VERSION or "", prefs.hook_timeout)
    except Exception as exc:
        print("trkAR: テイクのあとのコマンドを始められません: %s" % exc)


def _draw_hook_settings(layout, prefs):
    layout.prop(prefs, "hook_command")
    row = layout.row(align=True)
    row.prop(prefs, "hook_on_record")
    row.prop(prefs, "hook_on_export")
    layout.prop(prefs, "hook_timeout")


def _draw_hook(layout, context, path):
    """テイクのコマンドの状態と、手動の「もう一度流す」"""
    prefs = _prefs(context)
    st = hooks.status(path)
    if st is None and not prefs.hook_command.strip():
        return
    if st is not None:
        text, icon = hooks.label(st)
        row = layout.row()
        row.alert = st["state"] == "failed"
        row.label(text=text, icon=icon)
        if st.get("url"):
            layout.operator("wm.url_open", text="開く", icon="URL").url = st["url"]
    row = layout.row(align=True)
    row.enabled = bool(prefs.hook_command.strip()) and not hooks.busy(path)
    row.operator("artrk.take_hook_run", icon="FILE_REFRESH")
    if st is not None:
        row.operator("artrk.take_open", icon="TEXT", text="ログ").sub = hooks.LOG_FILE


class ARTRK_OT_take_hook_run(bpy.types.Operator):
    bl_idname = "artrk.take_hook_run"
    bl_label = "コマンドを流す"
    bl_description = ("アドオン設定の「テイクのあとに流すコマンド」を、このテイクでもう一度流す "
                      "(書き出してあれば event = exported、なければ recorded)")

    @classmethod
    def poll(cls, context):
        path = _take_path(context)
        return path is not None and not hooks.busy(path)

    def execute(self, context):
        path = _take_path(context)
        prefs = _prefs(context)
        event = "exported" if hooks._render_path(path) else "recorded"
        if not hooks.run(path, event, prefs.hook_command, ADDON_VERSION or "", prefs.hook_timeout):
            self.report({"ERROR"}, "アドオン設定の「テイクのあとに流すコマンド」が空か、読めません")
            return {"CANCELLED"}
        return {"FINISHED"}




# ---- 通信ポート (自動化、bridge) の命令。どれもメインスレッドで呼ばれる ----

class _Reports:
    """オペレーターの report を受ける代わり (通信ポートの返事にエラーを載せる)"""

    def __init__(self):
        self.errors = []

    def report(self, kinds, message):
        if "ERROR" in kinds:
            self.errors.append(message)


def _call(context, operator, **kwargs):
    result = operator(**kwargs)
    if "FINISHED" not in result and "RUNNING_MODAL" not in result:
        raise RuntimeError("%s が動きませんでした" % operator.idname_py())
    return result


def _anchor_state():
    anchor = bpy.data.objects.get(take.ANCHOR_NAME)
    if anchor is None:
        return None
    return {"location": list(anchor.location), "heading_deg": math.degrees(anchor.rotation_euler.z)}


def _bridge_state(context):
    scene = context.scene
    settings = scene.artrk
    path = _take_path(context)
    info = take.load_info(path) if path else {}
    video = path and os.path.isfile(take.render_video(path))
    return {
        "blend": bpy.data.filepath,
        "scene": scene.name,
        "take": os.path.basename(path) if path else "",
        "take_path": path or "",
        "playing": player.active,
        "recording": recorder.recording,
        "anchor": _anchor_state(),
        "settings": {key: getattr(settings, key) for key in _BRIDGE_SETTINGS},
        "export": {
            "running": _export["running"],
            "frame": _export.get("frame", 0), "start": _export.get("start", 0), "end": _export.get("end", 0),
            "message": _export["message"],
            "render": take.render_video(path) if video else "",
            "kept": _export.get("kept", ""),
            "info": info.get("render"),
        },
        "hook": hooks.status(path) if path else None,
    }


def _bridge_open_scene(context, path):
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        raise RuntimeError("ありません: %s" % path)
    if _export["running"] or recorder.recording:
        raise RuntimeError("録画か書き出しの最中です")
    _call(context, bpy.ops.wm.open_mainfile, filepath=path)
    # 開いた直後は窓や画面が入れ替わっているので、中身は次の state で見る
    return {"blend": path}


def _bridge_list_takes(context):
    folder = take.takes_folder(bpy.path.abspath(context.scene.artrk.take_folder))
    takes = []
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if not os.path.isfile(os.path.join(path, "take.json")):
                continue
            try:
                info = take.load_info(path)
            except (OSError, ValueError):
                continue
            takes.append({
                "name": name, "path": path, "frames": info.get("frames"), "fps": info.get("fps"),
                "playable": os.path.isfile(os.path.join(path, "actions.json")),
                "render": os.path.isfile(take.render_video(path)),
                "kept": sorted(n for n in os.listdir(path) if n.startswith("render_")),
            })
    return {"folder": folder, "takes": takes}


def _bridge_select_take(context, name=""):
    """テイクを選んで再生できるようにする。name が空なら最新"""
    if recorder.recording or _export["running"]:
        raise RuntimeError("録画か書き出しの最中です")
    listing = _bridge_list_takes(context)["takes"]
    found = [t for t in listing if t["name"] == name] if name else listing[-1:]
    if not found:
        raise RuntimeError("テイクがありません: %s" % (name or "(最新)"))
    context.scene.artrk.take_path = found[0]["path"]
    reports = _Reports()
    if not _start_player(reports, context):
        raise RuntimeError("; ".join(reports.errors) or "再生を始められません")
    _use_material_view(reports, context)
    return _bridge_state(context)


def _bridge_place_anchor(context, location=None, heading_deg=None):
    """背景のオフセットを作り (無ければ)、位置 (床の高さ) と向き (Z、度) を入れる。省いたものは録ったときのまま"""
    if not player.active:
        raise RuntimeError("先に select_take でテイクを選んでください")
    if bpy.data.objects.get(take.ANCHOR_NAME) is None:
        _call(context, bpy.ops.artrk.take_anchor_add)
    anchor = bpy.data.objects[take.ANCHOR_NAME]
    if location is not None:
        anchor.location = Vector([float(v) for v in location])
    if heading_deg is not None:
        anchor.rotation_euler.z = math.radians(float(heading_deg))
    context.view_layer.update()
    return _anchor_state()


def _bridge_remove_anchor(context):
    _call(context, bpy.ops.artrk.take_anchor_remove)
    return _anchor_state()


# configure で変えてよい設定 (書き出しまわりだけ)
_BRIDGE_SETTINGS = ("take_render_mode", "take_render_format", "take_render_size", "take_use_range",
                    "take_range_start", "take_range_end", "take_range_mode", "take_range_seconds",
                    "take_render_samples")


def _bridge_configure(context, **values):
    settings = context.scene.artrk
    unknown = sorted(set(values) - set(_BRIDGE_SETTINGS))
    if unknown:
        raise RuntimeError("変えられない設定: %s (使えるのは %s)" % (", ".join(unknown), ", ".join(_BRIDGE_SETTINGS)))
    for key, value in values.items():
        setattr(settings, key, value)
    return {key: getattr(settings, key) for key in _BRIDGE_SETTINGS}


def _bridge_export_take(context):
    """書き出しを始めて返す (終わりは state の export で見る)"""
    if _export["running"]:
        raise RuntimeError("前の書き出しがまだ動いています")
    if not player.active:
        raise RuntimeError("先に select_take でテイクを選んでください")
    _call(context, bpy.ops.artrk.take_render)
    return _bridge_state(context)


_BRIDGE_HANDLERS = {
    "state": _bridge_state,
    "open_scene": _bridge_open_scene,
    "list_takes": _bridge_list_takes,
    "select_take": _bridge_select_take,
    "place_anchor": _bridge_place_anchor,
    "remove_anchor": _bridge_remove_anchor,
    "configure": _bridge_configure,
    "export_take": _bridge_export_take,
}


class ARTRK_OT_bridge_start(bpy.types.Operator):
    bl_idname = "artrk.bridge_start"
    bl_label = "通信ポートを開く"
    bl_description = ("外のツール (Griptape Nodes のノード、Claude などのエージェント、自作のスクリプト) から、"
                      "決まった操作 (シーンを開く・テイクを選ぶ・背景のオフセット・書き出しの設定・書き出し・状態) を "
                      "127.0.0.1 の HTTP で受ける。任意の Python は受けない")

    def execute(self, context):
        try:
            bridge.start(_BRIDGE_HANDLERS, context.scene.artrk.bridge_port)
        except OSError as exc:
            self.report({"ERROR"}, "通信ポートを開けません: %s" % exc)
            return {"CANCELLED"}
        return {"FINISHED"}


class ARTRK_OT_bridge_stop(bpy.types.Operator):
    bl_idname = "artrk.bridge_stop"
    bl_label = "通信ポートを閉じる"

    def execute(self, context):
        bridge.stop()
        return {"FINISHED"}


def _prefs(context):
    return context.preferences.addons[__package__].preferences


class ARTRK_OT_take_open(bpy.types.Operator):
    bl_idname = "artrk.take_open"
    bl_label = "フォルダを開く"

    sub: StringProperty(default="")

    def execute(self, context):
        path = _take_path(context)
        if path is None:
            return {"CANCELLED"}
        folder = os.path.join(path, self.sub) if self.sub else path
        subprocess.Popen(["open" if sys.platform == "darwin" else "explorer", folder])
        return {"FINISHED"}




# --- パネル ---
# 項目ごとに別のパネルにして、見出しの ▷ で畳めるようにする。見出しには状態と開始 / 停止のボタンを出すので、
# 畳んだままでも使える。細かい調整は子パネル (最初は閉じている) に入れる。

class _ARtrkPanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "trkAR"


class _ARtrkSubPanel(_ARtrkPanel):
    bl_options = {"DEFAULT_CLOSED"}


def _header_toggle(layout, running, start, stop, status=""):
    row = layout.row(align=True)
    if status:
        row.label(text=status)
    if running:
        row.operator(stop, text="", icon="PAUSE")
    else:
        row.operator(start, text="", icon="PLAY")


class ARTRK_PT_panel(_ARtrkPanel, bpy.types.Panel):
    bl_label = "カメラ追従 (OSC)"
    bl_order = 0

    def draw_header(self, context):
        self.layout.label(icon="CAMERA_DATA")

    def draw_header_preset(self, context):
        server = tracking.server
        status = ("再生中" if player.active else "受信中") if server.running else ""
        _header_toggle(self.layout, server.running, "artrk.tracking_start", "artrk.tracking_stop", status)

    def draw(self, context):
        layout = self.layout
        settings = context.scene.artrk
        server = tracking.server
        col = layout.column(align=True)
        col.enabled = not server.running
        col.prop(settings, "port")
        # iPhone の送信先 (SystemPC IP / Port) に入れる値。▶ で畳める
        header, body = layout.panel("ARTRK_send_to", default_closed=False)
        header.label(text="iPhone の送信先", icon="URL")
        if body:
            box = body.box()
            ips = osc.local_ipv4_addresses()
            if not ips:
                box.label(text="ネットワークに繋がっていません", icon="ERROR")
            for ip, kind, _dev in ips:
                row = box.row(align=True)
                row.label(text="%s  %s : %d" % (kind or "", ip, settings.port),
                          icon="LINKED" if kind == "有線" else ("RADIOBUT_ON" if kind == "Wi-Fi" else "NETWORK_DRIVE"))
                row.operator("artrk.copy_ip", text="", icon="COPYDOWN").ip = ip
            if server.running and server.last_sender:
                box.label(text="受信元 %s" % server.last_sender, icon="CHECKMARK")
        layout.prop(settings, "camera")
        if server.running:
            if player.active:
                layout.label(text="テイク再生中: カメラには書き込みません", icon="PLAY")
            layout.label(text="受信 %d 件 / UDP %d (:%d)" % (server.packet_count, server.datagram_count, server.port))
            if server.datagram_count == 0:
                col = layout.column(align=True)
                col.label(text="何も届いていません", icon="ERROR")
                col.label(text="iPhone の送信先 IP / ポートと、macOS の")
                col.label(text="プライバシー > ローカルネットワーク を確認")
            elif server.packet_count == 0:
                layout.label(text="/client/current が来ていません", icon="ERROR")
            if server.last_error:
                layout.label(text=server.last_error, icon="ERROR")
            _draw_pose_sync(layout, settings, detail=False)

        layout.row().prop(settings, "mode", expand=True)
        if settings.mode == "RELATIVE":
            layout.operator("artrk.reanchor", icon="ORIENTATION_CURSOR")
            if server.running:
                if tracking.is_anchored():
                    layout.label(text="カメラを動かすには 受信停止 → 移動 → 受信開始", icon="INFO")
                else:
                    layout.label(text="最初の姿勢を待っています…", icon="TIME")
        else:
            layout.operator("artrk.align_to_cursor", icon="PIVOT_CURSOR")


def _draw_pose_sync(layout, settings, detail):
    """映像 (NDI 受信) が姿勢よりどれだけ遅れているかと、「映像に合わせる」の状態。"""
    lag = tracking.video_lag() if ndi_in.running else None
    if lag is not None:
        layout.label(text="映像の遅れ %.0f ms (揺れ %.0f〜%.0f)" % lag, icon="TIME")
    state = tracking.sync["state"]
    if state == "SYNCED":
        layout.label(text="映像と同期中", icon="CHECKMARK")
    elif state == "TOO_LATE":
        layout.label(text="映像の遅れが大きすぎて揃えられません", icon="ERROR")
    elif state == "NO_TIME":
        layout.label(text="時刻が合わないので揃えていません (trkAR を更新)", icon="INFO")
    elif state == "NO_VIDEO" and detail:
        layout.label(text="実写を NDI 受信すると揃えられます", icon="INFO")


class ARTRK_PT_tracking_place(_ARtrkSubPanel, bpy.types.Panel):
    """スケール・位置合わせのオフセットと、カメラ視点の枠の外の暗さ (たまにしか触らないもの)。"""
    bl_label = "位置・表示"
    bl_parent_id = "ARTRK_PT_panel"
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        settings = context.scene.artrk
        col = layout.column(align=True)
        col.prop(settings, "scale")
        if settings.mode != "RELATIVE":
            col.prop(settings, "location_offset")
            col.prop(settings, "yaw_offset")
            col.prop(settings, "roll_offset")
        camera = settings.camera or context.scene.camera
        if camera is not None and camera.type == "CAMERA":
            # カメラ視点で枠の外を暗くする (カメラのプロパティ > ビューポート表示 > パスパルトゥー と同じ)
            row = layout.row(align=True)
            row.prop(camera.data, "show_passepartout", text="")
            sub = row.row(align=True)
            sub.active = camera.data.show_passepartout
            sub.prop(camera.data, "passepartout_alpha", text="枠の外の暗さ", slider=True)


class ARTRK_PT_tracking_detail(_ARtrkSubPanel, bpy.types.Panel):
    bl_label = "詳細"
    bl_parent_id = "ARTRK_PT_panel"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.artrk
        layout.prop(settings, "smoothing", slider=True)
        col = layout.column(align=True)
        row = col.row(align=True)
        row.prop(settings, "pose_sync")
        sub = row.row(align=True)
        sub.active = settings.pose_sync
        sub.prop(settings, "pose_sync_offset", text="補正 ms")
        if tracking.server.running:
            _draw_pose_sync(col, settings, detail=True)
        layout.prop(settings, "sync_fov")
        row = layout.row(align=True)
        row.prop(settings, "use_rig", toggle=True)
        sub = row.row(align=True)
        sub.enabled = settings.use_rig
        sub.prop(settings, "use_rig_lens", toggle=True)
        layout.prop(settings, "record")
        layout.prop(settings, "rec_from_remote")
        col = layout.column(align=True)
        col.prop(settings, "camera_id_filter")
        col.prop(settings, "client_id_filter")
        if settings.last_client_id:
            layout.label(text="Client: %s" % settings.last_client_id[:8])


def _controls_active(settings):
    return bool(any(settings.ctl_location) or settings.ctl_pan or settings.ctl_tilt
                or settings.ctl_roll or settings.ctl_lens > 0.0)


class ARTRK_PT_controls(_ARtrkSubPanel, bpy.types.Panel):
    """Blender 側のオフセット。値が入っているときは見出しに印を付ける。"""
    bl_label = "Blender でオフセット"
    bl_parent_id = "ARTRK_PT_panel"

    def draw_header_preset(self, context):
        if _controls_active(context.scene.artrk):
            self.layout.label(text="使用中", icon="ERROR")

    def draw(self, context):
        layout = self.layout
        settings = context.scene.artrk
        col = layout.column(align=True)
        col.prop(settings, "ctl_location")
        col = layout.column(align=True)
        col.prop(settings, "ctl_pan")
        col.prop(settings, "ctl_tilt")
        col.prop(settings, "ctl_roll")
        layout.prop(settings, "ctl_lens")
        row = layout.row(align=True)
        row.operator("artrk.ctl_reset", text="向きを戻す").target = "ROTATION"
        row.operator("artrk.ctl_reset", text="全部戻す").target = "ALL"
        row.operator("artrk.ctl_keyframe", icon="KEY_HLT")
        if player.active:
            # 録ったときのずれはカメラの動きに焼き込み済み。再生中に変えても効かない
            box = layout.box()
            box.label(text="テイクの再生中は効きません (録ったときのずれは焼き込み済み)", icon="INFO")
            box.label(text="テイクの位置は「録画・テイク > 背景のオフセット」で動かす")


class ARTRK_PT_ndi_in(_ARtrkPanel, bpy.types.Panel):
    bl_label = "NDI 受信 (実写)"
    bl_order = 1

    def draw_header(self, context):
        self.layout.label(icon="IMAGE_DATA")

    def draw_header_preset(self, context):
        status = ""
        if ndi_in.running:
            status = "受信中"
        elif player.active:
            status = "テイク"
        _header_toggle(self.layout, ndi_in.running, "artrk.ndi_in_start", "artrk.ndi_in_stop", status)

    def draw(self, context):
        layout = self.layout
        settings = context.scene.artrk
        col = layout.column(align=True)
        col.enabled = not ndi_in.running
        row = col.row(align=True)
        row.prop(settings, "ndi_in_source", text="")
        row.menu("ARTRK_MT_ndi_sources", text="", icon="DOWNARROW_HLT")
        col.prop(settings, "ndi_in_low")
        if settings.ndi_in_source and settings.ndi_name and settings.ndi_name in settings.ndi_in_source:
            layout.label(text="Blender 自身の送出です (映像がループします)", icon="ERROR")
        if ndi_in.running:
            width, height = ndi_in.resolution
            if ndi_in.frames_shown:
                layout.label(text="%d×%d / %d フレーム" % (width, height, ndi_in.frames_shown))
            else:
                layout.label(text="映像を待っています…", icon="TIME")
            if ndi_in.last_error:
                layout.label(text=ndi_in.last_error, icon="ERROR")


class ARTRK_PT_ndi_in_screen(_ARtrkSubPanel, bpy.types.Panel):
    bl_label = "スクリーン・背景"
    bl_parent_id = "ARTRK_PT_ndi_in"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.artrk
        layout.label(text="画像「%s」として使えます" % ndi_input.IMAGE_NAME, icon="INFO")
        # 「スクリーンを置く」(原点に板を置く) は使わないのでボタンを外した (オペレーターは残してある)
        layout.operator("artrk.ndi_in_camera_bg", icon="VIEW_CAMERA")
        layout.prop(settings, "ndi_in_lens_crop")
        row = layout.row(align=True)
        row.operator("artrk.ndi_in_front", icon="OUTLINER_OB_CAMERA")
        row.prop(settings, "ndi_in_front_distance", text="距離")


class ARTRK_PT_depth(_ARtrkPanel, bpy.types.Panel):
    bl_label = "深度 (LiDAR)"
    bl_order = 2

    def draw_header(self, context):
        self.layout.label(icon="MOD_DISPLACE")

    def draw_header_preset(self, context):
        status = ""
        if depth_in.running:
            status = "受信中"
        elif player.active:
            status = "テイク"
        _header_toggle(self.layout, depth_in.running, "artrk.depth_start", "artrk.depth_stop", status)

    def draw(self, context):
        layout = self.layout
        settings = context.scene.artrk
        col = layout.column(align=True)
        col.enabled = not depth_in.running
        col.row(align=True).prop(settings, "depth_transport", expand=True)
        if settings.depth_transport == "UDP":
            col.prop(settings, "depth_udp_port")
        else:
            row = col.row(align=True)
            row.prop(settings, "depth_source", text="")
            row.menu("ARTRK_MT_depth_sources", text="", icon="DOWNARROW_HLT")
        if depth_in.running:
            if depth_in.frames_shown:
                width, height = depth_in.resolution
                layout.label(text="%d×%d / %d フレーム / 有効 %d%%" % (
                    width, height, depth_in.frames_shown, depth_in.valid_ratio * 100))
            elif bpy.data.objects.get(depth_input.OBJECT_NAME) is None:
                layout.label(text="深度スクリーンがありません", icon="ERROR")
            else:
                layout.label(text="深度を待っています… (%s)" % depth_in.source_name, icon="TIME")
            if depth_in.last_error:
                layout.label(text=depth_in.last_error, icon="ERROR")
        row = layout.row(align=True)
        row.operator("artrk.depth_screen", icon="OUTLINER_OB_CAMERA")
        row.operator("artrk.depth_freeze", icon="FREEZE")
        if not ndi_in.running and not player.active:
            layout.label(text="映像は NDI 受信 (実写) から貼られます", icon="INFO")
        if _needs_material_view(context) and bpy.data.objects.get(depth_input.OBJECT_NAME) is not None:
            layout.label(text="ソリッド表示では実写の色が付きません", icon="ERROR")
            layout.operator("artrk.view_material", icon="MATERIAL")


class ARTRK_PT_depth_people(_ARtrkSubPanel, bpy.types.Panel):
    bl_label = "人物・背景"
    bl_parent_id = "ARTRK_PT_depth"

    def draw(self, context):
        settings = context.scene.artrk
        col = self.layout.column(align=True)
        col.prop(settings, "depth_flat_cutout")
        if settings.depth_flat_cutout:
            if depth_in.running:
                col.label(text="人 %d%% (ARtrk NDI のアルファに直接使用中)" % (depth_in.person_ratio * 100),
                          icon="USER")
            else:
                col.label(text="深度受信 (UDP) を開始してください", icon="INFO")
            return
        if depth_in.running and depth_in.video_alpha:
            # artrk_matte.py (RVM) の "ARtrk Matte" を受けているとき
            col.label(text="輪郭は実写のアルファ (RVM) で切り抜き中", icon="MOD_MASK")
        if depth_in.running and depth_in.person_only:
            # 深度なしの機種: 人だけを表示する (人物の設定は効かない)
            col.label(text="深度なし: 人物マスクだけ (人 %d%%)" % (depth_in.person_ratio * 100),
                      icon="USER")
            col.prop(settings, "depth_person_distance")
        else:
            col.row(align=True).prop(settings, "depth_people", expand=True)
            if settings.depth_people != "OFF" and depth_in.running and depth_in.frames_shown:
                if depth_in.has_people:
                    col.label(text="人 %d%%" % (depth_in.person_ratio * 100), icon="USER")
                else:
                    col.label(text="人物マスクが来ていません (UDP と新しい trkAR が必要)", icon="INFO")
        row = col.row(align=True)
        row.prop(settings, "depth_background", toggle=True)
        sub = row.row(align=True)
        sub.active = settings.depth_background
        sub.prop(settings, "depth_background_distance", text="距離")
        if (settings.depth_background and depth_in.running and depth_in.person_only
                and settings.depth_background_distance <= settings.depth_person_distance):
            col.label(text="背景が人より手前です (人の距離より遠くする)", icon="ERROR")


class ARTRK_PT_depth_detail(_ARtrkSubPanel, bpy.types.Panel):
    bl_label = "メッシュ・輪郭・同期"
    bl_parent_id = "ARTRK_PT_depth"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.artrk
        col = layout.column(align=True)
        col.row(align=True).prop(settings, "depth_step", expand=True)
        col.prop(settings, "depth_max_distance")
        col.row(align=True).prop(settings, "depth_confidence", expand=True)
        col.prop(settings, "depth_edge_tolerance", slider=True)
        col = layout.column(align=True)
        col.prop(settings, "depth_edge_shift", slider=True)
        col.prop(settings, "depth_edge_feather", slider=True)
        col.prop(settings, "depth_denoise")
        col.prop(settings, "depth_temporal", slider=True)
        col = layout.column(align=True)
        row = col.row(align=True)
        row.prop(settings, "depth_sync")
        sub = row.row(align=True)
        sub.active = settings.depth_sync
        sub.prop(settings, "depth_sync_offset", text="補正 ms")
        if settings.depth_sync and depth_in.running and depth_in.frames_shown:
            if depth_in.sync_lag is not None:
                col.label(text="映像と同期中 (深度 %+.0f ms)" % -depth_in.sync_lag, icon="CHECKMARK")
            elif not ndi_in.running:
                col.label(text="実写を NDI 受信すると揃えられます", icon="INFO")
            else:
                col.label(text="時刻が合わないので揃えていません (trkAR を更新)", icon="INFO")


class ARTRK_PT_take(_ARtrkPanel, bpy.types.Panel):
    bl_label = "録画・テイク"
    bl_order = 3

    def draw_header(self, context):
        self.layout.label(icon="REC")

    def draw_header_preset(self, context):
        if recorder.recording:
            row = self.layout.row()
            row.alert = True
            seconds = int(recorder.seconds)
            row.operator("artrk.take_rec_stop", icon="PAUSE", text="%d:%02d" % (seconds // 60, seconds % 60))
        elif export_progress() is not None:
            done, total, _remaining = export_progress()
            self.layout.label(text="書き出し %d/%d" % (done, total))
        elif player.active:
            self.layout.label(text="再生中")

    def draw(self, context):
        layout = self.layout
        settings = context.scene.artrk
        if recorder.recording:
            row = layout.row()
            row.alert = True
            row.scale_y = 1.4
            seconds = int(recorder.seconds)
            row.operator("artrk.take_rec_stop", icon="PAUSE",
                         text="録画停止  %d:%02d  (%d フレーム)" % (seconds // 60, seconds % 60, recorder.frames))
            if recorder.slate_label:
                layout.label(text="%s を録画中" % recorder.slate_label, icon="FILE_MOVIE")
            if recorder.dropped or recorder.errors:
                layout.label(text="間引き %d / 書き込みエラー %d %s" % (recorder.dropped, recorder.errors,
                                                                  recorder.last_error), icon="ERROR")
            return
        prefs = _prefs(context)
        layout.label(text="次  %s   %s" % (_slate_label(prefs), prefs.slate_production or "-"), icon="FILE_MOVIE")
        row = layout.row(align=True)
        row.scale_y = 1.4
        row.operator("artrk.take_rec_start", icon="REC")
        row.prop(settings, "take_fps", text="")
        if not tracking.server.running:
            layout.label(text="トラッキングが止まっているとカメラは動きません", icon="INFO")
        if not ndi_in.running and not player.active:
            layout.label(text="NDI 受信 (実写) が止まっていると映像は録れません", icon="INFO")

        layout.prop(settings, "take_path", text="")
        row = layout.row(align=True)
        row.enabled = not _export["running"]
        if player.active:
            row.operator("artrk.take_live", icon="LOOP_BACK")
        else:
            row.operator("artrk.take_play", icon="PLAY")
        row.operator("artrk.take_open", icon="FILE_FOLDER", text="").sub = ""
        progress = export_progress()
        if progress is not None:
            done, total, remaining = progress
            text = "書き出し中 %d / %d" % (done, total)
            if remaining is not None:
                text += "  残り約 %d 秒" % remaining
            layout.label(text=text, icon="RENDER_ANIMATION")
            layout.label(text="Esc で中止")
        else:
            row = layout.row(align=True)
            row.operator("artrk.take_render", icon="RENDER_ANIMATION")
            row.prop(settings, "take_render_format", text="")
            row = layout.row(align=True)
            row.prop(settings, "take_use_range", toggle=True)
            sub = row.row(align=True)
            sub.active = settings.take_use_range
            sub.prop(settings, "take_range_start", text="開始")
            if settings.take_range_mode == "DURATION":
                sub.prop(settings, "take_range_seconds", text="秒")
            else:
                sub.prop(settings, "take_range_end", text="終了")
            if settings.take_use_range:
                layout.row(align=True).prop(settings, "take_range_mode", expand=True)
            count = _export_count(context, settings)
            if count is not None:
                text = "%d フレーム (%.2f 秒)" % count
                if settings.take_use_range:
                    first, last = _take_range(settings, context.scene.render,
                                              player.info["frame_start"], player.info["frame_end"])
                    text = "%d〜%d  " % (first, last) + text
                    wanted = round(settings.take_range_seconds * context.scene.render.fps
                                   / context.scene.render.fps_base)
                    if settings.take_range_mode == "DURATION" and count[0] < max(1, wanted):
                        text += "  テイクの終わりで切れます"
                if settings.take_render_size == "LTX" and count[0] > LTX_MAX_FRAMES:
                    layout.label(text=text + "  LTX は %d フレームまで" % LTX_MAX_FRAMES, icon="ERROR")
                else:
                    layout.label(text=text)
            if _export["message"]:
                layout.label(text=_export["message"], icon="CHECKMARK")
            take_path = _take_path(context)
            if take_path is not None:
                _draw_hook(layout, context, take_path)
        if player.active:
            info = player.info
            layout.label(text="%s  %d〜%d フレーム" % (info.get("name", ""), info["frame_start"], info["frame_end"]),
                         icon="SEQUENCE")
        if player.last_error:
            layout.label(text=player.last_error, icon="ERROR")


# 検証済みの LTX-2.5 V2V の長さ (24fps で約 5 秒)
LTX_MAX_FRAMES = 121


def _export_count(context, settings):
    """書き出すフレーム数と秒数。テイクを再生していないときは None。"""
    if not player.active:
        return None
    first, last = player.info["frame_start"], player.info["frame_end"]
    render = context.scene.render
    if settings.take_use_range:
        first, last = _take_range(settings, render, first, last)
    count = last - first + 1
    return count, count / (render.fps / render.fps_base)


def _take_range(settings, render, first, last):
    """書き出す (開始, 終了) フレーム。テイクの first〜last に収める。尺で決めるときは
    終了 = 開始 + round(秒 × fps) − 1 (終了フレームも書き出すので 1 引く)。"""
    start = min(max(settings.take_range_start, first), last)
    if settings.take_range_mode == "DURATION":
        fps = render.fps / render.fps_base
        end = start + max(1, round(settings.take_range_seconds * fps)) - 1
    else:
        end = settings.take_range_end
    return start, min(max(end, start), last)


class ARTRK_PT_take_slate(_ARtrkSubPanel, bpy.types.Panel):
    bl_label = "スレート・撮影情報"
    bl_parent_id = "ARTRK_PT_take"
    bl_options = set()

    def draw_header_preset(self, context):
        self.layout.label(text=_slate_label(_prefs(context)))

    def draw(self, context):
        layout = self.layout
        _draw_slate_settings(layout, _prefs(context))
        # take.json の device に書かれる機種 (/client/info)
        pose = tracking.current_pose()
        client = tracking.server.info_for(pose.client_id) if pose is not None else None
        if client is not None:
            text = "%s  %s %s  app %s (%s)" % (client.model, client.system_name, client.system_version,
                                               client.app_version, client.app_build)
            layout.label(text=text, icon="CAMERA_DATA")
        elif pose is not None:
            layout.label(text="機種の情報 (/client/info) がまだ届いていません", icon="INFO")
        else:
            layout.label(text="トラッキングを受信していないので機種は記録されません", icon="INFO")


class ARTRK_PT_take_anchor(_ARtrkSubPanel, bpy.types.Panel):
    bl_label = "背景のオフセット"
    bl_parent_id = "ARTRK_PT_take"

    def draw_header_preset(self, context):
        if bpy.data.objects.get(take.ANCHOR_NAME) is not None:
            self.layout.label(text="オフセット中")

    def draw(self, context):
        layout = self.layout
        anchor = bpy.data.objects.get(take.ANCHOR_NAME)
        if anchor is None:
            layout.label(text="テイクは録ったときの位置で再生されます", icon="INFO")
            layout.operator("artrk.take_anchor_add", icon="EMPTY_ARROWS")
            return
        layout.label(text="人の居る所 (床の高さ) と、カメラの見る向き (Y の矢印)")
        col = layout.column(align=True)
        col.prop(anchor, "location", text="")
        layout.prop(anchor, "rotation_euler", index=2, text="向き (Z)")
        row = layout.row(align=True)
        row.operator("artrk.take_anchor_cursor", icon="PIVOT_CURSOR")
        row.operator("artrk.take_anchor_add", icon="LOOP_BACK", text="元の位置")
        layout.operator("artrk.take_anchor_remove", icon="X")
        if not player.active:
            layout.label(text="オフセットが効くのはテイクの再生・書き出しの間だけ", icon="INFO")
        else:
            layout.label(text="置いた場所はこのテイクに残る (ほかのテイクには効かない)", icon="INFO")
            if player.previous_anchor is not None:
                layout.operator("artrk.take_anchor_previous", icon="RECOVER_LAST")


class ARTRK_PT_take_bridge(_ARtrkSubPanel, bpy.types.Panel):
    bl_label = "通信ポート (自動化)"
    bl_parent_id = "ARTRK_PT_take"
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header_preset(self, context):
        if bridge.running():
            self.layout.label(text="開いています")

    def draw(self, context):
        layout = self.layout
        if bridge.running():
            layout.label(text="http://127.0.0.1:%d" % bridge.port(), icon="LINKED")
            if bridge.last():
                layout.label(text=bridge.last())
            layout.operator("artrk.bridge_stop", icon="X")
        else:
            layout.prop(context.scene.artrk, "bridge_port")
            layout.operator("artrk.bridge_start", icon="PLAY")


class ARTRK_PT_take_settings(_ARtrkSubPanel, bpy.types.Panel):
    bl_label = "録画・書き出しの設定"
    bl_parent_id = "ARTRK_PT_take"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.artrk
        row = layout.row()
        row.prop(settings, "take_fps")
        if settings.take_fps == "SCENE":
            render = context.scene.render
            row.label(text="%.3g fps" % (render.fps / render.fps_base))
        layout.prop(settings, "take_folder")
        layout.prop(settings, "take_render_mode")
        layout.prop(settings, "take_render_size")
        sub = layout.row()
        sub.active = settings.take_render_mode == "VIEWPORT"
        sub.prop(settings, "take_render_samples")
        box = layout.box()
        box.label(text="テイクのあとに流すコマンド (連携)", icon="SCRIPT")
        _draw_hook_settings(box, _prefs(context))




class ARTRK_PT_ndi_out(_ARtrkSubPanel, bpy.types.Panel):
    bl_label = "カメラ視点を NDI 送出"
    bl_order = 4

    def draw_header(self, context):
        self.layout.label(icon="RENDER_ANIMATION")

    def draw_header_preset(self, context):
        _header_toggle(self.layout, ndi_output.running, "artrk.ndi_start", "artrk.ndi_stop",
                       "送出中" if ndi_output.running else "")

    def draw(self, context):
        layout = self.layout
        settings = context.scene.artrk
        col = layout.column(align=True)
        col.enabled = not ndi_output.running
        col.prop(settings, "ndi_name")
        row = col.row(align=True)
        row.prop(settings, "ndi_width")
        row.prop(settings, "ndi_height")
        row.operator("artrk.ndi_swap_size", text="", icon="FILE_REFRESH")
        col.prop(settings, "ndi_fps")
        col.prop(settings, "ndi_transparent")
        if ndi_output.running:
            this_view = context.area is not None and context.area.as_pointer() == ndi_output.area_pointer
            layout.label(text="送出元: %s" % ("このビュー" if this_view else "別のビュー"))
            layout.label(text="%d フレーム / 受信者 %d" % (ndi_output.frames_sent, ndi_output.sender.connections()))
            layout.label(text="描画 %.1f ms / 読み戻し %.1f ms" % (ndi_output.draw_ms, ndi_output.read_ms))
            if ndi_output.last_error:
                layout.label(text=ndi_output.last_error, icon="ERROR")
        else:
            layout.label(text="このビューの表示モードで描画されます", icon="INFO")


def _redraw_panels():
    try:
        # テイク再生中は録画したマスクをそのまま出す
        if not player.active:
            depth_in.idle_update(bpy.context.scene)
    except Exception as exc:
        depth_in.last_error = str(exc)
    # 受信件数などのステータス表示を更新する
    busy = (tracking.server.running or ndi_output.running or ndi_in.running or depth_in.running
            or recorder.recording or _export["running"] or hooks.busy())
    if busy:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    for region in area.regions:
                        if region.type == "UI":
                            region.tag_redraw()
    return 0.5


@persistent
def _on_load_pre(*_args):
    # ファイルを開き直すとカメラや 3D ビューが入れ替わるので止めておく
    if recorder.recording:
        recorder.stop(bpy.context)
    player.stop(None)
    tracking.stop()
    ndi_output.stop()
    ndi_in.stop()
    depth_in.stop()


@persistent
def _on_save_pre(*_args):
    # 受けている映像のコマが .blend に埋め込まれないようにする (ndi_input.blank_live_images)。
    # 深度スナップショット (ARtrk Depth Snapshot) は自分で止めたコマなので残す
    ndi_input.blank_live_images((ndi_input.IMAGE_NAME, depth_input.MASK_IMAGE_NAME))
    # 再生中に動かした置き場を、そのテイクの take.json にも残す (再生を止めずに保存して閉じても失わない)
    try:
        player.save_placement()
    except Exception as exc:
        print("trkAR: テイクの置き場を残せません: %s" % exc)


classes = (
    ARTRK_Preferences,
    ARTRK_Settings,
    ARTRK_OT_tracking_start,
    ARTRK_OT_tracking_stop,
    ARTRK_OT_copy_ip,
    ARTRK_OT_reanchor,
    ARTRK_OT_align_to_cursor,
    ARTRK_OT_ctl_reset,
    ARTRK_OT_ctl_keyframe,
    ARTRK_OT_ndi_swap_size,
    ARTRK_OT_ndi_start,
    ARTRK_OT_ndi_stop,
    ARTRK_MT_ndi_sources,
    ARTRK_MT_depth_sources,
    ARTRK_OT_ndi_in_set_source,
    ARTRK_OT_ndi_in_start,
    ARTRK_OT_ndi_in_stop,
    ARTRK_OT_ndi_in_screen,
    ARTRK_OT_ndi_in_front,
    ARTRK_OT_ndi_in_camera_bg,
    ARTRK_OT_view_material,
    ARTRK_OT_depth_start,
    ARTRK_OT_depth_stop,
    ARTRK_OT_depth_screen,
    ARTRK_OT_depth_freeze,
    ARTRK_OT_take_rec_start,
    ARTRK_OT_slate_unset,
    ARTRK_OT_take_rec_stop,
    ARTRK_OT_take_play,
    ARTRK_OT_take_live,
    ARTRK_OT_take_anchor_add,
    ARTRK_OT_take_anchor_cursor,
    ARTRK_OT_take_anchor_previous,
    ARTRK_OT_take_anchor_remove,
    ARTRK_OT_take_render,
    ARTRK_OT_take_open,
    ARTRK_OT_take_hook_run,
    ARTRK_OT_bridge_start,
    ARTRK_OT_bridge_stop,
    ARTRK_PT_panel,
    ARTRK_PT_tracking_place,
    ARTRK_PT_tracking_detail,
    ARTRK_PT_controls,
    ARTRK_PT_ndi_in,
    ARTRK_PT_ndi_in_screen,
    ARTRK_PT_depth,
    ARTRK_PT_depth_people,
    ARTRK_PT_depth_detail,
    ARTRK_PT_take,
    ARTRK_PT_take_slate,
    ARTRK_PT_take_anchor,
    ARTRK_PT_take_bridge,
    ARTRK_PT_take_settings,
    ARTRK_PT_ndi_out,
)

# 社内向け (STUDIO のときだけ登録する。親パネルより後ろに置く)
studio_classes = ()


def register():
    for cls in classes + studio_classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.artrk = PointerProperty(type=ARTRK_Settings)
    tracking.hold = lambda: player.active
    tracking.rec_hook = _on_remote_rec
    bpy.app.handlers.load_pre.append(_on_load_pre)
    bpy.app.handlers.save_pre.append(_on_save_pre)
    bpy.app.timers.register(_redraw_panels, persistent=True)
    bpy.app.timers.register(_update_camera_screen, persistent=True)


def unregister():
    if recorder.recording:
        recorder.stop(bpy.context)
    player.stop(bpy.context.scene)
    tracking.stop()
    tracking.hold = None
    tracking.rec_hook = None
    ndi_output.stop()
    depth_in.stop()
    bridge.stop()
    hooks.stop()
    ndi_in.close()
    if bpy.app.timers.is_registered(_redraw_panels):
        bpy.app.timers.unregister(_redraw_panels)
    if bpy.app.timers.is_registered(_update_camera_screen):
        bpy.app.timers.unregister(_update_camera_screen)
    if _on_load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_on_load_pre)
    if _on_save_pre in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.remove(_on_save_pre)
    del bpy.types.Scene.artrk
    for cls in reversed(classes + studio_classes):
        bpy.utils.unregister_class(cls)
