"""trkAR Camera — trkAR (iPhone) の OSC でカメラを動かし、そのカメラ視点を NDI で返す。"""

import importlib
import json
import math
import os
import re
import subprocess
import sys
import time

# 上書きインストール時、Blender は __init__ だけを読み直してサブモジュールは
# 古いまま使い回す。依存の浅い順に読み直して新旧が混ざらないようにする
for _name in ("osc", "ndi", "ndi_output", "screen_output", "os_screen", "ndi_input", "depth_input", "matte", "take", "awscli", "griptape", "ledger",
              "look", "hooks", "tracking", "view_controller", "bridge"):
    _module = sys.modules.get("%s.%s" % (__name__, _name))
    if _module is not None:
        importlib.reload(_module)

import bpy
from bpy.app.handlers import persistent
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from mathutils import Matrix, Vector

from . import (bridge, depth_input, hooks, i18n, legacy, matte, ndi_input, os_screen, osc, perf, prep, screen_output,
               take, tracking,
               view_controller)

STUDIO = False
_DEFAULT_PRODUCTION = ""
from .depth_input import DepthInput
from .ndi_input import NDIInput
from .ndi_output import NDIOutput
from .screen_output import ScreenOutput
from .i18n import _t

ndi_output = NDIOutput()
screen_out = ScreenOutput()
os_screen_out = os_screen.OSScreenOutput()
ndi_in = NDIInput()
depth_in = DepthInput()
depth_in.video = ndi_in
tracking.video = ndi_in
tracking.depth = depth_in
# RVM の切り抜きの中継 (別プロセスの tools/matte/trkar_matte.py)
matte_relay = matte.MatteRelay()
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


_take_offset_quiet = {"on": False}


def _take_offset_update(self, context):
    if _take_offset_quiet["on"]:
        return
    _set_take_offset(context, self.take_time_offset)


def _set_take_offset(context, offset):
    """再生中のテイクを、背景の CG に対して offset コマずらす。タイムラインの今のコマ (CG の時間) はそのままで、
    書き出しの範囲は同じ撮影の所を指すように一緒に動かす"""
    scene = context.scene
    settings = scene.trkar
    if not player.active:
        _sync_take_offset(settings, 0)
        return 0
    frame = scene.frame_current
    delta = player.set_time_offset(scene, offset)
    _sync_take_offset(settings, player.info["time_offset"])
    if delta:
        _take_offset_quiet["on"] = True
        try:
            settings.take_range_start = max(0, settings.take_range_start + delta)
            settings.take_range_end = max(0, settings.take_range_end + delta)
        finally:
            _take_offset_quiet["on"] = False
        _apply_take_range(context)
        if scene.frame_start <= frame <= scene.frame_end:
            scene.frame_set(frame)  # 同じ CG のコマに、ずらした実写のコマを読み直す
    return delta


def _sync_take_offset(settings, offset):
    if settings.take_time_offset != offset:
        _take_offset_quiet["on"] = True
        try:
            settings.take_time_offset = offset
        finally:
            _take_offset_quiet["on"] = False


def _take_range_update(_self, context):
    # 書き出しの「範囲」を変えたら、再生中のテイクのタイムラインの in / out にも入れる
    _apply_take_range(context)


def _take_path_update(_self, context):
    # 再生中にテイクのフォルダを選び直したら、そのテイクを読み直す (ライブ ↔ 再生を押し直さなくてよいように)
    _reload_selected_take(context)


def _set_ui_language(context, mode):
    """表示言語を変えたら、辞書を入れ直してパネルを描き直す"""
    i18n.register(mode)
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            area.tag_redraw()


def _reset_take(self, _context):
    # Scene / Cut が変わったら Take は 1 から。どちらも 0 (スレートを設定していない) なら 0 のまま (S00C00T00)
    self.slate_take = 1 if (self.slate_scene or self.slate_cut) else 0


class TRKAR_Preferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    ndi_library_path: StringProperty(
        name="NDI Library",
        description="Path to libndi.dylib / Processing.NDI.Lib.x64.dll. Empty = detect automatically",
        subtype="FILE_PATH",
    )
    ui_language: EnumProperty(
        name="Interface Language", default="AUTO",
        items=(("AUTO", "Follow Blender", "Japanese when Blender is set to Japanese, English otherwise"),
               ("JA", "日本語", "Japanese even when Blender is set to English"),
               ("EN", "English", "English even when Blender is set to Japanese")),
        update=lambda self, context: _set_ui_language(context, self.ui_language))
    show_sidebar: BoolProperty(
        name="Also Show in 3D View Sidebar", default=True,
        description="The panels always appear in the \"Scene\" tab of the Properties editor. Turn this on to also show them in the trkAR tab of the 3D View sidebar (N key)",
        update=lambda self, _context: _sync_sidebar(self.show_sidebar))

    matte_folder: StringProperty(
        name="RVM Relay Folder",
        description="Relay that mattes people out with AI (the folder with tools/matte's trkar_matte.py and .venv). Empty = search near the add-on",
        subtype="DIR_PATH",
    )


    # --- 撮影の来歴 (take.json の slate / environment / people)。.blend ごとではなくこの PC の設定に持つ
    # (撮影中に背景の .blend を開き替えても、スレートの番号が続くように) ---
    slate_production: StringProperty(name="Production", default=_DEFAULT_PRODUCTION,
                                     description="Production name (slate.production in take.json). Empty = not recorded (null)")
    slate_scene: IntProperty(name="Scene", default=0, min=0, update=_reset_take,
                             description="Scene number. 0 = not set (S00C00T00). Changing it resets Take to 1")
    slate_cut: IntProperty(name="Cut", default=0, min=0, update=_reset_take,
                           description="Cut number. 0 = not set. Changing it resets Take to 1")
    slate_take: IntProperty(name="Take", default=0, min=0,
                            description="Number of the next take to record. If Scene or Cut is set, it goes up by 1 each time REC starts")
    shot_site: StringProperty(name="Shoot Location", default="",
                              description="Place or event of the shoot (e.g. InterBee). Empty = not recorded (null)")
    shot_host: StringProperty(name="PC Display Name", default="",
                              description="A name for this PC (e.g. Mac mini). Empty = not recorded (null). The hostname is recorded automatically as well")
    shot_creator: StringProperty(name="Creator", default="", description="Empty = not recorded (null)")
    shot_operator: StringProperty(name="Camera Operator", default="", description="Empty = not recorded (null)")

    # --- テイクのあとに流すコマンド (クラウドなどへの連携の口。hooks.py / INTEGRATION.md) ---
    hook_command: StringProperty(
        name="Command",
        description="Command to run after recording or exporting a take. Replaces {take_dir} {event} {render} {take_json} (e.g. python3 ~/trkar_upload.py {take_dir} {event}). Empty = do nothing")
    hook_on_record: BoolProperty(name="After Recording Stops", default=True,
                                 description="Run the command when recording stops (event = recorded)")
    hook_on_export: BoolProperty(name="After Export", default=True,
                                 description="Run the command when an export finishes (event = exported)")
    hook_timeout: IntProperty(name="Timeout (min)", default=30, min=1, max=24 * 60,
                              description="Stop the command if it does not finish within this time")


    def draw(self, _context):
        self.layout.prop(self, "ui_language")
        self.layout.prop(self, "ndi_library_path")
        self.layout.prop(self, "show_sidebar")
        self.layout.prop(self, "matte_folder")
        box = self.layout.box()
        box.label(text=_t("Shoot Provenance (take.json)"), icon="FILE_MOVIE")
        _draw_slate_settings(box, self)
        box = self.layout.box()
        box.label(text=_t("After-Take Command (Integration)"), icon="SCRIPT")
        _draw_hook_settings(box, self)




class TRKAR_Settings(bpy.types.PropertyGroup):
    # --- 受信 (OSC) ---
    port: IntProperty(name="OSC Port", default=8000, min=1, max=65535,
                      description="Match trkAR's SystemPC OSC Port")
    camera: PointerProperty(name="Camera", type=bpy.types.Object, poll=_is_camera,
                            description="Camera to move. Empty = the scene's active camera")
    camera_id_filter: IntProperty(name="Camera ID", default=-1, min=-1,
                                  description="Only receive this trackingCameraID (-1 = all)")
    client_id_filter: StringProperty(name="Client ID", default="",
                                     description="Only receive this clientID (empty = all)")
    mode: EnumProperty(
        name="Reference",
        items=(
            ("RELATIVE", "Current Camera Position", "Move the camera only as much as the iPhone moves, relative to the camera's position and rotation when receiving started"),
            ("ABSOLUTE", "iPhone Origin", "Use the iPhone's coordinates as is (align with the offsets)"),
        ),
        default="RELATIVE",
        update=_reanchor_if_relative,
    )
    floor_from_iphone: BoolProperty(
        name="Height from iPhone Floor", default=True, update=_reset_smoothing,
        description="Even when using the current camera position as the reference, take only the height from the floor height measured by the iPhone. The video floor then matches the Blender floor (Floor Height). Only when the iPhone has a floor (SET GND, or a floor plane found automatically). Off = the height also moves only as much as it moved from the Blender camera")
    floor_z: FloatProperty(
        name="Floor Height", default=0.0, subtype="DISTANCE", update=_reset_smoothing,
        description="Height of the Blender floor (world Z) to match the video floor. With iPhone Origin, this is the Z of Location Offset")
    scale: FloatProperty(name="Scale", default=1.0, min=0.001, soft_max=100.0,
                         description="Movement multiplier (1 = 1 m in the real world is 1 m in Blender)",
                         update=_reanchor_if_relative)
    location_offset: FloatVectorProperty(name="Location Offset", subtype="TRANSLATION", size=3,
                                         update=_reset_smoothing)
    yaw_offset: FloatProperty(name="Rotation Offset (Z)", default=0.0, soft_min=-180.0, soft_max=180.0,
                              description="Rotation around the world Z axis (degrees)", update=_reset_smoothing)
    roll_offset: FloatProperty(name="Roll Correction", default=0.0, soft_min=-180.0, soft_max=180.0,
                               description="Rotation around the view axis (degrees). trkAR's portrait orientation is already corrected in the app",
                               update=_reset_smoothing)
    smoothing: FloatProperty(name="Smoothing", default=0.0, min=0.0, max=0.95,
                             description="0 = raw data. Higher is smoother but adds delay")
    pose_sync: BoolProperty(name="Sync to Video", default=True,
                            description="While receiving live video (NDI), use the pose captured at the same time as the frame on screen. The pose arrives before the video, so otherwise the CG camera moves ahead of the video. The camera is delayed by the video latency")
    pose_predict: BoolProperty(name="Predict Pose Latency", default=True,
                               description="When the pose briefly stalls over Wi-Fi and the pose for the displayed frame's capture time has not arrived yet, predict ahead from the last movement (up to %d ms). Off = the camera holds the latest pose meanwhile and jumps when it arrives" % int(tracking.PREDICT_LIMIT * 1000))
    pose_sync_offset: FloatProperty(name="Offset Correction", default=0.0, min=-300.0, max=300.0, subtype="NONE",
                                    description="+ delays the camera further / − makes it earlier (ms). If the CG still moves first after syncing, use +")
    sync_fov: BoolProperty(name="Sync Field of View", default=True,
                           description="Apply the horizontal FOV sent from the iPhone to the camera")
    use_rig: BoolProperty(name="iPhone Controller", default=True,
                          description="Add the offset moved with the trkAR joystick (/client/rig)",
                          update=_reanchor_if_relative)
    use_rig_lens: BoolProperty(name="iPhone Lens Settings", default=True,
                               description="Apply the focal length, f-stop, and focus distance from trkAR's LENS panel")
    # --- Blender 側のオフセット (キーフレーム可) ---
    ctl_location: FloatVectorProperty(name="Move", subtype="TRANSLATION", size=3,
                                      description="Movement added to the camera position (world coordinates)")
    ctl_pan: FloatProperty(name="Pan", default=0.0, soft_min=-180.0, soft_max=180.0,
                           description="Rotation around world Z with the camera position as the pivot (degrees)")
    ctl_tilt: FloatProperty(name="Tilt", default=0.0, soft_min=-90.0, soft_max=90.0,
                            description="Rotation around the camera's left-right axis (degrees). + tilts up")
    ctl_roll: FloatProperty(name="Roll", default=0.0, soft_min=-180.0, soft_max=180.0,
                            description="Rotation around the view axis (degrees)")
    ctl_fstop: FloatProperty(name="F-Stop", default=0.0, min=-1.0, soft_max=22.0,
                             description="Depth of field f-stop. 0 = do not override (iPhone lens settings / camera settings), -1 = turn off depth of field")
    ctl_focus: FloatProperty(name="Focus Distance", default=0.0, min=0.0, soft_max=50.0, unit="LENGTH",
                             description="0 = do not override. Autofocus (Focus on Person Plate) takes priority when it is on")
    ctl_pan_shift: FloatVectorProperty(
        name="Pan Compensation", subtype="TRANSLATION", size=3,
        description="Position added so the camera does not jump when the pan changes (automatic. \"Reset All\" sets it to 0)")
    ctl_lens: FloatProperty(name="Focal Length", default=0.0, min=0.0, soft_max=300.0,
                            description="35mm equivalent (mm). 0 = do not override (follows iPhone lens settings / field of view sync)")
    use_remote: BoolProperty(
        name="Control from Another Device (Remote)", default=True,
        description="Add the sticks of the monitor device (the one that selected this camera's Camera ID under TARGET in trkAR's CTL) to this offset. While it is sending, the lens (focal length, f-stop, focus) also uses that device's settings")
    record: BoolProperty(name="Record Keyframes During Playback", default=False,
                         description="Record the received pose every frame while the timeline plays")
    last_client_id: StringProperty(name="Receiving Client ID", default="")
    rec_from_remote: BoolProperty(
        name="Record with iPhone REC", default=True,
        description="Start and stop this REC with the REC button in trkAR (iPhone). The start signal arrives when the 3-2-1 countdown on the iPhone ends")

    # --- 送出 (NDI) ---
    ndi_name: StringProperty(name="NDI Name", default="Blender (trkAR)")
    ndi_width: IntProperty(name="Width", default=1280, min=16, max=7680)
    ndi_height: IntProperty(name="Height", default=720, min=16, max=4320)
    ndi_fps: FloatProperty(name="FPS", default=30.0, min=1.0, max=60.0)
    ndi_transparent: BoolProperty(name="Transparent Background", default=False,
                                  description="Send with alpha without drawing the background (for overlaying on video in OBS)")

    # --- 画面の送出 (NDI) ---
    # trkAR 受信の ▶ (まとめて開始) で、実写・深度も始めるか (カメラ追従はいつも始める)
    receive_video: BoolProperty(name="Also Receive Video", default=True,
                                description="The ▶ in trkAR Receive also starts receiving live video (NDI) (when a source is selected)")
    receive_depth: BoolProperty(name="Also Receive Depth", default=False,
                                description="The ▶ in trkAR Receive also starts receiving depth")
    # NDI 送出のパネルで選ぶ送るもの (どれも 1 つずつしか動かさない)
    ndi_out_mode: EnumProperty(
        name="What to Send",
        items=(("CAMERA", "Camera View", "Render the tracking camera's view in this view's shading mode and send it (for the iPhone's MONI or OBS)"),
               ("SCREEN_OS", "Screen (OS Capture)",
                "Capture the Blender screen with macOS screen capture and send it. The scene fps rarely drops. Latency increases slightly because it goes through OS compositing. macOS 14 or later needs Screen Recording permission"),
               ("SCREEN_BLENDER", "Screen (Read in Blender)",
                "Read the Blender screen inside Blender and send it. Lower latency, but in heavy scenes it waits for the GPU, so the scene fps drops. Blender 5.3 or later")),
        default="CAMERA")
    screen_ndi_cursor: BoolProperty(name="Show Mouse Cursor", default=True,
                                    description="With Screen (OS Capture), also send the mouse cursor")
    screen_ndi_name: StringProperty(name="NDI Name", default="Blender Screen (trkAR)")
    screen_ndi_target: EnumProperty(
        name="Range",
        items=(("WINDOW", "Whole Window", "The whole Blender window (including menus and pop-ups)"),
               ("AREA", "This Area Only", "Only the area where Start Output was pressed (this 3D View)")),
        default="WINDOW")
    screen_ndi_fps: FloatProperty(name="FPS", default=30.0, min=1.0, max=60.0)
    screen_ndi_half: BoolProperty(name="Send at Half Size", default=False,
                                  description="Send at half width and height (when Retina or 4K screens are heavy)")


    # --- 受信 (NDI → シーン) ---
    ndi_in_camera_source: StringProperty(
        name="Source Before Matting", default="",
        description="The source received before RVM matting started (trkAR on the iPhone). If the matte video is selected while matting is not running, switch back to this when receiving starts")
    ndi_in_source: StringProperty(name="Source", default="",
                                  description="NDI source name to receive (e.g. IPHONE (trkAR))")
    ndi_in_lens_crop: BoolProperty(
        name="Crop to Lens", default=True,
        description="Size the video plate and camera background by the iPhone's actual field of view. When you zoom in with CTL's LENS or Blender's focal length, the video is cropped at the center just like the CG (zooming out shows nothing beyond the video). Off = fill the camera frame as before")
    ndi_in_front_distance: FloatProperty(
        name="Near Distance", default=1.0, min=0.01, soft_max=20.0, unit="LENGTH",
        description="How far in front of the camera to place the \"Place in Front of Camera\" plate. CG behind this distance is hidden by the plate (person)")
    ndi_in_front_follow: BoolProperty(
        name="Match Person Distance", default=False,
        description="Places the \"Place in Front of Camera\" plate at the person's distance measured from depth (LiDAR / ARKit estimated depth; the median depth inside the person mask). Which CG is in front of or behind the person then matches the real position. When no depth is received or the person is not seen for a while, it is placed at \"Near Distance\"")
    dof_autofocus: BoolProperty(
        name="Focus on Person Plate", default=False,
        description="Keeps the tracked camera's depth-of-field focus distance at the distance of the \"Place in Front of Camera\" plate (the person's distance with \"Match Person Distance\"). Blur strength (f-stop) and depth of field on/off stay as set in the iPhone controller's lens settings or the camera settings")
    dof_autofocus_smoothing: FloatProperty(
        name="Focus Follow", default=0.5, min=0.0, soft_max=3.0, subtype="TIME_ABSOLUTE", unit="TIME_ABSOLUTE",
        description="Approximate time (seconds) for the focus to catch up to the plate distance. Smooths out small jitter in the person's distance so the focus does not hunt back and forth. 0 matches the plate distance directly")
    matte_model: EnumProperty(
        name="RVM Model",
        items=(("720", "720 (Fast)", "About 17ms on Apple M4"), ("1080", "1080 (Detailed)", "About 25ms on Apple M4")),
        default="720",
    )
    # 公開版は中継を入れていない人が多いので既定はオフ (入れていないと受信のたびに警告が出る)
    receive_matte: BoolProperty(
        name="Start Matte on Receive", default=STUDIO,
        description="When receiving video with ▶ in trkAR Receive, also start the RVM matte and receive the matted video (trkAR Matte)")
    ndi_in_low: BoolProperty(name="Receive Low Resolution", default=False,
                             description="Receive the NDI preview stream (lighter, but lower resolution)")
    ndi_in_auto_screen: BoolProperty(
        name="Place Plate on Receive", default=True,
        description="When video receiving starts and nothing shows the received video (plate, screen, depth screen, camera background, or a material using the video), place a plate in front of the camera (same as \"Place in Front of Camera\")")


    # --- 受信 (LiDAR 深度 → 立体スクリーン) ---
    depth_transport: EnumProperty(
        name="Receive Method",
        items=(("UDP", "UDP (Uncompressed)", "Uncompressed depth that trkAR sends to port 9100 on the SystemPC IP. Clean"),
               ("NDI", "NDI", "trkAR-Depth over NDI. NDI compression tends to make it noisy")),
        default="UDP")
    depth_udp_port: IntProperty(name="UDP Port", default=depth_input.UDP_DEFAULT_PORT, min=1, max=65535)
    depth_source: StringProperty(name="Depth Source", default="",
                                 description="trkAR depth NDI source (e.g. IPHONE (trkAR-Depth))")
    depth_step: EnumProperty(
        name="Decimation",
        items=(("1", "1 (Finest)", "Every LiDAR pixel (256×144 vertices)"),
               ("2", "1/2", "1/2 horizontally and vertically"),
               ("4", "1/4", "1/4 horizontally and vertically (light)")),
        default="2",
    )
    depth_max_distance: FloatProperty(name="Max Distance", default=5.0, min=0.1, max=6.5, unit="LENGTH",
                                      description="Pixels farther than this become transparent (real-world m)")
    depth_confidence: EnumProperty(
        name="Confidence",
        items=(("0", "All", "Also use low confidence (noisy)"),
               ("1", "Medium and Above", "Confidence medium and above"),
               ("2", "High Only", "Confidence high only")),
        default="1",
    )
    depth_people: EnumProperty(
        name="Person",
        items=(("OFF", "Off", "Do not use the person mask (cut only at depth steps)"),
               ("EDGE", "Cut at Person Outline", "Show everything, and cut the boundary between person and background with the ARKit person mask"),
               ("ONLY", "Person Only", "Show only where ARKit detects a person")),
        default="EDGE",
        description="ARKit person segmentation (only when receiving over UDP)")
    depth_person_distance: FloatProperty(
        name="Person Distance", default=2.0, min=0.1, max=8.0, unit="LENGTH",
        description="On devices without depth (iPad mini etc.) that send only the person mask, the distance at which to place person pixels that have no ARKit estimated depth either (real-world m)")
    depth_flat_cutout: BoolProperty(
        name="Flat Cutout with Person Mask",
        description="Instead of the depth mesh (3D), use the ARKit person mask directly as the alpha of the live NDI video (trkAR NDI). Even on PCs too slow for RVM, you can cut out just the person on the flat planes of \"Bring NDI Video into Scene\" (Place Screen / Place in Front of Camera), ignoring depth (only while receiving over UDP; no depth screen needed)",
        default=False)
    depth_background: BoolProperty(
        name="Background Plate",
        description="Places a plate with the live video behind the person. It shows where there is no person (on devices without depth, everything except the person). Lets you put CG between the person and the background",
        default=False)
    depth_background_distance: FloatProperty(
        name="Background Distance", default=4.0, min=0.1, soft_max=20.0, unit="LENGTH",
        description="Distance from the camera at which to place the background plate (real-world; tracking scale applies)")
    take_folder: StringProperty(
        name="Save To", default="", subtype="DIR_PATH",
        description="Folder for takes (a subfolder is created per take). If empty, trkar_takes next to the .blend (~/Movies/trkAR Takes for an unsaved .blend)")
    take_path: StringProperty(name="Take", subtype="DIR_PATH",
                              description="Folder of the take to play, export, or generate (REC sets it to the latest). Choosing another during playback reloads that take",
                              update=_take_path_update)
    take_fps: EnumProperty(
        name="Recording FPS",
        items=(("24", "24 fps", "Matches video generation (LTX / H3 etc.). Live video (30fps) is resampled at 24fps"),
               ("30", "30 fps", "Same as the iPhone live video"),
               ("SCENE", "Keep Scene", "Do not change the output frame rate")),
        default="24")
    take_prep_long_side: IntProperty(
        name="Long Side", default=960, min=256, max=2048, step=16,
        description="Long side in pixels of the generation inputs (reference video). Aspect ratio follows the scene output (rounded to a multiple of 16)")
    take_prep_layout: BoolProperty(
        name="Location Layout", default=True,
        description="Also make 4 views (layout.png) looking front, back, left, and right from the person's position (takes a little time)")
    take_render_size: EnumProperty(
        name="Resolution",
        items=(("SCENE", "Scene Output", "Export at the scene output resolution"),
               ("LTX", "LTX (896×512)", "Long side 896, short side 512 (multiples of 128). 512×896 for a portrait scene"),
               ("H3", "H3 (960×544)", "MiniMax-H3 generation resolution (multiples of 32). 544×960 for a portrait scene")),
        default="SCENE")
    take_use_range: BoolProperty(
        name="Range", default=False, update=_take_range_update,
        description="Export or send only part of the take (start to end frame). During playback it is also set as the timeline in / out. Off for the whole take")
    take_range_start: IntProperty(name="Start", default=0, min=0, update=_take_range_update,
                                  description="First frame to export (timeline number = background CG time)")
    take_range_end: IntProperty(name="End", default=120, min=0, update=_take_range_update,
                                description="Last frame to export (inclusive)")
    take_time_offset: IntProperty(
        name="CG Time Offset", default=0, update=_take_offset_update,
        description="How many frames to shift the playing take relative to the background CG time (+ shifts later). Saved per take in take.json, and also applies to export, generation inputs, and sending cuts (clay input)")
    take_range_mode: EnumProperty(
        name="Range Mode", update=_take_range_update,
        items=(("END", "End Frame", "Set by start and end frames"),
               ("DURATION", "Duration (Seconds)", "Set by start frame and length (seconds). The end is set automatically from the scene fps")),
        default="END")
    take_range_seconds: FloatProperty(
        name="Duration", default=5.0, min=0.05, soft_max=60.0, step=50, precision=2, update=_take_range_update,
        description="Length to export from the start frame (seconds). Frames = seconds × fps (rounded)")
    take_render_samples: IntProperty(
        name="Samples", default=0, min=0, max=256,
        description=("EEVEE samples when exporting from the viewport. 0 keeps the scene setting (viewport samples). Lower is faster (4 to 8 makes heavy scenes several times faster)"))
    take_render_mode: EnumProperty(
        name="Export",
        items=(("VIEWPORT", "Viewport", "Export as the 3D view looks (shading). Fast"),
               ("RENDER", "Render", "Export with the scene render engine (EEVEE / Cycles)")),
        default="VIEWPORT")
    take_render_format: EnumProperty(
        name="Format",
        items=(("MP4", "MP4 (H.264)", "<take>/render.mp4 (backup copy as render_<.blend name>.mp4). Can go straight into video generation (Griptape / ComfyUI etc.). No alpha"),
               ("PNG", "PNG Sequence", "<take>/render/0001.png … (backup copy in render_<.blend name>/). With alpha (when the background is transparent)")),
        default="MP4")
    bridge_port: IntProperty(name="Port", default=bridge.DEFAULT_PORT, min=1024, max=65535,
                             description="Automation Port number (listens on 127.0.0.1 only)")
    depth_edge_shift: FloatProperty(name="Edge Position", default=0.0, min=-1.0, max=1.0,
                                    description="+ expands the outline outward / − trims it inward (a few pixels)",
                                    update=_update_depth_edge)
    depth_edge_feather: FloatProperty(name="Edge Feather", default=0.3, min=0.0, max=1.0,
                                      subtype="FACTOR", description="0 = sharp; higher makes the outline softer and more see-through",
                                      update=_update_depth_edge)
    edge_style: EnumProperty(
        name="Edge Style",
        items=(("SHARP", "Sharp", "Narrows the semi-transparent band to stop grainy dots scattering on the outline while receiving (the outline gets slightly jagged)"),
               ("SOFT", "Soft", "Draws semi-transparency as is (keeps see-through hair, but dots scatter on the outline while receiving)")),
        default="SHARP", update=_update_depth_edge)
    edge_sharp_width: FloatProperty(
        name="Edge Width", default=0.2, min=0.02, max=1.0, subtype="FACTOR", update=_update_depth_edge,
        description="Semi-transparent width kept in Sharp mode. Smaller gives no dots but jagged edges; larger is smoother but shows dots")
    depth_denoise: BoolProperty(name="Denoise", default=True,
                                description="Apply a 3×3 median to the depth (does not cross boundaries)")
    depth_temporal: FloatProperty(name="Temporal Smoothing", default=0.5, min=0.0, max=0.95,
                                  description="Blend with the previous frame to reduce flicker (areas with large motion are not blended)")
    depth_sync: BoolProperty(name="Sync to Video", default=True,
                             description="Output the depth captured at the same time as the live video (NDI receive). Depth arrives earlier, so without this the outline moves ahead of the video")
    depth_sync_offset: FloatProperty(name="Sync Offset", default=0.0, min=-300.0, max=300.0, subtype="NONE",
                                     description="+ delays the depth further / − makes it earlier (ms). If the outline still moves ahead after syncing, use +")
    depth_edge_tolerance: FloatProperty(name="Step Tolerance", default=0.1, min=0.0, soft_max=1.0,
                                        subtype="FACTOR",
                                        description="Make faces transparent when the distances of their 4 corners differ by this ratio or more (no membrane between near and far)")


class TRKAR_OT_tracking_start(bpy.types.Operator):
    bl_idname = "trkar.tracking_start"
    bl_label = "Start Receiving"
    bl_description = "Receive OSC from trkAR and move the camera"

    def execute(self, context):
        settings = context.scene.trkar
        camera = settings.camera or context.scene.camera
        if camera is None:
            self.report({"ERROR"}, _t("Specify a camera, or add a camera to the scene"))
            return {"CANCELLED"}
        # オイラーだと分解時に ±180° で跳ねてキーフレーム補間が暴れるため
        camera.rotation_mode = "QUATERNION"
        try:
            tracking.start(settings.port, camera)
        except OSError as exc:
            self.report({"ERROR"}, _t("Cannot open port %d: %s") % (settings.port, exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class TRKAR_OT_tracking_stop(bpy.types.Operator):
    bl_idname = "trkar.tracking_stop"
    bl_label = "Stop Receiving"

    def execute(self, _context):
        tracking.stop()
        return {"FINISHED"}


class TRKAR_OT_copy_ip(bpy.types.Operator):
    bl_idname = "trkar.copy_ip"
    bl_label = "Copy IP"
    bl_description = "Copy this IP to the clipboard (enter it as SystemPC IP on the iPhone)"

    ip: StringProperty()

    def execute(self, context):
        context.window_manager.clipboard = self.ip
        self.report({"INFO"}, _t("Copied %s") % self.ip)
        return {"FINISHED"}


class TRKAR_OT_reanchor(bpy.types.Operator):
    bl_idname = "trkar.reanchor"
    bl_label = "Reset Reference Here"
    bl_description = "Take the current camera position and iPhone pose as the new reference (e.g. after re-gripping the iPhone)"

    def execute(self, context):
        settings = context.scene.trkar
        tracking.reanchor(settings.camera or context.scene.camera)
        return {"FINISHED"}


class TRKAR_OT_align_to_cursor(bpy.types.Operator):
    bl_idname = "trkar.align_to_cursor"
    bl_label = "Current Position to 3D Cursor"
    bl_description = "Set the location offset so the iPhone's current horizontal position is at the 3D cursor (height is kept)"

    def execute(self, context):
        raw = tracking.last_raw_location()
        if raw is None:
            self.report({"ERROR"}, _t("No pose received yet"))
            return {"CANCELLED"}
        settings = context.scene.trkar
        yaw = Matrix.Rotation(math.radians(settings.yaw_offset), 3, "Z")
        moved = yaw @ (raw * settings.scale)
        cursor = context.scene.cursor.location
        offset = Vector(settings.location_offset)
        offset.x = cursor.x - moved.x
        offset.y = cursor.y - moved.y
        settings.location_offset = offset
        return {"FINISHED"}


class TRKAR_OT_ctl_reset(bpy.types.Operator):
    bl_idname = "trkar.ctl_reset"
    bl_label = "Reset Offset"
    bl_description = "Reset the Blender-side offset to 0"
    bl_options = {"REGISTER", "UNDO"}

    target: EnumProperty(items=(("ROTATION", "Rotation", ""), ("ALL", "All", "")), default="ALL")

    def execute(self, context):
        settings = context.scene.trkar
        settings.ctl_pan = settings.ctl_tilt = settings.ctl_roll = 0.0
        if self.target == "ALL":
            settings.ctl_location = (0.0, 0.0, 0.0)
            settings.ctl_lens = settings.ctl_fstop = settings.ctl_focus = 0.0
            tracking.reset_pan_shift(settings)
        return {"FINISHED"}


class TRKAR_OT_ctl_keyframe(bpy.types.Operator):
    bl_idname = "trkar.ctl_keyframe"
    bl_label = "Insert Keyframe"
    bl_description = "Insert a keyframe for the Blender-side offset at the current frame"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        for name in ("ctl_location", "ctl_pan", "ctl_tilt", "ctl_roll", "ctl_lens", "ctl_fstop", "ctl_focus"):
            scene.keyframe_insert('trkar.%s' % name, frame=scene.frame_current)
        return {"FINISHED"}


class TRKAR_OT_ndi_swap_size(bpy.types.Operator):
    bl_idname = "trkar.ndi_swap_size"
    bl_label = "Swap Width/Height"
    bl_description = "Swap the width and height of the output resolution (to match trkAR's 9:16 portrait)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.trkar
        settings.ndi_width, settings.ndi_height = settings.ndi_height, settings.ndi_width
        return {"FINISHED"}


class TRKAR_OT_ndi_start(bpy.types.Operator):
    bl_idname = "trkar.ndi_start"
    bl_label = "Start NDI Output"
    bl_description = "Render the camera view with this view's shading settings and output it over NDI"

    @classmethod
    def poll(cls, context):
        return _view3d(context)[1] is not None

    def execute(self, context):
        settings = context.scene.trkar
        try:
            ndi_output.start(
                _view3d(context)[1],
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


class TRKAR_OT_ndi_stop(bpy.types.Operator):
    bl_idname = "trkar.ndi_stop"
    bl_label = "Stop NDI Output"

    def execute(self, _context):
        ndi_output.stop()
        return {"FINISHED"}


class TRKAR_OT_screen_ndi_start(bpy.types.Operator):
    bl_idname = "trkar.screen_ndi_start"
    bl_label = "Start Screen NDI Output"
    bl_description = "Output what is shown on the Blender screen (the whole window or this area) over NDI as is"

    @classmethod
    def poll(cls, context):
        if not screen_output.supported():
            cls.poll_message_set(_t("Requires Blender 5.3 or later"))
            return False
        return context.window is not None

    def execute(self, context):
        settings = context.scene.trkar
        area = _view3d(context)[1] if settings.screen_ndi_target == "AREA" else None
        if settings.screen_ndi_target == "AREA" and area is None:
            self.report({"ERROR"}, _t("No area to output"))
            return {"CANCELLED"}
        try:
            screen_out.start(
                context.window,
                area,
                settings.screen_ndi_name,
                settings.screen_ndi_fps,
                settings.screen_ndi_half,
                _library_override(context),
            )
        except Exception as exc:
            screen_out.stop()
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class TRKAR_OT_os_screen_ndi_start(bpy.types.Operator):
    bl_idname = "trkar.os_screen_ndi_start"
    bl_label = "Start Screen NDI Output (OS Capture)"
    bl_description = "Capture the Blender screen (the whole window or this area) with macOS screen capture and output it over NDI"

    @classmethod
    def poll(cls, context):
        if not os_screen.supported():
            cls.poll_message_set(_t("Available on macOS 14 or later"))
            return False
        return context.window is not None

    def execute(self, context):
        settings = context.scene.trkar
        area = _view3d(context)[1] if settings.screen_ndi_target == "AREA" else None
        if settings.screen_ndi_target == "AREA" and area is None:
            self.report({"ERROR"}, _t("No area to output"))
            return {"CANCELLED"}
        try:
            os_screen_out.start(context.window, area, settings.screen_ndi_name, settings.screen_ndi_fps,
                                settings.screen_ndi_half, settings.screen_ndi_cursor)
        except Exception as exc:
            os_screen_out.stop()
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class TRKAR_OT_receive_start(bpy.types.Operator):
    bl_idname = "trkar.receive_start"
    bl_label = "Start trkAR Receiving"
    bl_description = "Start Camera Tracking (OSC) together with receiving the checked live video (NDI) and depth"

    def execute(self, context):
        settings = context.scene.trkar
        video = bpy.ops.trkar.ndi_in_start
        if settings.receive_matte and not matte_relay.running:
            video = lambda: _start_video_with_matte(self, context)
        steps = [(not tracking.server.running, bpy.ops.trkar.tracking_start)]
        steps.append((settings.receive_video and settings.ndi_in_source and not ndi_in.running, video))
        steps.append((settings.receive_depth and not depth_in.running, bpy.ops.trkar.depth_start))
        for wanted, operator in steps:
            if wanted:
                try:
                    operator()
                except RuntimeError as exc:  # 1 つ失敗しても残りは始める (理由は Info に出る)
                    self.report({"WARNING"}, str(exc))
        return {"FINISHED"}


def _start_video_with_matte(operator, context):
    """実写を RVM の切り抜き付きで受け始める。切り抜きの映像が選ばれていて切り抜きが動いていなければ、元のソースに
    戻してから切り抜く。別に動いている中継 (見えている trkAR Matte) が選ばれていれば、それをそのまま受ける"""
    source = _resolve_video_source(operator, context)
    if source is None:
        return
    if source.endswith(MATTE_SOURCE_SUFFIX):
        bpy.ops.trkar.ndi_in_start()
        return
    try:
        bpy.ops.trkar.matte_start()
    except RuntimeError as exc:
        # 切り抜きを始められなくても (中継のフォルダが無いなど)、実写はそのまま受ける
        operator.report({"WARNING"}, _t("Cannot start matting, so receiving without matte: %s") % exc)
        bpy.ops.trkar.ndi_in_start()


class TRKAR_OT_receive_stop(bpy.types.Operator):
    bl_idname = "trkar.receive_stop"
    bl_label = "Stop trkAR Receiving"
    bl_description = "Stop receiving Camera Tracking, live video, depth, and matte all at once"

    def execute(self, context):
        if matte_relay.running or bpy.app.timers.is_registered(_switch_to_matte):
            _stop_matte(context)  # 中継を止め、ソースを切り抜く前に戻す
        for running, operator in ((depth_in.running, bpy.ops.trkar.depth_stop),
                                  (ndi_in.running, bpy.ops.trkar.ndi_in_stop),
                                  (tracking.server.running, bpy.ops.trkar.tracking_stop)):
            if running:
                operator()
        return {"FINISHED"}


class TRKAR_OT_ndi_out_start(bpy.types.Operator):
    bl_idname = "trkar.ndi_out_start"
    bl_label = "Start NDI Output"
    bl_description = "Output what is selected in the NDI Output panel (camera view / screen)"

    def execute(self, context):
        operator = {"CAMERA": bpy.ops.trkar.ndi_start, "SCREEN_OS": bpy.ops.trkar.os_screen_ndi_start,
                    "SCREEN_BLENDER": bpy.ops.trkar.screen_ndi_start}[context.scene.trkar.ndi_out_mode]
        if not operator.poll():
            self.report({"ERROR"}, _t("Cannot start from here (press it from the 3D View sidebar, and check the supported version)"))
            return {"CANCELLED"}
        return operator()


class TRKAR_OT_ndi_out_stop(bpy.types.Operator):
    bl_idname = "trkar.ndi_out_stop"
    bl_label = "Stop NDI Output"

    def execute(self, _context):
        ndi_output.stop()
        screen_out.stop()
        os_screen_out.stop()
        return {"FINISHED"}


def _ndi_out_running():
    return ndi_output.running or screen_out.running or os_screen_out.running


class TRKAR_OT_screen_ndi_stop(bpy.types.Operator):
    bl_idname = "trkar.screen_ndi_stop"
    bl_label = "Stop Screen NDI Output"

    def execute(self, _context):
        screen_out.stop()
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
        layout.label(text=_t("Not found (wait a few seconds and reopen)"), icon="INFO")
    for name in names:
        op = layout.operator("trkar.ndi_in_set_source", text=name)
        op.name = name
        op.target = target


class TRKAR_MT_ndi_sources(bpy.types.Menu):
    bl_idname = "TRKAR_MT_ndi_sources"
    bl_label = "NDI Source"

    def draw(self, context):
        _draw_sources(self.layout, context, "ndi_in_source")


class TRKAR_MT_depth_sources(bpy.types.Menu):
    bl_idname = "TRKAR_MT_depth_sources"
    bl_label = "Depth NDI Source"

    def draw(self, context):
        _draw_sources(self.layout, context, "depth_source")


class TRKAR_OT_ndi_in_set_source(bpy.types.Operator):
    bl_idname = "trkar.ndi_in_set_source"
    bl_label = "Select NDI Source"
    bl_options = {"INTERNAL"}

    name: StringProperty()
    target: StringProperty(default="ndi_in_source")

    def execute(self, context):
        if self.target in {"ndi_in_source", "depth_source"}:
            setattr(context.scene.trkar, self.target, self.name)
        return {"FINISHED"}


# RVM の切り抜きの中継 (tools/matte の trkar_matte.py) が送り出す NDI の名前の終わり
MATTE_SOURCE_SUFFIX = "(trkAR Matte)"


def _matte_running():
    return matte_relay.running


def _resolve_video_source(operator, context):
    """受けるソース。切り抜きの映像 (trkAR Matte) が選ばれているのに切り抜きが動いておらず見えてもいないときは、
    切り抜く前のソースに戻す (切り抜き中に保存した / Blender を閉じたあと、無いソースを待ち続けないように)。
    戻す先が分からなければ None (理由を出す)"""
    settings = context.scene.trkar
    source = settings.ndi_in_source
    if not source.endswith(MATTE_SOURCE_SUFFIX) or _matte_running():
        return source
    try:
        visible = ndi_in.sources(_library_override(context))
    except Exception:  # noqa: BLE001  NDI が読めないときは下の start で理由が出る
        visible = []
    if source in visible:
        return source  # 別に動いている中継 (ターミナルの trkar_matte.py など)
    fallback = settings.ndi_in_camera_source
    if not fallback:
        cameras = [n for n in visible if n.endswith(("(trkAR)", "(ARtrk)"))]
        fallback = cameras[0] if len(cameras) == 1 else ""
    if not fallback:
        operator.report({"ERROR"}, _t("The matte video %s is selected, but matting is not running. Select the original iPhone source, or use Matte with RVM") % source)
        return None
    settings.ndi_in_source = fallback
    operator.report({"INFO"}, _t("Matting is not running, so receiving the original source %s") % fallback)
    return fallback


class TRKAR_OT_ndi_in_start(bpy.types.Operator):
    bl_idname = "trkar.ndi_in_start"
    bl_label = "Start NDI Receiving"
    bl_description = "Stream the NDI video into the image \"%s\"" % ndi_input.IMAGE_NAME

    def execute(self, context):
        settings = context.scene.trkar
        if not settings.ndi_in_source:
            self.report({"ERROR"}, _t("Select a source"))
            return {"CANCELLED"}
        source = _resolve_video_source(self, context)
        if source is None:
            return {"CANCELLED"}
        if player.active:
            player.stop(context.scene)
        try:
            ndi_in.start(source, settings.ndi_in_low, _library_override(context))
        except Exception as exc:
            ndi_in.stop()
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        _ensure_video_shown(self, context)
        return {"FINISHED"}


def _video_shown(context):
    """受けた映像 (画像 trkAR NDI) を映しているものがシーンにあるか: 板・スクリーン・深度スクリーンなど、
    その画像を使うマテリアルが使われているか、カメラの背景に出しているか"""
    image = bpy.data.images.get(ndi_input.IMAGE_NAME)
    if image is None:
        return False
    for material in bpy.data.materials:
        if material.users and material.node_tree is not None and any(
                node.type == "TEX_IMAGE" and node.image == image for node in material.node_tree.nodes):
            return True
    camera = context.scene.trkar.camera or context.scene.camera
    if camera is not None and camera.type == "CAMERA":
        return any(bg.image == image for bg in camera.data.background_images)
    return False


def _ensure_video_shown(operator, context):
    """実写を受け始めたのに映すものが何も無ければ、カメラの前に板を置く (初めてのときに「映らない」とならないように)"""
    settings = context.scene.trkar
    depth_input.update_sharpen(settings)  # 前の版で作った板にも輪郭の描き方を入れる
    if not settings.ndi_in_auto_screen or _video_shown(context):
        return
    camera = settings.camera or context.scene.camera
    if camera is None or camera.type != "CAMERA":
        return
    try:
        bpy.ops.trkar.ndi_in_front()
    except RuntimeError as exc:
        operator.report({"WARNING"}, _t("Cannot place the video plate: %s") % exc)
        return
    operator.report({"INFO"}, _t("Placed the video plate in front of the camera (change it under Plate & Background)"))


class TRKAR_OT_ndi_in_stop(bpy.types.Operator):
    bl_idname = "trkar.ndi_in_stop"
    bl_label = "Stop NDI Receiving"

    def execute(self, _context):
        ndi_in.stop()
        return {"FINISHED"}


def _matte_folder(context):
    addon = context.preferences.addons.get(__package__)
    configured = bpy.path.abspath(addon.preferences.matte_folder) if addon else ""
    return matte.find_folder(configured)


def _find_matte_source(context):
    try:
        names = ndi_in.sources(_library_override(context))
    except Exception:
        return None
    return next((n for n in names if n.endswith("(%s)" % matte.OUTPUT_NAME)), None)


def _switch_to_matte():
    """中継の NDI が見えたら、Blender の NDI 受信をそちらに切り替える (モデルの読み込みに数秒かかる)"""
    if not matte_relay.running:
        return None
    context = bpy.context
    name = _find_matte_source(context)
    if name is None:
        return 0.5
    settings = context.scene.trkar
    settings.ndi_in_source = name
    try:
        ndi_in.start(name, settings.ndi_in_low, _library_override(context))
    except Exception as exc:
        matte_relay.last_error = str(exc)
    return None


class TRKAR_OT_matte_start(bpy.types.Operator):
    bl_idname = "trkar.matte_start"
    bl_label = "Matte with RVM"
    bl_description = ("Matte the selected NDI source with RVM (Robust Video Matting, AI person matting) on this PC, then switch to receiving \"%s\" with alpha (latency about +50ms)") % getattr(matte, "OUTPUT_NAME", "")

    def execute(self, context):
        settings = context.scene.trkar
        source = settings.ndi_in_source
        if not source:
            self.report({"ERROR"}, _t("Select an NDI source (trkAR on the iPhone) first"))
            return {"CANCELLED"}
        if source.endswith("(%s)" % matte.OUTPUT_NAME):
            self.report({"ERROR"}, _t("A matted video is selected. Select the original iPhone source"))
            return {"CANCELLED"}
        settings.ndi_in_camera_source = source  # 切り抜きが動いていないのに切り抜きの映像が残ったら、ここへ戻す
        just_stopped = time.monotonic() - matte_relay.stopped_at < 10.0
        existing = None if just_stopped else _find_matte_source(context)
        if existing is not None:
            # ターミナルなどで trkar_matte.py がもう動いている (同じ名前では送り出せない)
            ndi_in.stop()
            settings.ndi_in_source = existing
            try:
                ndi_in.start(existing, settings.ndi_in_low, _library_override(context))
            except Exception as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            self.report({"WARNING"}, _t("Receiving from a relay already running separately (%s)") % existing)
            return {"FINISHED"}
        folder = _matte_folder(context)
        if folder is None:
            self.report({"ERROR"}, _t("trkar_matte.py not found. Set \"RVM Relay Folder\" in the add-on preferences to tools/matte"))
            return {"CANCELLED"}
        matte_relay.previous_source = source
        matte_relay.previous_running = ndi_in.running
        try:
            matte_relay.start(folder, source, settings.matte_model)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        ndi_in.stop()
        if bpy.app.timers.is_registered(_switch_to_matte):
            bpy.app.timers.unregister(_switch_to_matte)
        bpy.app.timers.register(_switch_to_matte, first_interval=1.0)
        _ensure_video_shown(self, context)
        return {"FINISHED"}


class TRKAR_OT_matte_stop(bpy.types.Operator):
    bl_idname = "trkar.matte_stop"
    bl_label = "Stop Matting"
    bl_description = "Stop the RVM relay and switch NDI receiving back to the original source"

    def execute(self, context):
        _stop_matte(context)
        return {"FINISHED"}


def _stop_matte(context):
    if bpy.app.timers.is_registered(_switch_to_matte):
        bpy.app.timers.unregister(_switch_to_matte)
    matte_relay.stop()
    matte_relay.last_error = ""
    settings = context.scene.trkar
    previous = matte_relay.previous_source or settings.ndi_in_camera_source
    if previous and settings.ndi_in_source.endswith("(%s)" % matte.OUTPUT_NAME):
        ndi_in.stop()
        settings.ndi_in_source = previous
        if matte_relay.previous_running:
            try:
                ndi_in.start(settings.ndi_in_source, settings.ndi_in_low, _library_override(context))
            except Exception:
                ndi_in.stop()
    matte_relay.previous_source = ""


def _image_aspect(image):
    width, height = ndi_in.resolution
    if width and height:
        return width / height
    if image.size[0] > 16 and image.size[1] > 16:
        return image.size[0] / image.size[1]
    return 16.0 / 9.0


def _video_material(name, image):
    """受信映像を貼った発光マテリアル。アルファ付きの映像 (RVM の "trkAR Matte" など) は透ける。
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
    depth_input.apply_sharpen(material, bpy.context.scene.trkar)
    return material


class TRKAR_OT_ndi_in_screen(bpy.types.Operator):
    bl_idname = "trkar.ndi_in_screen"
    bl_label = "Place Screen"
    bl_description = "Place a plate (emission material) showing the received video at the 3D cursor. Video with alpha is see-through"
    bl_options = {"REGISTER", "UNDO"}

    height: FloatProperty(name="Height", default=1.0, min=0.01, unit="LENGTH")

    def execute(self, context):
        image = ndi_input.ensure_image()
        half_h = self.height / 2.0
        half_w = half_h * _image_aspect(image)
        # XZ 平面に立てて -Y 向き (正面図から見える向き)
        mesh = bpy.data.meshes.new("trkAR NDI Screen")
        mesh.from_pydata(
            [(-half_w, 0.0, -half_h), (half_w, 0.0, -half_h), (half_w, 0.0, half_h), (-half_w, 0.0, half_h)],
            [],
            [(0, 1, 2, 3)],
        )
        uv = mesh.uv_layers.new(name="UVMap")
        for loop, coord in zip(uv.data, ((0, 0), (1, 0), (1, 1), (0, 1))):
            loop.uv = coord

        material = _video_material("trkAR NDI Screen", image)
        mesh.materials.append(material)

        obj = bpy.data.objects.new("trkAR NDI Screen", mesh)
        obj.location = context.scene.cursor.location
        if hasattr(obj, "visible_shadow"):
            obj.visible_shadow = False
        context.collection.objects.link(obj)
        for other in context.selected_objects:
            other.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        return {"FINISHED"}


CAMERA_SCREEN_NAME = "trkAR Camera Screen"


def _source_hfov(scene):
    """iPhone の実際の水平画角 (度)。ライブは届いた姿勢の値、テイクの再生中は録ったときの値。分からなければ 0"""
    if not scene.trkar.ndi_in_lens_crop:
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
    camera = scene.trkar.camera or scene.camera
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


# 実写の板を人の距離に置くとき: 測った距離をならす割合 (1 回の深度ごと) と、人の距離が測れなくなってから
# `手前の距離` に戻すまでの秒数 (人が一瞬見えなくなっても板が跳ばないように、その間は直前の距離のまま)
FRONT_FOLLOW_SMOOTHING = 0.3
FRONT_FOLLOW_HOLD = 2.0
_front = {"distance": None, "at": 0.0}


def _front_distance(scene):
    """`カメラの前に置く` の板の距離 (Blender の長さ) と、人の距離に合わせているか。
    テイクの再生・書き出し中は、録ったときの人の距離 (深度のコマに残したもの) に置く。人の距離が残っていない
    前のテイクは、カメラのピントにキーがあればそのピント (録ったときは人の板の距離に合わせていた) に置く"""
    settings = scene.trkar
    if settings.ndi_in_front_follow and player.active:
        if depth_in.recorded_person_depth is not None:
            return depth_in.recorded_person_depth * settings.scale, True
        camera = settings.camera or scene.camera
        if camera is not None and camera.type == "CAMERA" and _focus_keyed(camera.data):
            return camera.data.dof.focus_distance, True
        return settings.ndi_in_front_distance, False
    if settings.ndi_in_front_follow and depth_in.running and depth_in.person_depth is not None \
            and time.monotonic() - depth_in.person_depth_at < FRONT_FOLLOW_HOLD:
        measured = depth_in.person_depth * settings.scale
        if _front["distance"] is None:
            _front["distance"] = measured
        elif depth_in.person_depth_at != _front["at"]:
            _front["distance"] += (measured - _front["distance"]) * FRONT_FOLLOW_SMOOTHING
        _front["at"] = depth_in.person_depth_at
        return _front["distance"], True
    _front["distance"] = None
    return settings.ndi_in_front_distance, False


def _focus_keyed(cam):
    animation = cam.animation_data
    action = animation.action if animation is not None else None
    if action is None:
        return False
    try:
        return any(c.data_path == "dof.focus_distance" for c in action.fcurves)
    except AttributeError:  # Blender 5 の層のあるアクション
        return any(c.data_path == "dof.focus_distance"
                   for layer in action.layers for strip in layer.strips
                   for bag in strip.channelbags for c in bag.fcurves)


_focus = {"distance": None, "at": 0.0}


def _autofocus_distance():
    """tracking.focus_hook。オートフォーカスが入っていれば、実写の板の距離 (Blender の長さ) を
    `ピントの追従` の秒数でならしたもの (時定数の指数移動平均。呼ばれる間隔によらず同じ速さで寄る)。"""
    scene = bpy.context.scene
    if scene is None or not scene.trkar.dof_autofocus:
        _focus["distance"] = None
        return None
    target = _front_distance(scene)[0]
    now = time.monotonic()
    tau = scene.trkar.dof_autofocus_smoothing
    if _focus["distance"] is None and tau > 0.0:
        # オートフォーカスを入れ直したときは、今のピント (手で合わせていた所) から寄っていく
        camera = scene.trkar.camera or scene.camera
        if camera is not None and camera.type == "CAMERA":
            _focus["distance"], _focus["at"] = camera.data.dof.focus_distance, now
    if _focus["distance"] is None or tau <= 0.0:
        _focus["distance"] = target
    else:
        _focus["distance"] += (target - _focus["distance"]) * (1.0 - math.exp(-(now - _focus["at"]) / tau))
    _focus["at"] = now
    return _focus["distance"]


def _hide_depth_screen(scene):
    """`人の距離に合わせる` で深度を受けている間は、深度メッシュ (trkAR Depth Screen) を隠す。
    同じ実写を貼った板と人の所がほぼ同じ奥行きに重なり、どちらが手前かがコマごとに入れ替わってちらつくため
    (深度は人の距離を測るのにだけ使う)。止めたら、隠す前の表示に戻す"""
    obj = bpy.data.objects.get(depth_input.OBJECT_NAME)
    if obj is None:
        return
    hide = scene.trkar.ndi_in_front_follow and (depth_in.running or player.active)
    saved = obj.get("trkar_follow_hidden")
    try:
        if hide and saved is None:
            obj["trkar_follow_hidden"] = [int(obj.hide_get()), int(obj.hide_render)]
            obj.hide_set(True)
            obj.hide_render = True
        elif not hide and saved is not None:
            obj.hide_set(bool(saved[0]))
            obj.hide_render = bool(saved[1])
            del obj["trkar_follow_hidden"]
    except RuntimeError:  # 今のビューレイヤーに無いなど
        pass


def _update_camera_screen():
    """画角 (iPhone の FOV / レンズ)・解像度・映像の縦横比が変わったら板の大きさを合わせ直す。"""
    scene = bpy.context.scene
    if scene is not None:
        _update_camera_background(scene)
        _hide_depth_screen(scene)
    obj = bpy.data.objects.get(CAMERA_SCREEN_NAME)
    if obj is None or scene is None or obj.type != "MESH" or obj.mode == "EDIT":
        return 1.0 / 30.0
    camera = obj.parent
    if camera is None or camera.type != "CAMERA" or len(obj.data.vertices) != 4:
        return 1.0 / 30.0
    data = camera.data
    render = scene.render
    distance = max(_front_distance(scene)[0], data.clip_start * 1.01)
    aspect = _image_aspect(ndi_input.ensure_image())
    hfov = _source_hfov(scene) if data.type == "PERSP" else 0.0
    key = (data.lens, data.sensor_width, data.sensor_height, data.sensor_fit, data.shift_x, data.shift_y,
           render.resolution_x, render.resolution_y, render.pixel_aspect_x, render.pixel_aspect_y,
           round(aspect, 4), round(distance, 5), data.type, data.ortho_scale, round(hfov, 2))
    if obj.get("trkar_key") != str(key):
        corners = _camera_screen_corners(scene, camera, distance, aspect, hfov)
        obj.data.vertices.foreach_set("co", [c for corner in corners for c in corner])
        obj.data.update()
        obj["trkar_key"] = str(key)
    return 1.0 / 30.0


class TRKAR_OT_ndi_in_front(bpy.types.Operator):
    bl_idname = "trkar.ndi_in_front"
    bl_label = "Place in Front of Camera"
    bl_description = ("Parent a plate showing the received video to the tracked camera, filling the frame at `Near Distance`. With video that has alpha (RVM), only the person appears in front of the CG")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.trkar
        camera = settings.camera or context.scene.camera
        if camera is None or camera.type != "CAMERA":
            self.report({"ERROR"}, _t("No camera"))
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
        if "trkar_key" in obj:
            del obj["trkar_key"]
        _update_camera_screen()
        _use_material_view(self, context)
        return {"FINISHED"}


class TRKAR_OT_ndi_in_camera_bg(bpy.types.Operator):
    bl_idname = "trkar.ndi_in_camera_bg"
    bl_label = "Show as Camera Background"
    bl_description = "Set the received video as the tracked camera's background image (visible only in the 3D View camera view)"

    def execute(self, context):
        settings = context.scene.trkar
        camera = settings.camera or context.scene.camera
        if camera is None or camera.type != "CAMERA":
            self.report({"ERROR"}, _t("No camera"))
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


def _view3d(context):
    """操作の対象の 3D ビュー (window, area)。押したのが 3D ビューならそれ。プロパティエディターなどから押したときは、
    このウィンドウの 3D ビューのうちカメラ視点のもの、無ければいちばん大きいもの。3D ビューが無ければ area は None"""
    window, area = context.window, context.area
    if area is not None and area.type == "VIEW_3D":
        return window, area
    if window is None:
        return None, None
    views = [a for a in window.screen.areas if a.type == "VIEW_3D"]
    if not views:
        return window, None
    camera_view = lambda a: a.spaces.active.region_3d is not None and a.spaces.active.region_3d.view_perspective == "CAMERA"
    return window, max(views, key=lambda a: (camera_view(a), a.width * a.height))


def _needs_material_view(context):
    area = _view3d(context)[1]
    space = area.spaces.active if area is not None else None
    return space is not None and space.shading.type in {"SOLID", "WIREFRAME"}


def _use_material_view(operator, context):
    """ソリッド表示だと実写の色も透明な縁も出ないので、このビューをマテリアルプレビューにする。"""
    if _needs_material_view(context):
        _view3d(context)[1].spaces.active.shading.type = "MATERIAL"
        operator.report({"INFO"}, _t("Switched this view to Material Preview to show the live video colors"))


class TRKAR_OT_view_material(bpy.types.Operator):
    bl_idname = "trkar.view_material"
    bl_label = "Switch to Material Preview"
    bl_description = "Switch this view to Material Preview (in Solid shading the screen shows no live video colors)"

    def execute(self, context):
        _use_material_view(self, context)
        return {"FINISHED"}


class TRKAR_OT_depth_start(bpy.types.Operator):
    bl_idname = "trkar.depth_start"
    bl_label = "Start Depth Receiving"
    bl_description = "Receive LiDAR depth from trkAR and update the shape of \"%s\" every frame" % depth_input.OBJECT_NAME

    def execute(self, context):
        settings = context.scene.trkar
        if settings.depth_transport == "NDI" and not settings.depth_source:
            self.report({"ERROR"}, _t("Select a depth source"))
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
            bpy.ops.trkar.depth_screen()
        _use_material_view(self, context)
        return {"FINISHED"}


class TRKAR_OT_depth_stop(bpy.types.Operator):
    bl_idname = "trkar.depth_stop"
    bl_label = "Stop Depth Receiving"

    def execute(self, context):
        depth_in.stop()
        depth_in.idle_update(context.scene)
        return {"FINISHED"}


class TRKAR_OT_depth_screen(bpy.types.Operator):
    bl_idname = "trkar.depth_screen"
    bl_label = "Create Depth Screen"
    bl_description = "Create a screen (with live video texture) that becomes 3D from depth, as a child of the tracked camera"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.trkar
        camera = settings.camera or context.scene.camera
        if camera is None:
            self.report({"ERROR"}, _t("No camera"))
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


class TRKAR_OT_depth_freeze(bpy.types.Operator):
    bl_idname = "trkar.depth_freeze"
    bl_label = "Freeze Current Shape"
    bl_description = "Keep the current Depth Screen as a separate object, with its current location and video"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        source = bpy.data.objects.get(depth_input.OBJECT_NAME)
        if source is None or len(source.data.vertices) == 0:
            self.report({"ERROR"}, _t("No Depth Screen yet"))
            return {"CANCELLED"}
        world = source.matrix_world.copy()
        obj = source.copy()
        obj.data = source.data.copy()
        obj.name = obj.data.name = "trkAR Depth Snapshot"
        obj.parent = None
        obj.matrix_world = world
        # 映像も今のフレームで止める
        material = source.data.materials[0].copy() if source.data.materials else None
        if material is not None:
            image = ndi_input.ensure_image().copy()
            image.name = "trkAR Depth Snapshot"
            image.pack()
            mask_node = material.node_tree.nodes.get(depth_input.MASK_NODE)
            if mask_node is not None and mask_node.image is not None:
                mask = mask_node.image.copy()
                mask.name = "trkAR Depth Snapshot Mask"
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
    path = bpy.path.abspath(context.scene.trkar.take_path).rstrip("/\\")
    return path if path and os.path.isfile(os.path.join(path, "take.json")) else None


class TRKAR_OT_take_rec_start(bpy.types.Operator):
    bl_idname = "trkar.take_rec_start"
    bl_label = "REC"
    bl_description = ("Record the camera motion (keyframes), live video, and Depth Screen while playing the timeline. Press again or stop playback to finish")

    def execute(self, context):
        if player.active:
            _play_to_live(context)
        folder = take.takes_folder(context.scene.trkar.take_folder)
        render = context.scene.render
        if context.scene.trkar.take_fps != "SCENE":
            render.fps = int(context.scene.trkar.take_fps)
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
        context.scene.trkar.take_path = path
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
    settings = context.scene.trkar
    camera = settings.camera or context.scene.camera
    rig = tracking._state.get("rig") if settings.use_rig else None
    return {
        "mode": settings.mode,
        "scale": settings.scale,
        "floor": {"from_iphone": settings.floor_from_iphone, "z": settings.floor_z},
        "blender": {"location": [round(v, 4) for v in settings.ctl_location], "pan_deg": settings.ctl_pan,
                    "tilt_deg": settings.ctl_tilt, "roll_deg": settings.ctl_roll, "lens_mm": settings.ctl_lens,
                    "pan_shift": [round(v, 4) for v in settings.ctl_pan_shift],
                    "fstop": settings.ctl_fstop, "focus": settings.ctl_focus},
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
    row.operator("trkar.slate_unset", text="", icon="X")
    col = layout.column(align=True)
    col.prop(prefs, "shot_site")
    col.prop(prefs, "shot_host")
    col = layout.column(align=True)
    col.prop(prefs, "shot_creator")
    col.prop(prefs, "shot_operator")


class TRKAR_OT_slate_unset(bpy.types.Operator):
    bl_idname = "trkar.slate_unset"
    bl_label = "Reset Slate to Unset"
    bl_description = "Set Scene / Cut / Take to 0 (in provenance, S00C00T00 = a cut with no slate set)"

    def execute(self, context):
        prefs = _prefs(context)
        prefs.slate_scene = prefs.slate_cut = 0
        prefs.slate_take = 0
        context.preferences.is_dirty = True
        return {"FINISHED"}


class TRKAR_OT_take_rec_stop(bpy.types.Operator):
    bl_idname = "trkar.take_rec_stop"
    bl_label = "Stop Recording"

    def execute(self, context):
        path = recorder.stop(context)
        if path:
            self.report({"INFO"}, _t("%s: %d frames (dropped %d)") % (os.path.basename(path), recorder.frames,
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
            bpy.ops.trkar.take_rec_start()
        elif recorder.recording:
            bpy.ops.trkar.take_rec_stop()


def _play_to_live(context):
    player.stop(context.scene)
    if _live_before_play["ndi"] and not ndi_in.running and context.scene.trkar.ndi_in_source:
        bpy.ops.trkar.ndi_in_start()
    if _live_before_play["depth"] and not depth_in.running:
        bpy.ops.trkar.depth_start()
    _live_before_play["ndi"] = _live_before_play["depth"] = False


def _start_player(operator, context):
    path = _take_path(context)
    if path is None:
        operator.report({"ERROR"}, _t("Select a take folder (the one containing take.json)"))
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
    _sync_take_offset(context.scene.trkar, player.info["time_offset"])
    _apply_take_range(context)
    return True


def _apply_take_range(context):
    """再生中のテイクのタイムラインの in / out を、書き出しの「範囲」(入っていなければテイクの全体) にする"""
    if not player.active or player.info is None or _export["running"]:
        return
    scene = context.scene
    first, last = player.info["frame_start"], player.info["frame_end"]
    if scene.trkar.take_use_range:
        first, last = _take_range(scene.trkar, scene.render, first, last)
    if (scene.frame_start, scene.frame_end) != (first, last):
        scene.frame_start, scene.frame_end = first, last
    if not first <= scene.frame_current <= last:
        scene.frame_set(first)


# take_path をコードから入れて、すぐ自分で再生を始めるとき (前後のテイク、通信ポートの select_take) は、
# 更新の読み直しを止めて二度読みしないようにする
_take_path_quiet = {"on": False}


def _set_take_path(context, path):
    _take_path_quiet["on"] = True
    try:
        context.scene.trkar.take_path = path
    finally:
        _take_path_quiet["on"] = False


def _reload_selected_take(context):
    """再生中に選び直されたテイクを読み直す。録画・書き出しの最中や、同じテイクのときは何もしない"""
    if _take_path_quiet["on"] or not player.active or recorder.recording or _export["running"]:
        return
    path = _take_path(context)
    if path is None or os.path.normpath(path) == os.path.normpath(player.path or ""):
        return
    reports = _Reports()
    if not _start_player(reports, context):
        print(_t("trkAR: could not reload the take:"), "; ".join(reports.errors))


def _take_siblings(context):
    """今のテイクと同じフォルダにあるテイク (take.json のあるもの) を名前順 (= 録った順) に。
    テイクを選んでいなければ保存先のフォルダ。(フォルダ, [パス...])"""
    current = _take_path(context)
    folder = os.path.dirname(current) if current else take.takes_folder(context.scene.trkar.take_folder)
    if not os.path.isdir(folder):
        return folder, []
    paths = [os.path.join(folder, name) for name in sorted(os.listdir(folder))]
    return folder, [p for p in paths if os.path.isfile(os.path.join(p, "take.json"))]


class TRKAR_OT_take_step(bpy.types.Operator):
    bl_idname = "trkar.take_step"
    bl_label = "Previous / Next Take"
    bl_description = "Switch to the previous / next take in the same folder (in recording order), ready to play"

    step: IntProperty(default=1)

    @classmethod
    def description(cls, _context, properties):
        return _t("Switch to the previous take (one older) and play it") if properties.step < 0 else \
            _t("Switch to the next take (one newer) and play it")

    def execute(self, context):
        if recorder.recording or _export["running"]:
            self.report({"ERROR"}, _t("Recording or exporting is in progress"))
            return {"CANCELLED"}
        _folder, takes = _take_siblings(context)
        if not takes:
            self.report({"ERROR"}, _t("No takes"))
            return {"CANCELLED"}
        current = _take_path(context)
        if current in takes:
            index = takes.index(current) + self.step
        else:  # 選んでいなければ、前 = 最新、次 = 最初
            index = len(takes) - 1 if self.step < 0 else 0
        if not 0 <= index < len(takes):
            self.report({"INFO"}, _t("This is the first take") if index < 0 else _t("This is the last take"))
            return {"CANCELLED"}
        _set_take_path(context, takes[index])
        if not _start_player(self, context):
            return {"CANCELLED"}
        _use_material_view(self, context)
        self.report({"INFO"}, "%s (%d / %d)" % (os.path.basename(takes[index]), index + 1, len(takes)))
        return {"FINISHED"}


def _draw_take_position(layout, context):
    """選んでいるテイクが何本目か、どのシーンで録ったか、長さ"""
    current = _take_path(context)
    if current is None:
        return
    _folder, takes = _take_siblings(context)
    try:
        info = take.load_info(current)
    except (OSError, ValueError):
        return
    fps = info.get("fps") or 24
    frames = (info.get("frame_end") or 0) - (info.get("frame_start") or 1) + 1
    recorded_in = (info.get("location") or {}).get("blend") or "-"
    text = _t("%d / %d   %s   %.1f s") % (takes.index(current) + 1 if current in takes else 0, len(takes),
                                     recorded_in, frames / fps)
    layout.label(text=text, icon="FILE_MOVIE")


class TRKAR_OT_take_play(bpy.types.Operator):
    bl_idname = "trkar.take_play"
    bl_label = "Play Take"
    bl_description = "Stop receiving and make the take's camera motion, live video, and depth playable on the timeline"

    def execute(self, context):
        if not _start_player(self, context):
            return {"CANCELLED"}
        _use_material_view(self, context)
        return {"FINISHED"}


class TRKAR_OT_take_align_here(bpy.types.Operator):
    bl_idname = "trkar.take_align_here"
    bl_label = "Range Start Here"
    bl_description = ("Shift the take against the CG so the start of the take's shot (the range start if a range is set, otherwise the beginning of the take) lands on the current frame of the timeline (the point in the background CG to match)")

    @classmethod
    def poll(cls, context):
        return player.active and not _export["running"]

    def execute(self, context):
        scene = context.scene
        settings = scene.trkar
        target = scene.frame_current
        first = player.info["frame_start"]
        if settings.take_use_range:
            first = _take_range(settings, scene.render, first, player.info["frame_end"])[0]
        delta = target - first
        if not delta:
            return {"CANCELLED"}
        _set_take_offset(context, player.info["time_offset"] + delta)
        scene.frame_set(target)
        self.report({"INFO"}, _t("Shifted the take by %+d frames against the CG (current offset %+d)") % (delta, player.info["time_offset"]))
        return {"FINISHED"}


class TRKAR_OT_take_live(bpy.types.Operator):
    bl_idname = "trkar.take_live"
    bl_label = "Back to Live"
    bl_description = "Stop playing the take and resume receiving the NDI / depth that was being received before"

    def execute(self, context):
        _play_to_live(context)
        return {"FINISHED"}


def _anchor_restart(operator, context):
    """背景のオフセット (Empty) を作った・消したのを再生中のカメラに反映する (再生し直す)"""
    if player.active:
        frame = context.scene.frame_current
        _start_player(operator, context)
        context.scene.frame_set(frame)


class TRKAR_OT_take_anchor_add(bpy.types.Operator):
    bl_idname = "trkar.take_anchor_add"
    bl_label = "Add Offset"
    bl_description = ("Place an Empty for shifting the take (person and camera motion) against the background where the person is in the take (\"Near Distance\" in front of the camera on the first frame, at floor height), and parent the playing camera to it. Moving the Empty or rotating it in Z shifts the location within the background while keeping the camera motion. The Empty's Y arrow is the camera's viewing direction on the first frame. The placement is saved per take in take.json and is also used for export")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if not player.active and not _start_player(self, context):
            return {"CANCELLED"}
        anchor = take.ensure_anchor(context.scene)
        anchor.matrix_world = player.pivot
        _anchor_restart(self, context)
        _use_material_view(self, context)
        return {"FINISHED"}


class TRKAR_OT_take_anchor_cursor(bpy.types.Operator):
    bl_idname = "trkar.take_anchor_cursor"
    bl_label = "To 3D Cursor"
    bl_description = ("Move the offset reference (where the person is in the take) to the 3D cursor. Shift + right-click the floor first, then press")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        anchor = bpy.data.objects.get(take.ANCHOR_NAME)
        if anchor is None:
            return {"CANCELLED"}
        anchor.location = context.scene.cursor.location
        return {"FINISHED"}


class TRKAR_OT_take_anchor_previous(bpy.types.Operator):
    bl_idname = "trkar.take_anchor_previous"
    bl_label = "Use Previous Anchor"
    bl_description = ("Restore the anchor position that was in the .blend before playing this take, and save it to this take. Use it to carry over an anchor set before anchors became per-take (up to 0.9.11)")
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


class TRKAR_OT_take_anchor_remove(bpy.types.Operator):
    bl_idname = "trkar.take_anchor_remove"
    bl_label = "Remove Offset"
    bl_description = "Remove the Background Offset and play the take at the position where it was recorded"
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


class TRKAR_OT_take_render(bpy.types.Operator):
    bl_idname = "trkar.take_render"
    bl_label = "Export"
    bl_description = ("Export the take from the camera view to <take>/render.mp4 (or a PNG sequence in render/). render.mp4 is overwritten by the next export, so a copy is also kept as render_<.blend name>.mp4. Resolution follows the scene's output settings")

    def execute(self, context):
        if not _start_player(self, context):
            return {"CANCELLED"}
        scene = context.scene
        settings = scene.trkar
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
            self.report({"ERROR"}, _t("The previous export is still running"))
            return {"CANCELLED"}
        area, region = _view3d_window(context)
        if area is None:
            restore()
            self.report({"ERROR"}, _t("No 3D View"))
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
            self.report({"ERROR"}, _t("Could not start the export"))
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
        _export["message"] = _t("Export cleanup failed: %s") % exc
        return None
    seconds = time.monotonic() - _export["t0"]
    out = _export["out"]
    if not completed:
        _export["message"] = _t("Export canceled (up to frame %d)") % _export["frame"]
    elif _export["video"]:
        size = os.path.getsize(out) / 1e6 if os.path.exists(out) else 0
        _export["message"] = _t("render.mp4 %.1f MB (%.0f s)") % (size, seconds)
    else:
        _export["message"] = _t("%d images in render/ (%.0f s)") % (len(take.list_frames(out)), seconds)
    if completed and _export["kept"]:
        _export["message"] += _t("  copy %s") % os.path.basename(_export["kept"])
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
            # テイクのコマ (粘土の入力の make_clay_take.py が既定の範囲に使う)。背景の CG は + time_offset
            "frame_start": take.take_frame(info, scene.frame_start),
            "frame_end": take.take_frame(info, scene.frame_end),
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
        print(_t("trkAR: cannot start the after-take command: %s") % exc)


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
            layout.operator("wm.url_open", text=_t("Open"), icon="URL").url = st["url"]
    row = layout.row(align=True)
    row.enabled = bool(prefs.hook_command.strip()) and not hooks.busy(path)
    row.operator("trkar.take_hook_run", icon="FILE_REFRESH")
    if st is not None:
        row.operator("trkar.take_open", icon="TEXT", text=_t("Log")).sub = hooks.LOG_FILE


class TRKAR_OT_take_hook_run(bpy.types.Operator):
    bl_idname = "trkar.take_hook_run"
    bl_label = "Run Command"
    bl_description = ("Run the add-on preferences' \"After-Take Command\" again for this take (event = exported if exported, otherwise recorded)")

    @classmethod
    def poll(cls, context):
        path = _take_path(context)
        return path is not None and not hooks.busy(path)

    def execute(self, context):
        path = _take_path(context)
        prefs = _prefs(context)
        event = "exported" if hooks._render_path(path) else "recorded"
        if not hooks.run(path, event, prefs.hook_command, ADDON_VERSION or "", prefs.hook_timeout):
            self.report({"ERROR"}, _t("\"After-Take Command\" in the add-on preferences is empty or unreadable"))
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
        raise RuntimeError(_t("%s did not run") % operator.idname_py())
    return result


def _anchor_state():
    anchor = bpy.data.objects.get(take.ANCHOR_NAME)
    if anchor is None:
        return None
    return {"location": list(anchor.location), "heading_deg": math.degrees(anchor.rotation_euler.z)}


def _bridge_state(context):
    scene = context.scene
    settings = scene.trkar
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
        raise RuntimeError(_t("Not found: %s") % path)
    if _export["running"] or recorder.recording:
        raise RuntimeError(_t("Recording or exporting is in progress"))
    _call(context, bpy.ops.wm.open_mainfile, filepath=path)
    # 開いた直後は窓や画面が入れ替わっているので、中身は次の state で見る
    return {"blend": path}


def _bridge_list_takes(context):
    folder = take.takes_folder(bpy.path.abspath(context.scene.trkar.take_folder))
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
        raise RuntimeError(_t("Recording or exporting is in progress"))
    listing = _bridge_list_takes(context)["takes"]
    found = [t for t in listing if t["name"] == name] if name else listing[-1:]
    if not found:
        raise RuntimeError(_t("Take not found: %s") % (name or _t("(latest)")))
    _set_take_path(context, found[0]["path"])
    reports = _Reports()
    if not _start_player(reports, context):
        raise RuntimeError("; ".join(reports.errors) or _t("Cannot start playback"))
    _use_material_view(reports, context)
    return _bridge_state(context)


def _bridge_place_anchor(context, location=None, heading_deg=None):
    """背景のオフセットを作り (無ければ)、位置 (床の高さ) と向き (Z、度) を入れる。省いたものは録ったときのまま"""
    if not player.active:
        raise RuntimeError(_t("Select a take with select_take first"))
    if bpy.data.objects.get(take.ANCHOR_NAME) is None:
        _call(context, bpy.ops.trkar.take_anchor_add)
    anchor = bpy.data.objects[take.ANCHOR_NAME]
    if location is not None:
        anchor.location = Vector([float(v) for v in location])
    if heading_deg is not None:
        anchor.rotation_euler.z = math.radians(float(heading_deg))
    context.view_layer.update()
    return _anchor_state()


def _bridge_remove_anchor(context):
    _call(context, bpy.ops.trkar.take_anchor_remove)
    return _anchor_state()


# configure で変えてよい設定 (書き出しまわりだけ)
_BRIDGE_SETTINGS = ("take_render_mode", "take_render_format", "take_render_size", "take_use_range",
                    "take_range_start", "take_range_end", "take_range_mode", "take_range_seconds",
                    "take_render_samples")


def _bridge_configure(context, **values):
    settings = context.scene.trkar
    unknown = sorted(set(values) - set(_BRIDGE_SETTINGS))
    if unknown:
        raise RuntimeError(_t("Unchangeable setting: %s (available: %s)") % (", ".join(unknown), ", ".join(_BRIDGE_SETTINGS)))
    for key, value in values.items():
        setattr(settings, key, value)
    return {key: getattr(settings, key) for key in _BRIDGE_SETTINGS}


def _bridge_export_take(context):
    """書き出しを始めて返す (終わりは state の export で見る)"""
    if _export["running"]:
        raise RuntimeError(_t("The previous export is still running"))
    if not player.active:
        raise RuntimeError(_t("Select a take with select_take first"))
    _call(context, bpy.ops.trkar.take_render)
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


class TRKAR_OT_bridge_start(bpy.types.Operator):
    bl_idname = "trkar.bridge_start"
    bl_label = "Open Automation Port"
    bl_description = ("Accept fixed operations (open a scene, select a take, Background Offset, export settings, export, status) over HTTP on 127.0.0.1 from outside tools (Griptape Nodes nodes, agents such as Claude, your own scripts). Arbitrary Python is not accepted")

    def execute(self, context):
        try:
            bridge.start(_BRIDGE_HANDLERS, context.scene.trkar.bridge_port)
        except OSError as exc:
            self.report({"ERROR"}, _t("Cannot open the Automation Port: %s") % exc)
            return {"CANCELLED"}
        return {"FINISHED"}


class TRKAR_OT_bridge_stop(bpy.types.Operator):
    bl_idname = "trkar.bridge_stop"
    bl_label = "Close Automation Port"

    def execute(self, context):
        bridge.stop()
        return {"FINISHED"}


def _prefs(context):
    return context.preferences.addons[__package__].preferences


class TRKAR_OT_take_prepare(bpy.types.Operator):
    bl_idname = "trkar.take_prepare"
    bl_label = "Export Generation Inputs"
    bl_description = ("Write intermediate files for video generation models that take references to <take>/for_generation/: the reference video (clay background + person outlines), actor photos, location layout, first frame, camera, and a prompt template. Uses the range if one is set. Does not generate")

    def execute(self, context):
        if _export["running"] or recorder.recording:
            self.report({"ERROR"}, _t("Recording or export in progress"))
            return {"CANCELLED"}
        if not _start_player(self, context):
            return {"CANCELLED"}
        scene, settings = context.scene, context.scene.trkar
        camera = settings.camera or scene.camera
        if camera is None:
            self.report({"ERROR"}, _t("No camera"))
            return {"CANCELLED"}
        first, last = player.info["frame_start"], player.info["frame_end"]
        if settings.take_use_range:
            first, last = _take_range(settings, scene.render, first, last)
        wm = context.window_manager
        count = last - first + 1
        total = count * 2 + 3
        stage_base = {"clay": 0, "reference": count, "encode": total - 3, "actor": total - 2, "layout": total - 1}

        def progress(stage, done, _count):
            wm.progress_update(stage_base.get(stage, 0) + done)

        wm.progress_begin(0, total)
        try:
            _out, manifest = prep.export(scene, player, camera, settings.take_prep_long_side, first, last,
                                         settings.take_prep_layout, progress)
        except Exception as exc:
            self.report({"ERROR"}, _t("Cannot export generation inputs: %s") % exc)
            return {"CANCELLED"}
        finally:
            wm.progress_end()
        ref = manifest["files"]["reference.mp4"]
        self.report({"INFO"}, _t("Exported to %s/ (%d×%d, %d frames, %.0f s)") % (
            prep.FOLDER, ref["width"], ref["height"], ref["frames"], manifest["seconds_to_make"]))
        return {"FINISHED"}


class TRKAR_OT_take_open(bpy.types.Operator):
    bl_idname = "trkar.take_open"
    bl_label = "Open Folder"

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

class _TrkARPanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "trkAR"


class _TrkARSubPanel(_TrkARPanel):
    bl_options = {"DEFAULT_CLOSED"}


def _header_toggle(layout, running, start, stop, status=""):
    row = layout.row(align=True)
    if status:
        row.label(text=status)
    if running:
        row.operator(stop, text="", icon="PAUSE")
    else:
        row.operator(start, text="", icon="PLAY")


class TRKAR_PT_panel(_TrkARPanel, bpy.types.Panel):
    """trkAR (iPhone / iPad など) から受けるもの (姿勢・実写・深度) を 1 つにまとめたパネル。
    見出しの ▶ でまとめて始め、中の段ごとに状態と個別の入り切りを出す"""
    bl_label = "trkAR Receive"
    bl_order = 0

    def draw_header(self, context):
        self.layout.label(icon="CAMERA_DATA")

    def draw_header_preset(self, context):
        running = _receiving()
        status = (_t("Playing") if player.active else _t("Receiving")) if running else ""
        _header_toggle(self.layout, running, "trkar.receive_start", "trkar.receive_stop", status)

    def draw(self, context):
        layout = self.layout
        settings = context.scene.trkar
        server = tracking.server
        _receive_section(layout, "TRKAR_recv_tracking", _t("Camera Tracking"), "CAMERA_DATA",
                         (_t("Playing") if player.active else _t("Receiving")) if server.running else "",
                         server.running, "trkar.tracking_start", "trkar.tracking_stop", None, context, _draw_tracking)
        _receive_section(layout, "TRKAR_recv_video", _t("Video"), "IMAGE_DATA", _video_status(),
                         ndi_in.running, "trkar.ndi_in_start", "trkar.ndi_in_stop", "receive_video", context,
                         _draw_ndi_in)
        _receive_section(layout, "TRKAR_recv_depth", _t("Depth"), "MOD_DISPLACE", _depth_status(),
                         depth_in.running, "trkar.depth_start", "trkar.depth_stop", "receive_depth", context,
                         _draw_depth)


def _receiving():
    return tracking.server.running or ndi_in.running or depth_in.running


def _receive_section(layout, idname, title, icon, status, running, start, stop, include, context, draw):
    """trkAR 受信の中の 1 段 (見出し: まとめて始めるときに入れるか・名前・状態・個別の ▶ / ⏸)。"""
    header, body = layout.panel(idname, default_closed=idname != "TRKAR_recv_tracking")
    row = header.row(align=True)
    if include is not None:
        row.prop(context.scene.trkar, include, text="")
    row.label(text=title, icon=icon)
    _header_toggle(header, running, start, stop, status)
    if body:
        draw(body, context)


def _draw_tracking(layout, context):
    """カメラ追従 (OSC) の段の中身。"""
    settings = context.scene.trkar
    server = tracking.server
    col = layout.column(align=True)
    col.enabled = not server.running
    col.prop(settings, "port")
    # iPhone の送信先 (SystemPC IP / Port) に入れる値。▶ で畳める
    header, body = layout.panel("TRKAR_send_to", default_closed=False)
    header.label(text=_t("iPhone Send Target"), icon="URL")
    if body:
        box = body.box()
        ips = osc.local_ipv4_addresses()
        if not ips:
            box.label(text=_t("Not connected to a network"), icon="ERROR")
        for ip, kind, _dev in ips:
            row = box.row(align=True)
            # 接続の種類 (osc.py の値)。「有線」は比べるための値なので、出すときだけ訳す
            shown = _t("Wired") if kind == "有線" else (kind or "")
            row.label(text="%s  %s : %d" % (shown, ip, settings.port),
                      icon="LINKED" if kind == "有線" else ("RADIOBUT_ON" if kind == "Wi-Fi" else "NETWORK_DRIVE"))
            row.operator("trkar.copy_ip", text="", icon="COPYDOWN").ip = ip
        if server.running and server.last_sender:
            box.label(text=_t("Receiving from %s") % server.last_sender, icon="CHECKMARK")
    layout.prop(settings, "camera")
    if server.running:
        if player.active:
            layout.label(text=_t("Playing a take: not writing to the camera"), icon="PLAY")
        layout.label(text=_t("Received %d / UDP %d (:%d)") % (server.packet_count, server.datagram_count, server.port))
        # 姿勢が届いている周期 (trkAR 1.1.2 からは ARKit のフレームごと、60fps の機種で 60Hz 近く。1.1.1 までは約 30Hz)
        rate = tracking.pose_rate()
        layout.label(text=_t("Pose %.0f Hz") % rate if rate > 0 else _t("Pose -- Hz"), icon="TIME")
        if server.datagram_count == 0:
            col = layout.column(align=True)
            col.label(text=_t("Nothing is arriving"), icon="ERROR")
            col.label(text=_t("Check the iPhone's target IP / port and macOS"))
            col.label(text=_t("Privacy > Local Network"))
        elif server.packet_count == 0:
            layout.label(text=_t("/client/current is not arriving"), icon="ERROR")
        if server.last_error:
            layout.label(text=server.last_error, icon="ERROR")
        _draw_pose_sync(layout, settings, detail=False)
        pose = tracking.current_pose()
        if pose is not None and not pose.floor_height:
            layout.label(text=_t("iPhone floor not set: point at the floor and tap SET GND"), icon="ERROR")

    layout.row().prop(settings, "mode", expand=True)
    if settings.mode == "RELATIVE":
        row = layout.row(align=True)
        row.prop(settings, "floor_from_iphone", text=_t("Height from Floor"))
        sub = row.row(align=True)
        sub.active = settings.floor_from_iphone
        sub.prop(settings, "floor_z", text=_t("Floor"))
        layout.operator("trkar.reanchor", icon="ORIENTATION_CURSOR")
        if server.running:
            if tracking.is_anchored():
                layout.label(text=_t("To move the camera: Stop Receiving, move it, Start Receiving"), icon="INFO")
            else:
                layout.label(text=_t("Waiting for the first pose…"), icon="TIME")
    else:
        layout.operator("trkar.align_to_cursor", icon="PIVOT_CURSOR")


def _draw_pose_sync(layout, settings, detail):
    """映像 (NDI 受信) が姿勢よりどれだけ遅れているかと、「映像に合わせる」の状態。"""
    lag = tracking.video_lag() if ndi_in.running else None
    if lag is not None:
        layout.label(text=_t("Video delay %.0f ms (jitter %.0f–%.0f)") % lag, icon="TIME")
    state = tracking.sync["state"]
    if state == "SYNCED":
        layout.label(text=_t("Synced with video"), icon="CHECKMARK")
        count, ahead = tracking.predicted_recent()
        if count:
            # 姿勢が映像より遅れて届いた (Wi-Fi の止まりなど)
            verb = _t("filled in by prediction") if settings.pose_predict else _t("camera stopped")
            layout.label(text=_t("Waited for pose on %d frames / 5 s (%s; max %.0f ms)") % (count, verb, ahead), icon="ERROR")
    elif state == "TOO_LATE":
        layout.label(text=_t("Video delay is too large to align"), icon="ERROR")
    elif state == "NO_TIME":
        layout.label(text=_t("Not aligning because the clocks don't match (update trkAR)"), icon="INFO")
    elif state == "NO_VIDEO" and detail:
        layout.label(text=_t("Receive the video over NDI to align"), icon="INFO")


class TRKAR_PT_tracking_place(_TrkARSubPanel, bpy.types.Panel):
    """スケール・位置合わせのオフセットと、カメラ視点の枠の外の暗さ (たまにしか触らないもの)。"""
    bl_label = "Position & Display"
    bl_parent_id = "TRKAR_PT_panel"
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        settings = context.scene.trkar
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
            sub.prop(camera.data, "passepartout_alpha", text=_t("Outside Frame Dimming"), slider=True)


class TRKAR_PT_tracking_detail(_TrkARSubPanel, bpy.types.Panel):
    bl_label = "Sync & Details"
    bl_parent_id = "TRKAR_PT_panel"
    bl_order = 2

    def draw(self, context):
        layout = self.layout
        settings = context.scene.trkar
        layout.prop(settings, "smoothing", slider=True)
        col = layout.column(align=True)
        row = col.row(align=True)
        row.prop(settings, "pose_sync")
        sub = row.row(align=True)
        sub.active = settings.pose_sync
        sub.prop(settings, "pose_sync_offset", text=_t("Correction ms"))
        sub = col.row(align=True)
        sub.active = settings.pose_sync
        sub.prop(settings, "pose_predict")
        if tracking.server.running:
            _draw_pose_sync(col, settings, detail=True)
            # 更新と描画の時間 (コマ落ち・ふわふわの原因を見る。perf.py)
            box = layout.box()
            box.label(text=_t("Update and draw time (last 3 s)"), icon="TIME")
            for line in perf.lines():
                box.label(text=line)
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
        header, body = layout.panel("TRKAR_detail_depth", default_closed=True)
        header.label(text=_t("Depth Mesh, Outline & Sync"), icon="MOD_DISPLACE")
        if body:
            TRKAR_PT_depth_detail.draw(_Section(body), context)


def _controls_active(settings):
    return bool(any(settings.ctl_location) or settings.ctl_pan or settings.ctl_tilt
                or settings.ctl_roll or settings.ctl_lens > 0.0 or any(settings.ctl_pan_shift)
                or settings.ctl_fstop != 0.0 or settings.ctl_focus > 0.0)


class TRKAR_PT_controls(_TrkARSubPanel, bpy.types.Panel):
    """Blender 側のオフセット。値が入っているときは見出しに印を付ける。"""
    bl_label = "Blender Offset"
    bl_parent_id = "TRKAR_PT_panel"
    bl_order = 3

    def draw_header_preset(self, context):
        if tracking.remote_status() is not None:
            self.layout.label(text=_t("Remote Control Active"), icon="LINKED")
        elif _controls_active(context.scene.trkar):
            self.layout.label(text=_t("In Use"), icon="ERROR")

    def draw(self, context):
        layout = self.layout
        settings = context.scene.trkar
        layout.operator("trkar.view_controller", icon="VIEW_PAN", depress=view_controller.controller.running,
                        text=_t("Close View Controller") if view_controller.controller.running
                        else _t("View Controller"))
        view_controller.draw_panel(layout, context)
        layout.prop(settings, "use_remote")
        remote = tracking.remote_status()
        if remote is not None:
            layout.label(text=_t("Controlled from Client %s (lens settings also from that device)") % remote[:8], icon="LINKED")
        elif settings.use_remote and tracking.server.running:
            pose = tracking.current_pose()
            if pose is not None:
                layout.label(text=_t("Select TARGET → CAM %d in CTL on the monitor device to move it") % pose.camera_id,
                             icon="INFO")
        col = layout.column(align=True)
        col.prop(settings, "ctl_location")
        col = layout.column(align=True)
        col.prop(settings, "ctl_pan")
        col.prop(settings, "ctl_tilt")
        col.prop(settings, "ctl_roll")
        col = layout.column(align=True)
        col.prop(settings, "ctl_lens")
        col.prop(settings, "ctl_fstop")
        col.prop(settings, "ctl_focus")
        row = layout.row(align=True)
        row.operator("trkar.ctl_reset", text=_t("Reset Rotation")).target = "ROTATION"
        row.operator("trkar.ctl_reset", text=_t("Reset All")).target = "ALL"
        row.operator("trkar.ctl_keyframe", icon="KEY_HLT")
        if player.active:
            # 録ったときのずれはカメラの動きに焼き込み済み。再生中に変えても効かない
            box = layout.box()
            box.label(text=_t("Has no effect during take playback (the offset at recording time is baked in)"), icon="INFO")
            box.label(text=_t("Move a take's position with \"Recording & Takes > Background Offset\""))


def _video_status():
    """実写 (NDI) の段の見出しの状態。"""
    status = ""
    if ndi_in.running:
        status = "RVM" if matte_relay.running else _t("Receiving")
    elif player.active:
        status = _t("Take")
    return status



def _draw_ndi_in(layout, context):
    """実写 (NDI) の段の中身。"""
    settings = context.scene.trkar
    col = layout.column(align=True)
    col.enabled = not ndi_in.running
    row = col.row(align=True)
    row.prop(settings, "ndi_in_source", text="")
    row.menu("TRKAR_MT_ndi_sources", text="", icon="DOWNARROW_HLT")
    col.prop(settings, "ndi_in_low")
    col.prop(settings, "ndi_in_auto_screen")
    if settings.ndi_in_source and settings.ndi_name and settings.ndi_name in settings.ndi_in_source:
        layout.label(text=_t("This is Blender's own output (the video will loop)"), icon="ERROR")
    if ndi_in.running:
        width, height = ndi_in.resolution
        if ndi_in.frames_shown:
            layout.label(text=_t("%d×%d / %d frames") % (width, height, ndi_in.frames_shown))
            layout.label(text=_t("Receive %.1f fps / Display %.1f fps") % (ndi_in.recv_rate.fps, ndi_in.show_rate.fps))
        else:
            waiting = time.monotonic() - getattr(ndi_in, "started_at", time.monotonic())
            try:
                missing = waiting > 5.0 and ndi_in.source_name not in ndi_in.sources(_library_override(context))
            except Exception:  # noqa: BLE001
                missing = False
            if missing:
                # 無いソースを待ち続けない (選び直す)
                layout.label(text=_t("Source %s not found") % ndi_in.source_name, icon="ERROR")
                layout.label(text=_t("Select the source again and restart receiving"))
            else:
                layout.label(text=_t("Waiting for video…"), icon="TIME")
        if ndi_in.last_error:
            layout.label(text=ndi_in.last_error, icon="ERROR")
    # RVM の切り抜き (別プロセスの中継 tools/matte。入れていなければ押したときに場所を聞く)
    sub = layout.column(align=True)
    if matte_relay.running:
        sub.operator("trkar.matte_stop", icon="CANCEL")
        status = matte_relay.status
        if status.startswith("受信"):
            # "受信 30 fps / 送出 30 fps / RVM 17 ms / 受信→送出 40 ms / ..." を 2 行に
            parts = status.split(" / ")
            sub.label(text=" / ".join(parts[:3]), icon="MOD_MASK")
            sub.label(text=" / ".join(parts[3:]))
        else:
            sub.label(text=status or _t("Starting RVM…"), icon="TIME")
    else:
        row = sub.row(align=True)
        row.operator("trkar.matte_start", icon="MOD_MASK")
        row.prop(settings, "matte_model", text="")
        sub.prop(settings, "receive_matte")
        if matte_relay.last_error:
            sub.label(text=matte_relay.last_error, icon="ERROR")
    _draw_edge_style(layout, settings)


def _draw_edge_style(layout, settings):
    """切り抜いた人の輪郭の描き方 (くっきり / やわらか)。"""
    col = layout.column(align=True)
    col.row(align=True).prop(settings, "edge_style", expand=True)
    if settings.edge_style == "SHARP":
        col.prop(settings, "edge_sharp_width", slider=True)


class TRKAR_PT_ndi_in_screen(_TrkARSubPanel, bpy.types.Panel):
    bl_label = "Plate & Background"
    bl_parent_id = "TRKAR_PT_panel"
    bl_order = 1

    def draw(self, context):
        layout = self.layout
        settings = context.scene.trkar
        layout.label(text=_t("Available as image \"%s\"") % ndi_input.IMAGE_NAME, icon="INFO")
        # 「スクリーンを置く」(原点に板を置く) は使わないのでボタンを外した (オペレーターは残してある)
        layout.operator("trkar.ndi_in_camera_bg", icon="VIEW_CAMERA")
        layout.prop(settings, "ndi_in_lens_crop")
        row = layout.row(align=True)
        row.operator("trkar.ndi_in_front", icon="OUTLINER_OB_CAMERA")
        sub = row.row(align=True)
        sub.active = not settings.ndi_in_front_follow
        sub.prop(settings, "ndi_in_front_distance", text=_t("Distance"))
        layout.prop(settings, "ndi_in_front_follow")
        if settings.ndi_in_front_follow:
            distance, following = _front_distance(context.scene)
            if following:
                layout.label(text=_t("Placed at the person's distance, %.2f m") % distance, icon="USER")
                layout.label(text=_t("Depth mesh is hidden (it flickers where it overlaps the plate)"), icon="HIDE_ON")
            elif not depth_in.running:
                layout.label(text=_t("Placed at `Distance` because depth is not being received"), icon="INFO")
            else:
                layout.label(text=_t("Placed at `Distance` because no person is visible"), icon="INFO")
        layout.prop(settings, "dof_autofocus")
        if settings.dof_autofocus:
            layout.prop(settings, "dof_autofocus_smoothing")
            camera = settings.camera or context.scene.camera
            if camera is not None and camera.type == "CAMERA" and camera.data.dof.use_dof:
                dof = camera.data.dof
                layout.label(text=_t("Focus %.2f m (F%.1f)") % (dof.focus_distance, dof.aperture_fstop), icon="CAMERA_DATA")
            else:
                # iPhone のコントローラーで F 値を 0 (DOF OFF) にしていると、カメラの被写界深度は毎回切られる
                layout.label(text=_t("Camera depth of field is off (turn it on with the iPhone's f-stop or the camera settings)"), icon="INFO")
        # 深度の人物・背景 (人を板にする・人の距離の設定と並べる)
        header, body = layout.panel("TRKAR_screen_people", default_closed=True)
        header.label(text=_t("Person & Background (Depth)"), icon="USER")
        if body:
            TRKAR_PT_depth_people.draw(_Section(body), context)


def _depth_status():
    """深度の段の見出しの状態。"""
    status = ""
    if depth_in.running:
        status = _t("Receiving")
    elif player.active:
        status = _t("Take")
    return status



def _draw_depth(layout, context):
    """深度の段の中身。"""
    settings = context.scene.trkar
    col = layout.column(align=True)
    col.enabled = not depth_in.running
    col.row(align=True).prop(settings, "depth_transport", expand=True)
    if settings.depth_transport == "UDP":
        col.prop(settings, "depth_udp_port")
    else:
        row = col.row(align=True)
        row.prop(settings, "depth_source", text="")
        row.menu("TRKAR_MT_depth_sources", text="", icon="DOWNARROW_HLT")
    if depth_in.running:
        if depth_in.frames_shown:
            width, height = depth_in.resolution
            layout.label(text=_t("%d×%d / %d frames / valid %d%%") % (
                width, height, depth_in.frames_shown, depth_in.valid_ratio * 100))
        elif bpy.data.objects.get(depth_input.OBJECT_NAME) is None:
            layout.label(text=_t("No depth screen"), icon="ERROR")
        else:
            layout.label(text=_t("Waiting for depth… (%s)") % depth_in.source_name, icon="TIME")
        if depth_in.last_error:
            layout.label(text=depth_in.last_error, icon="ERROR")
    row = layout.row(align=True)
    row.operator("trkar.depth_screen", icon="OUTLINER_OB_CAMERA")
    row.operator("trkar.depth_freeze", icon="FREEZE")
    if not ndi_in.running and not player.active:
        layout.label(text=_t("The video comes from NDI receive (Video)"), icon="INFO")
    if _needs_material_view(context) and bpy.data.objects.get(depth_input.OBJECT_NAME) is not None:
        layout.label(text=_t("Live video colors are not shown in Solid shading"), icon="ERROR")
        layout.operator("trkar.view_material", icon="MATERIAL")


class _Section:
    """パネルの draw を、別のパネルの中の折りたたみ (layout.panel) に描くときの self の代わり。"""

    def __init__(self, layout):
        self.layout = layout


class TRKAR_PT_depth_people:
    """板・背景の中の「人物・背景 (深度)」(パネルとしては登録しない)。"""

    def draw(self, context):
        settings = context.scene.trkar
        col = self.layout.column(align=True)
        col.prop(settings, "depth_flat_cutout")
        if settings.depth_flat_cutout:
            if depth_in.running:
                col.label(text=_t("Person %d%% (used directly as the trkAR NDI alpha)") % (depth_in.person_ratio * 100),
                          icon="USER")
            else:
                col.label(text=_t("Start depth receiving (UDP)"), icon="INFO")
            return
        if depth_in.running and depth_in.video_alpha:
            # trkar_matte.py (RVM) の "trkAR Matte" を受けているとき
            col.label(text=_t("Outline is matted with the video alpha (RVM)"), icon="MOD_MASK")
        if depth_in.running and depth_in.person_only:
            # 深度なしの機種: 人だけを表示する (人物の設定は効かない)
            col.label(text=_t("No depth: person mask only (person %d%%)") % (depth_in.person_ratio * 100),
                      icon="USER")
            col.prop(settings, "depth_person_distance")
        else:
            col.row(align=True).prop(settings, "depth_people", expand=True)
            if settings.depth_people != "OFF" and depth_in.running and depth_in.frames_shown:
                if depth_in.has_people:
                    col.label(text=_t("Person %d%%") % (depth_in.person_ratio * 100), icon="USER")
                else:
                    col.label(text=_t("Person mask is not arriving (requires UDP and a newer trkAR)"), icon="INFO")
        row = col.row(align=True)
        row.prop(settings, "depth_background", toggle=True)
        sub = row.row(align=True)
        sub.active = settings.depth_background
        sub.prop(settings, "depth_background_distance", text=_t("Distance"))
        if (settings.depth_background and depth_in.running and depth_in.person_only
                and settings.depth_background_distance <= settings.depth_person_distance):
            col.label(text=_t("Background is in front of the person (set it farther than the person)"), icon="ERROR")


class TRKAR_PT_depth_detail:
    """同期・詳細の中の「深度のメッシュ・輪郭・同期」(パネルとしては登録しない)。"""

    def draw(self, context):
        layout = self.layout
        settings = context.scene.trkar
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
        sub.prop(settings, "depth_sync_offset", text=_t("Correction ms"))
        if settings.depth_sync and depth_in.running and depth_in.frames_shown:
            if depth_in.sync_lag is not None:
                col.label(text=_t("Synced with video (depth %+.0f ms)") % -depth_in.sync_lag, icon="CHECKMARK")
            elif not ndi_in.running:
                col.label(text=_t("Receive the video over NDI to align"), icon="INFO")
            else:
                col.label(text=_t("Not aligning because the clocks don't match (update trkAR)"), icon="INFO")


class TRKAR_PT_take(_TrkARPanel, bpy.types.Panel):
    bl_label = "Recording & Takes"
    bl_order = 3

    def draw_header(self, context):
        self.layout.label(icon="REC")

    def draw_header_preset(self, context):
        if recorder.recording:
            row = self.layout.row()
            row.alert = True
            seconds = int(recorder.seconds)
            row.operator("trkar.take_rec_stop", icon="PAUSE", text="%d:%02d" % (seconds // 60, seconds % 60))
        elif export_progress() is not None:
            done, total, _remaining = export_progress()
            self.layout.label(text=_t("Exporting %d/%d") % (done, total))
        elif player.active:
            self.layout.label(text=_t("Playing"))

    def draw(self, context):
        layout = self.layout
        settings = context.scene.trkar
        if recorder.recording:
            row = layout.row()
            row.alert = True
            row.scale_y = 1.4
            seconds = int(recorder.seconds)
            row.operator("trkar.take_rec_stop", icon="PAUSE",
                         text=_t("Stop Recording  %d:%02d  (%d frames)") % (seconds // 60, seconds % 60, recorder.frames))
            if recorder.slate_label:
                layout.label(text=_t("Recording %s") % recorder.slate_label, icon="FILE_MOVIE")
            if recorder.dropped or recorder.errors:
                layout.label(text=_t("Dropped %d / write errors %d %s") % (recorder.dropped, recorder.errors,
                                                                  recorder.last_error), icon="ERROR")
            return
        prefs = _prefs(context)
        layout.label(text=_t("Next  %s   %s") % (_slate_label(prefs), prefs.slate_production or "-"), icon="FILE_MOVIE")
        row = layout.row(align=True)
        row.scale_y = 1.4
        row.operator("trkar.take_rec_start", icon="REC")
        row.prop(settings, "take_fps", text="")
        if not tracking.server.running:
            layout.label(text=_t("The camera won't move while tracking is stopped"), icon="INFO")
        if not ndi_in.running and not player.active:
            layout.label(text=_t("Video can't be recorded while NDI receive (Video) is stopped"), icon="INFO")

        row = layout.row(align=True)
        row.enabled = not (recorder.recording or _export["running"])
        row.operator("trkar.take_step", icon="TRIA_LEFT", text="").step = -1
        row.prop(settings, "take_path", text="")
        row.operator("trkar.take_step", icon="TRIA_RIGHT", text="").step = 1
        _draw_take_position(layout, context)
        row = layout.row(align=True)
        row.enabled = not _export["running"]
        if player.active:
            row.operator("trkar.take_live", icon="LOOP_BACK")
        else:
            row.operator("trkar.take_play", icon="PLAY")
        row.operator("trkar.take_open", icon="FILE_FOLDER", text="").sub = ""
        progress = export_progress()
        if progress is not None:
            done, total, remaining = progress
            text = _t("Exporting %d / %d") % (done, total)
            if remaining is not None:
                text += _t("  About %d s left") % remaining
            layout.label(text=text, icon="RENDER_ANIMATION")
            layout.label(text=_t("Esc to Cancel"))
        else:
            row = layout.row(align=True)
            row.operator("trkar.take_render", icon="RENDER_ANIMATION")
            row.prop(settings, "take_render_format", text="")
            # 参照を入れられる動画生成モデルに渡す中間ファイル (prep.py)。生成はしない
            row = layout.row(align=True)
            row.operator("trkar.take_prepare", icon="FILE_MOVIE")
            row.prop(settings, "take_prep_long_side", text="")
            row.prop(settings, "take_prep_layout", text="", icon="VIEW_CAMERA")
            row.operator("trkar.take_open", icon="FILE_FOLDER", text="").sub = prep.FOLDER
            row = layout.row(align=True)
            row.prop(settings, "take_use_range", toggle=True)
            sub = row.row(align=True)
            sub.active = settings.take_use_range
            sub.prop(settings, "take_range_start", text=_t("Start"))
            if settings.take_range_mode == "DURATION":
                sub.prop(settings, "take_range_seconds", text=_t("Seconds"))
            else:
                sub.prop(settings, "take_range_end", text=_t("End"))
            if settings.take_use_range:
                layout.row(align=True).prop(settings, "take_range_mode", expand=True)
            # 背景の CG の時間とのずれ (タイムラインで CG の合わせたい所に移って、範囲の頭をそこへ)
            row = layout.row(align=True)
            row.operator("trkar.take_align_here", icon="NEXT_KEYFRAME")
            row.prop(settings, "take_time_offset", text=_t("CG Time Offset"))
            count = _export_count(context, settings)
            if count is not None:
                text = _t("%d frames (%.2f s)") % count
                if settings.take_use_range:
                    first, last = _take_range(settings, context.scene.render,
                                              player.info["frame_start"], player.info["frame_end"])
                    text = _t("%d–%d  ") % (first, last) + text
                    wanted = round(settings.take_range_seconds * context.scene.render.fps
                                   / context.scene.render.fps_base)
                    if settings.take_range_mode == "DURATION" and count[0] < max(1, wanted):
                        text += _t("  Cut off at the end of the take")
                if settings.take_render_size == "LTX" and count[0] > LTX_MAX_FRAMES:
                    layout.label(text=text + _t("  LTX allows up to %d frames") % LTX_MAX_FRAMES, icon="ERROR")
                else:
                    layout.label(text=text)
            if _export["message"]:
                layout.label(text=_export["message"], icon="CHECKMARK")
            take_path = _take_path(context)
            if take_path is not None:
                _draw_hook(layout, context, take_path)
        if player.active:
            info = player.info
            text = _t("%s  frames %d–%d") % (info.get("name", ""), info["frame_start"], info["frame_end"])
            if info.get("time_offset"):
                text += _t(" (take %d–%d)") % (info["take_frame_start"], info["take_frame_end"])
            layout.label(text=text, icon="SEQUENCE")
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


class TRKAR_PT_take_slate(_TrkARSubPanel, bpy.types.Panel):
    bl_label = "Slate & Shoot Info"
    bl_parent_id = "TRKAR_PT_take"
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
            layout.label(text=_t("Device info (/client/info) has not arrived yet"), icon="INFO")
        else:
            layout.label(text=_t("Device model is not recorded because tracking is not being received"), icon="INFO")


class TRKAR_PT_take_anchor(_TrkARSubPanel, bpy.types.Panel):
    bl_label = "Background Offset"
    bl_parent_id = "TRKAR_PT_take"

    def draw_header_preset(self, context):
        if bpy.data.objects.get(take.ANCHOR_NAME) is not None:
            self.layout.label(text=_t("Offset Active"))

    def draw(self, context):
        layout = self.layout
        anchor = bpy.data.objects.get(take.ANCHOR_NAME)
        if anchor is None:
            layout.label(text=_t("Takes play back at the position where they were recorded"), icon="INFO")
            layout.operator("trkar.take_anchor_add", icon="EMPTY_ARROWS")
            return
        layout.label(text=_t("Where the person stands (floor height) and the camera's facing (Y arrow)"))
        col = layout.column(align=True)
        col.prop(anchor, "location", text="")
        layout.prop(anchor, "rotation_euler", index=2, text=_t("Facing (Z)"))
        row = layout.row(align=True)
        row.operator("trkar.take_anchor_cursor", icon="PIVOT_CURSOR")
        row.operator("trkar.take_anchor_add", icon="LOOP_BACK", text=_t("Original Position"))
        layout.operator("trkar.take_anchor_remove", icon="X")
        if not player.active:
            layout.label(text=_t("The offset applies only while playing or exporting a take"), icon="INFO")
        else:
            layout.label(text=_t("The placement stays with this take (doesn't affect other takes)"), icon="INFO")
            if player.previous_anchor is not None:
                layout.operator("trkar.take_anchor_previous", icon="RECOVER_LAST")


class TRKAR_PT_take_bridge(_TrkARSubPanel, bpy.types.Panel):
    bl_label = "Automation Port"
    bl_parent_id = "TRKAR_PT_take"
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header_preset(self, context):
        if bridge.running():
            self.layout.label(text=_t("Opened"))

    def draw(self, context):
        layout = self.layout
        if bridge.running():
            layout.label(text="http://127.0.0.1:%d" % bridge.port(), icon="LINKED")
            if bridge.last():
                layout.label(text=bridge.last())
            layout.operator("trkar.bridge_stop", icon="X")
        else:
            layout.prop(context.scene.trkar, "bridge_port")
            layout.operator("trkar.bridge_start", icon="PLAY")


class TRKAR_PT_take_settings(_TrkARSubPanel, bpy.types.Panel):
    bl_label = "Recording & Export Settings"
    bl_parent_id = "TRKAR_PT_take"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.trkar
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
        box.label(text=_t("Command to run after a take (integration)"), icon="SCRIPT")
        _draw_hook_settings(box, _prefs(context))




class TRKAR_PT_ndi_out(_TrkARSubPanel, bpy.types.Panel):
    bl_label = "NDI Output"
    bl_order = 4

    def draw_header(self, context):
        self.layout.label(icon="RENDER_ANIMATION")

    def draw_header_preset(self, context):
        running = _ndi_out_running()
        _header_toggle(self.layout, running, "trkar.ndi_out_start", "trkar.ndi_out_stop", _t("Sending") if running else "")

    def draw(self, context):
        layout = self.layout
        settings = context.scene.trkar
        running = _ndi_out_running()
        row = layout.row()
        row.enabled = not running
        row.prop(settings, "ndi_out_mode", expand=True)
        mode = settings.ndi_out_mode
        if running:
            # 動いているものを出す (止めずに送るものを切り替えたときも、何が動いているか分かるように)
            mode = "CAMERA" if ndi_output.running else ("SCREEN_OS" if os_screen_out.running else "SCREEN_BLENDER")
        if mode == "CAMERA":
            _draw_camera_out(layout, context, settings)
        else:
            _draw_screen_out(layout, context, settings, mode)


def _draw_camera_out(layout, context, settings):
    col = layout.column(align=True)
    col.enabled = not ndi_output.running
    col.prop(settings, "ndi_name")
    row = col.row(align=True)
    row.prop(settings, "ndi_width")
    row.prop(settings, "ndi_height")
    row.operator("trkar.ndi_swap_size", text="", icon="FILE_REFRESH")
    col.prop(settings, "ndi_fps")
    col.prop(settings, "ndi_transparent")
    if ndi_output.running:
        this_view = context.area is not None and context.area.as_pointer() == ndi_output.area_pointer
        layout.label(text=_t("Output source: %s") % (_t("this view") if this_view else _t("another view")))
        layout.label(text=_t("%d frames / receivers %d") % (ndi_output.frames_sent, ndi_output.sender.connections()))
        layout.label(text=_t("Draw %.1f fps / output %.1f fps (setting %.3g)") % (
            ndi_output.draw_rate.fps, ndi_output.send_rate.fps, ndi_output.fps))
        layout.label(text=_t("Draw %.1f ms / readback %.1f ms") % (ndi_output.draw_ms, ndi_output.read_ms))
        if ndi_output.last_error:
            layout.label(text=ndi_output.last_error, icon="ERROR")
    else:
        layout.label(text=_t("Drawn in this view's shading mode"), icon="INFO")


def _draw_screen_out(layout, context, settings, mode):
    os_mode = mode == "SCREEN_OS"
    output = os_screen_out if os_mode else screen_out
    if not (os_screen.supported() if os_mode else screen_output.supported()):
        layout.label(text=_t("Requires macOS 14 or later") if os_mode else _t("Available in Blender 5.3 or later"), icon="ERROR")
        return
    col = layout.column(align=True)
    col.enabled = not output.running
    col.prop(settings, "screen_ndi_name")
    col.prop(settings, "screen_ndi_target", text="")
    col.prop(settings, "screen_ndi_fps")
    col.prop(settings, "screen_ndi_half")
    if os_mode:
        col.prop(settings, "screen_ndi_cursor")
    if output.running and output.area_pointer:
        this_view = context.area is not None and context.area.as_pointer() == output.area_pointer
        layout.label(text=_t("Output source: %s") % (_t("this area") if this_view else _t("another area")))
    if os_mode and output.running:
        layout.label(text=_t("%d x %d / output %d fps (setting %.3g) / receivers %d") % (
            output.width, output.height, output.fps, settings.screen_ndi_fps, output.connections))
    elif output.running:
        layout.label(text=_t("%d x %d / %d frames / receivers %d") % (
            output.width, output.height, output.frames_sent, output.sender.connections()))
        layout.label(text=_t("Capture %.1f fps / output %.1f fps (setting %.3g)") % (
            output.capture_rate.fps, output.send_rate.fps, output.fps))
        layout.label(text=_t("Capture %.1f ms / send %.1f ms") % (output.capture_ms, output.send_ms))
        # 止まった (ボコッとなった) ときに、Blender で止まったのかを見分ける。ここが小さいのに受け手で止まるならネットワーク
        layout.label(text=_t("Longest gap (5 s): capture %.0f ms / output %.0f ms") % (
            output.capture_rate.max_gap_ms, output.send_rate.max_gap_ms))
    if output.last_error:
        for line in _wrap(output.last_error, 40):
            layout.label(text=line, icon="ERROR")
    if not output.running:
        if os_mode:
            layout.label(text=_t("Scene fps drops less (slightly more delay)"), icon="INFO")
        else:
            layout.label(text=_t("Lower delay (fps drops in heavy scenes)"), icon="INFO")
            layout.label(text=_t("The mouse cursor is not captured"))


def _wrap(text, width):
    return [text[i:i + width] for i in range(0, len(text), width)] or [""]


def _redraw_panels():
    try:
        # テイク再生中は録画したマスクをそのまま出す
        if not player.active:
            depth_in.idle_update(bpy.context.scene)
    except Exception as exc:
        depth_in.last_error = str(exc)
    # 受信件数などのステータス表示を更新する
    busy = (tracking.server.running or _ndi_out_running() or ndi_in.running or depth_in.running
            or recorder.recording or _export["running"] or hooks.busy() or matte_relay.running)
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
    view_controller.controller.stop()
    ndi_output.stop()
    ndi_in.stop()
    depth_in.stop()


def _migrate_once():
    _on_load_post()
    return None


@persistent
def _on_load_post(*_args):
    # 名前を ARtrk から変える前に保存した .blend を、そのまま使えるようにする (legacy.py)
    try:
        legacy.migrate()
    except Exception as exc:  # noqa: BLE001  引き継げなくても開くのは止めない
        print(_t("trkAR: can't carry over data with old names: %s") % exc)
    # 前の版で保存した .blend は、受信前の画像が黒 (不透明) で、板が CG を隠していた。透明に戻す
    try:
        image = bpy.data.images.get(ndi_input.IMAGE_NAME)
        if image is not None and image.source == "GENERATED" and tuple(image.generated_color) != ndi_input.BLANK_COLOR:
            ndi_input.blank_live_images((ndi_input.IMAGE_NAME,))
    except Exception as exc:  # noqa: BLE001
        print(_t("trkAR: can't make the pre-receive image transparent: %s") % exc)


@persistent
def _on_save_pre(*_args):
    # 受けている映像のコマが .blend に埋め込まれないようにする (ndi_input.blank_live_images)。
    # 深度スナップショット (trkAR Depth Snapshot) は自分で止めたコマなので残す
    ndi_input.blank_live_images((ndi_input.IMAGE_NAME, depth_input.MASK_IMAGE_NAME))
    # 再生中に動かした置き場を、そのテイクの take.json にも残す (再生を止めずに保存して閉じても失わない)
    try:
        player.save_placement()
    except Exception as exc:
        print(_t("trkAR: can't keep the take anchor: %s") % exc)


classes = (
    TRKAR_Preferences,
    TRKAR_Settings,
    TRKAR_OT_tracking_start,
    TRKAR_OT_tracking_stop,
    TRKAR_OT_copy_ip,
    TRKAR_OT_reanchor,
    TRKAR_OT_align_to_cursor,
    TRKAR_OT_ctl_reset,
    TRKAR_OT_ctl_keyframe,
    TRKAR_OT_ndi_swap_size,
    TRKAR_OT_ndi_start,
    TRKAR_OT_ndi_stop,
    TRKAR_OT_screen_ndi_start,
    TRKAR_OT_screen_ndi_stop,
    TRKAR_OT_os_screen_ndi_start,
    TRKAR_OT_ndi_out_start,
    TRKAR_OT_ndi_out_stop,
    TRKAR_OT_receive_start,
    TRKAR_OT_receive_stop,
    TRKAR_MT_ndi_sources,
    TRKAR_MT_depth_sources,
    TRKAR_OT_ndi_in_set_source,
    TRKAR_OT_ndi_in_start,
    TRKAR_OT_ndi_in_stop,
    TRKAR_OT_ndi_in_screen,
    TRKAR_OT_ndi_in_front,
    TRKAR_OT_ndi_in_camera_bg,
    TRKAR_OT_view_material,
    TRKAR_OT_depth_start,
    TRKAR_OT_depth_stop,
    TRKAR_OT_depth_screen,
    TRKAR_OT_depth_freeze,
    TRKAR_OT_take_rec_start,
    TRKAR_OT_slate_unset,
    TRKAR_OT_take_rec_stop,
    TRKAR_OT_take_play,
    TRKAR_OT_take_align_here,
    TRKAR_OT_take_live,
    TRKAR_OT_take_step,
    TRKAR_OT_take_anchor_add,
    TRKAR_OT_take_anchor_cursor,
    TRKAR_OT_take_anchor_previous,
    TRKAR_OT_take_anchor_remove,
    TRKAR_OT_take_render,
    TRKAR_OT_take_prepare,
    TRKAR_OT_take_open,
    TRKAR_OT_take_hook_run,
    TRKAR_OT_bridge_start,
    TRKAR_OT_bridge_stop,
    TRKAR_PT_panel,
    TRKAR_PT_tracking_place,
    TRKAR_PT_tracking_detail,
    TRKAR_PT_controls,
    TRKAR_PT_ndi_in_screen,
    TRKAR_PT_take,
    TRKAR_PT_take_slate,
    TRKAR_PT_take_anchor,
    TRKAR_PT_take_bridge,
    TRKAR_PT_take_settings,
    TRKAR_PT_ndi_out,
    TRKAR_OT_matte_start,
    TRKAR_OT_matte_stop,
)

# 社内向け (STUDIO のときだけ登録する。親パネルより後ろに置く)
studio_classes = ()
studio_early_classes = ()


def _scene_tab_panel(cls):
    """サイドバーのパネル cls と同じ中身で、プロパティエディターの「シーン」タブに出すパネル。
    プロパティエディターには新しいタブを足せないので、設定の置き場所 (scene.trkar) に合わせてシーンタブに置く"""
    attrs = {}
    for base in reversed(cls.__mro__):
        if base.__module__ == __name__:
            # bl_rna は登録したときに Blender が付ける印なので写さない (写すと登録済みと見なされる)
            attrs.update({k: v for k, v in vars(base).items() if not k.startswith("__") and k != "bl_rna"})
    attrs.pop("bl_category", None)
    attrs.update(bl_space_type="PROPERTIES", bl_region_type="WINDOW", bl_context="scene",
                 bl_idname=_scene_tab_id(cls.__name__))
    if "bl_parent_id" in attrs:
        attrs["bl_parent_id"] = _scene_tab_id(attrs["bl_parent_id"])
    return type(_scene_tab_id(cls.__name__), (bpy.types.Panel,), attrs)


def _scene_tab_id(name):
    return name.replace("TRKAR_PT_", "TRKAR_PT_scene_")


def _panels():
    return [cls for cls in classes + studio_classes if issubclass(cls, bpy.types.Panel)]


_ui = {"sidebar": False, "scene": []}


def _sync_sidebar(show):
    """3D ビューのサイドバーのパネルを出す / 外す (親を先に登録し、子を先に外す)。"""
    if show and not _ui["sidebar"]:
        for cls in _panels():
            bpy.utils.register_class(cls)
    elif not show and _ui["sidebar"]:
        for cls in reversed(_panels()):
            bpy.utils.unregister_class(cls)
    _ui["sidebar"] = show


def register():
    panels = set(_panels())
    for cls in studio_early_classes + classes + studio_classes:
        if cls not in panels:
            bpy.utils.register_class(cls)
    addon = bpy.context.preferences.addons.get(__package__)
    # 表示言語 (設定のクラスを登録したあとに読む)
    i18n.register(getattr(addon.preferences, "ui_language", "AUTO") if addon is not None else "AUTO")
    _sync_sidebar(addon.preferences.show_sidebar if addon is not None else True)
    _ui["scene"] = [_scene_tab_panel(cls) for cls in _panels()]
    for cls in _ui["scene"]:
        bpy.utils.register_class(cls)
    bpy.types.Scene.trkar = PointerProperty(type=TRKAR_Settings)
    view_controller.register()
    tracking.hold = lambda: player.active
    tracking.rec_hook = _on_remote_rec
    tracking.focus_hook = _autofocus_distance
    take.plate_hook = lambda _scene: _update_camera_screen()
    bpy.app.handlers.load_pre.append(_on_load_pre)
    bpy.app.handlers.load_post.append(_on_load_post)
    bpy.app.handlers.save_pre.append(_on_save_pre)
    # 有効にした時点で開いている .blend も (登録中は bpy.data を書けないことがあるのでタイマーで)
    bpy.app.timers.register(_migrate_once, first_interval=0.1)
    bpy.app.timers.register(_redraw_panels, persistent=True)
    bpy.app.timers.register(_update_camera_screen, persistent=True)


def unregister():
    i18n.unregister()
    if recorder.recording:
        recorder.stop(bpy.context)
    player.stop(bpy.context.scene)
    tracking.stop()
    tracking.hold = None
    tracking.rec_hook = None
    tracking.focus_hook = None
    take.plate_hook = None
    view_controller.controller.stop()
    ndi_output.stop()
    screen_out.stop()
    os_screen_out.stop()
    depth_in.stop()
    if bpy.app.timers.is_registered(_switch_to_matte):
        bpy.app.timers.unregister(_switch_to_matte)
    matte_relay.stop()
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
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    view_controller.unregister()
    del bpy.types.Scene.trkar
    for cls in reversed(_ui["scene"]):
        bpy.utils.unregister_class(cls)
    _ui["scene"] = []
    _sync_sidebar(False)
    panels = set(_panels())
    for cls in reversed(studio_early_classes + classes + studio_classes):
        if cls not in panels:
            bpy.utils.unregister_class(cls)
