"""3D ビューの右下に出す小さなコントローラー (trkAR の CTL と同じ動き)。

MOVE (前後左右)・CRANE (上下)・PAN (横)・TILT (縦) を、マウスでつまんで倒している間、
倒した量の速さで動かし続ける (離すと止まる)。速さは S / M / F (倒し切ったとき 0.25 / 1 / 4 m/s、15 / 45 / 120 度/s)。
キーボード (KEY が入っていて、マウスがこの 3D ビューの上にあるとき): W / S 前後、A / D 左右、E / Q 上下、
← / → パン、↑ / ↓ チルト、1 / 2 / 3 速さ。押している間は倒し切ったのと同じで、マウスのスティックと足す。

- 受信中 (iPhone の姿勢が届いている) は「Blender でオフセット」に積算する (tracking.local_stick。リモートと同じ)
- 受信していないときは、カメラのオブジェクトを直接動かす
ROT RST (向きだけ) / ALL RST (全部) で戻す (受信中はオフセット、受信していないときは動かし始める前のカメラ)。
コントローラーの外のクリックやキーは、そのまま Blender に渡す。

置き場所は「ビューの右下」(上のとおり描く) か「パネルの中」(Blender でオフセットのパネルに、つまむと 0 に戻る
スライダーで出す)。パネルのスライダーを離したことは Blender から直接は届かないので、つまんでいる間は
マウスの動きがスライダーだけに届くこと (Blender が操作中のボタンにイベントを独り占めさせる) を使い、
離したあと最初にマウスの動きがこちらに届いたら 0 に戻す。カメラを動かすのは、つまんでいる間も止まらない
bpy.app.timers で回す (モーダルのタイマーのイベントもつまんでいる間は届かないため)。
"""

import math
import time

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Quaternion, Vector

from . import tracking
try:
    from .i18n import _t
except ImportError:  # アドオンの外から単独で読まれたとき (render_clay.py・GTN のノードなど)。訳さない
    def _t(message):
        return message

STEP_INTERVAL = 1.0 / 60.0

SPEEDS = (("S", 0.25, 15.0), ("M", 1.0, 45.0), ("F", 4.0, 120.0))
# キーボード: キー → (軸, 向き)。軸は move_x / move_y / crane / look_x / look_y
KEYS = {"W": ("move_y", 1), "S": ("move_y", -1), "D": ("move_x", 1), "A": ("move_x", -1),
        "E": ("crane", 1), "Q": ("crane", -1), "RIGHT_ARROW": ("look_x", 1), "LEFT_ARROW": ("look_x", -1),
        "UP_ARROW": ("look_y", 1), "DOWN_ARROW": ("look_y", -1)}
# レンズの段: F 値のボタン (0 = 上書きしない、-1 = 被写界深度を切る)、焦点距離とピントのスライダーの範囲
FSTOPS = ((0.0, "—"), (-1.0, "OFF"), (1.4, "1.4"), (2.0, "2"), (2.8, "2.8"), (4.0, "4"), (5.6, "5.6"), (8.0, "8"),
          (16.0, "16"))
FOCAL_RANGE = (12.0, 200.0)
FOCUS_RANGE = (0.2, 50.0)
SPEED_KEYS = {"ONE": 0, "TWO": 1, "THREE": 2, "NUMPAD_1": 0, "NUMPAD_2": 1, "NUMPAD_3": 2}
ACCENT = (0.36, 0.82, 1.0, 0.9)
DIM = (0.36, 0.82, 1.0, 0.35)
BASE = (0.05, 0.06, 0.07, 0.45)


class Controller:
    def __init__(self):
        self.area_pointer = 0
        self.handle = None
        # マウスのスティック (二乗カーブ済み)
        self.mouse_move = (0.0, 0.0)
        self.mouse_crane = 0.0
        self.mouse_look = (0.0, 0.0)
        self.roll = 0.0
        # キーボードで押しているキー (KEYS のキー名)
        self.keys = set()
        # レンズの段 (焦点距離・F 値・ピント) を開いているか
        self.lens_open = False
        # 受信していないときに直接動かす前のカメラ (ALL RST で戻す所)
        self._origin = None
        self.dragging = None
        self._last = None
        self._draw_fn = self.draw
        self._step_fn = self._step

    # --- 速さ・キー操作・置き場所 (パネルのボタンと右下の絵の両方から変えるので WindowManager のプロパティに置く)

    @staticmethod
    def _props():
        return bpy.context.window_manager.trkar_stick

    def _get_speed(self):
        return int(self._props().speed)

    def _set_speed(self, value):
        self._props().speed = str(value)

    speed = property(_get_speed, _set_speed)

    def _get_keyboard(self):
        return self._props().keyboard

    def _set_keyboard(self, value):
        self._props().keyboard = value

    keyboard = property(_get_keyboard, _set_keyboard)

    @property
    def in_panel(self):
        return self._props().place == "PANEL"

    # --- tracking.integrate_controls が読む値 (マウスのスティックとキーボードを足して -1...1)

    def _key(self, axis):
        return sum(sign for key, (name, sign) in KEYS.items() if name == axis and key in self.keys)

    @property
    def move(self):
        return (_clamp(self.mouse_move[0] + self._key("move_x")), _clamp(self.mouse_move[1] + self._key("move_y")))

    @move.setter
    def move(self, value):
        self.mouse_move = value

    @property
    def crane(self):
        return _clamp(self.mouse_crane + self._key("crane"))

    @crane.setter
    def crane(self, value):
        self.mouse_crane = value

    def _get_look(self):
        return (_clamp(self.mouse_look[0] + self._key("look_x")), _clamp(self.mouse_look[1] + self._key("look_y")))

    def _set_look(self, value):
        self.mouse_look = value

    # (パン, チルト)。デコレーターの書き方は公開版の検査 (社内向けモジュール名) に掛かるので property() で
    look = property(_get_look, _set_look)

    @property
    def active(self):
        return any((self.move[0], self.move[1], self.crane, self.look[0], self.look[1]))

    @property
    def move_speed(self):
        return SPEEDS[self.speed][1]

    @property
    def rotate_speed(self):
        return SPEEDS[self.speed][2]

    @property
    def running(self):
        return self.handle is not None

    def start(self, area):
        self.stop()
        self.area_pointer = area.as_pointer()
        self.handle = bpy.types.SpaceView3D.draw_handler_add(self._draw_fn, (), "WINDOW", "POST_PIXEL")
        tracking.local_stick = self
        if not bpy.app.timers.is_registered(self._step_fn):
            bpy.app.timers.register(self._step_fn, first_interval=STEP_INTERVAL)
        area.tag_redraw()

    def stop(self):
        if self.handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self.handle, "WINDOW")
            self.handle = None
        if bpy.app.timers.is_registered(self._step_fn):
            bpy.app.timers.unregister(self._step_fn)
        if tracking.local_stick is self:
            tracking.local_stick = None
        self.release_panel()
        self.release()
        self.release_keys()
        self._last = None
        self._origin = None
        self.area_pointer = 0

    def release(self):
        """マウスのスティックを離した (キーボードはそのまま)。"""
        self.move, self.crane, self.look, self.dragging = (0.0, 0.0), 0.0, (0.0, 0.0), None

    def release_keys(self):
        self.keys.clear()

    def release_panel(self):
        """パネルのスライダーを 0 に戻す (update で mouse_* も 0 になる)。戻したら True。"""
        try:
            props = self._props()
        except AttributeError:
            return False
        if not any(getattr(props, name) for name in STICK_NAMES):
            return False
        for name in STICK_NAMES:
            if getattr(props, name):
                setattr(props, name, 0.0)
        return True

    def stick_from_panel(self, props):
        """パネルのスライダーが動いた (つまんでいる間、動かすたびに来る)。"""
        self.mouse_move = (_curve(props.move_x), _curve(props.move_y))
        self.mouse_crane = _curve(props.crane)
        self.mouse_look = (_curve(props.pan), _curve(props.tilt))

    def _step(self):
        """カメラを動かす・絵を描き直す (つまんでいる間も回る)。"""
        if not self.running:
            return None
        try:
            self.step_camera(bpy.context)
        except (AttributeError, ReferenceError):
            return STEP_INTERVAL
        if self.dragging is not None or self.keys or self.active:
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.as_pointer() == self.area_pointer or (self.in_panel and area.type in ("VIEW_3D", "PROPERTIES")):
                        area.tag_redraw()
        return STEP_INTERVAL

    # --- 置き場所 (リージョンの画素、左下が原点)

    def layout(self, region, area):
        """右下に置く。サイドバー (N) が 3D ビューに重なって出ているときは、その左に寄せる。
        下の段: MOVE (スティック)・CRANE (縦)・PAN (横)・TILT (縦)。その上: S / M / F・KEY・×。いちばん上: ROT RST・ALL RST"""
        ui = bpy.context.preferences.system.ui_scale
        m, r, gap, bar = 16 * ui, 44 * ui, 12 * ui, 22 * ui
        width = 4 * r + 2 * bar + 3 * gap
        inset = 0
        sidebar = next((a for a in area.regions if a.type == "UI"), None)
        if sidebar is not None and sidebar.width > 1 and bpy.context.preferences.system.use_region_overlap:
            inset = sidebar.width
        left = region.width - inset - m - width
        base = m + 20 * ui
        move = (left + r, base + r)
        crane = (left + 2 * r + gap, base, bar, 2 * r)                          # x, y, 幅, 高さ
        pan = (crane[0] + bar + gap, base + r - bar / 2, 2 * r, bar)
        tilt = (pan[0] + 2 * r + gap, base, bar, 2 * r)
        row = base + 2 * r + 10 * ui
        speeds = [(left + i * 30 * ui, row, 26 * ui, 20 * ui) for i in range(len(SPEEDS))]
        close = (left + width - 20 * ui, row, 20 * ui, 20 * ui)
        key = (close[0] - 40 * ui, row, 34 * ui, 20 * ui)
        lens = (key[0] - 46 * ui, row, 40 * ui, 20 * ui)
        top = row + 26 * ui
        resets = {"rotation": (left, top, 60 * ui, 20 * ui), "all": (left + 66 * ui, top, 60 * ui, 20 * ui)}
        lay = {"ui": ui, "r": r, "move": move, "sliders": {"crane": (crane, True), "pan": (pan, False),
                                                          "tilt": (tilt, True)},
               "speeds": speeds, "close": close, "key": key, "lens": lens, "resets": resets, "top": top, "left": left,
               "width": width}
        if self.lens_open:
            # レンズの段 (下から: ピント・F 値・焦点距離・今の値)。キーの案内の上に積む
            y = top + 26 * ui + (16 * ui if self.keyboard else 0)
            label, chip = 46 * ui, 30 * ui
            slider = lambda y: (left + label, y + 3 * ui, width - label - chip - 6 * ui, 14 * ui)
            lay["focus"] = (slider(y), (left + width - chip, y, chip, 20 * ui), y)
            y += 26 * ui
            step = width / len(FSTOPS)
            lay["fstops"] = [(left + i * step, y, step - 3 * ui, 20 * ui) for i in range(len(FSTOPS))]
            y += 26 * ui
            lay["focal"] = (slider(y), (left + width - chip, y, chip, 20 * ui), y)
            lay["lens_info"] = y + 26 * ui
        return lay

    # --- マウス

    def handle_event(self, event, region, area):
        """モーダルから。コントローラーで受けたら True (Blender には渡さない)。"""
        x, y = event.mouse_x - region.x, event.mouse_y - region.y
        lay = self.layout(region, area)
        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            for i, rect in enumerate(lay["speeds"]):
                if _inside(rect, x, y):
                    self.speed = i
                    return True
            if _inside(lay["close"], x, y):
                self.stop()
                return True
            if _inside(lay["key"], x, y):
                self.keyboard = not self.keyboard
                self.release_keys()
                return True
            if _inside(lay["lens"], x, y):
                self.lens_open = not self.lens_open
                return True
            if self.lens_open and self._lens_press(x, y, lay):
                return True
            for name, rect in lay["resets"].items():
                if _inside(rect, x, y):
                    self.reset(bpy.context, rotation_only=name == "rotation")
                    return True
            cx, cy = lay["move"]
            if math.hypot(x - cx, y - cy) <= lay["r"]:
                self.dragging = "move"
                self._drag(x, y, lay)
                return True
            for name, (rect, _vertical) in lay["sliders"].items():
                if _inside(rect, x, y):
                    self.dragging = name
                    self._drag(x, y, lay)
                    return True
            return False
        if self.dragging is None:
            return False
        if event.type == "MOUSEMOVE":
            self._drag(x, y, lay)
            return True
        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            self.release()
            return True
        return False

    def handle_key(self, event):
        """モーダルから (マウスがこの 3D ビューの上のとき)。キーボードで受けたら True。"""
        if not self.keyboard:
            return False
        if event.type in SPEED_KEYS and event.value == "PRESS":
            self.speed = SPEED_KEYS[event.type]
            return True
        if event.type not in KEYS:
            return False
        if event.value == "PRESS":
            self.keys.add(event.type)
        elif event.value == "RELEASE":
            self.keys.discard(event.type)
        return True

    # --- レンズ (Blender でオフセットの焦点距離・F 値・ピント、と オートフォーカス)

    def _lens_press(self, x, y, lay):
        settings = bpy.context.scene.trkar
        for (value, _label), rect in zip(FSTOPS, lay["fstops"]):
            if _inside(rect, x, y):
                settings.ctl_fstop = value
                self._lens_changed()
                return True
        for name in ("focal", "focus"):
            slider, chip, _y = lay[name]
            if _inside(chip, x, y):
                if name == "focal":
                    settings.ctl_lens = 0.0  # AR: iPhone の画角 / レンズ設定に戻す
                else:
                    settings.dof_autofocus = not settings.dof_autofocus
                self._lens_changed()
                return True
            sx, sy, sw, sh = slider
            if sx <= x <= sx + sw and sy - 4 <= y <= sy + sh + 4:
                self.dragging = name
                self._drag(x, y, lay)
                return True
        return False

    def _lens_changed(self):
        """受信していないときは、カメラのレンズにすぐ当てる (受信中は tracking が毎回当てる)。"""
        if tracking.current_pose() is not None:
            return
        settings = bpy.context.scene.trkar
        camera = settings.camera or bpy.context.scene.camera
        if camera is not None and camera.type == "CAMERA":
            tracking.apply_lens(settings, camera.data)

    def lens_values(self):
        """今のカメラの (焦点距離 mm, 被写界深度, F 値, ピント距離)。"""
        settings = bpy.context.scene.trkar
        camera = settings.camera or bpy.context.scene.camera
        if camera is None or camera.type != "CAMERA":
            return 0.0, False, 0.0, 0.0
        cam = camera.data
        return cam.lens, cam.dof.use_dof, cam.dof.aperture_fstop, cam.dof.focus_distance

    def _drag(self, x, y, lay):
        if self.dragging in ("focal", "focus"):
            (sx, _sy, sw, _sh), _chip, _y = lay[self.dragging]
            t = _clamp((x - sx) / sw * 2 - 1) * 0.5 + 0.5
            low, high = FOCAL_RANGE if self.dragging == "focal" else FOCUS_RANGE
            value = math.exp(math.log(low) + (math.log(high) - math.log(low)) * t)
            settings = bpy.context.scene.trkar
            if self.dragging == "focal":
                settings.ctl_lens = round(value)
            else:
                # ピントを触ったらオートフォーカスを切って、ここからはこの値で合わせる (trkAR の LENS と同じ)
                settings.dof_autofocus = False
                settings.ctl_focus = round(value, 2)
            self._lens_changed()
            return
        if self.dragging == "move":
            cx, cy = lay["move"]
            r = lay["r"]
            dx, dy = (x - cx) / r, (y - cy) / r
            length = math.hypot(dx, dy)
            if length > 1.0:
                dx, dy = dx / length, dy / length
            self.move = (_curve(dx), _curve(dy))
            return
        (rx, ry, w, h), vertical = lay["sliders"][self.dragging]
        value = _curve(_clamp((y - (ry + h / 2)) / (h / 2) if vertical else (x - (rx + w / 2)) / (w / 2)))
        if self.dragging == "crane":
            self.crane = value
        elif self.dragging == "pan":
            self.look = (value, self.mouse_look[1])
        else:
            self.look = (self.mouse_look[0], value)

    # --- リセット

    def reset(self, context, rotation_only):
        """ROT RST (向きだけ) / ALL RST (全部)。受信中は「Blender でオフセット」を、受信していないときは
        カメラをコントローラーで動かし始める前の位置・向きに戻す。"""
        settings = context.scene.trkar
        if tracking.current_pose() is not None:
            # 向きだけ戻すときは、パンの補正が合わせるのでカメラはその場で向きだけ戻る
            settings.ctl_pan = settings.ctl_tilt = settings.ctl_roll = 0.0
            if not rotation_only:
                settings.ctl_location = (0.0, 0.0, 0.0)
                tracking.reset_pan_shift(settings)
            return
        camera = settings.camera or context.scene.camera
        if camera is None or self._origin is None:
            return
        if rotation_only:
            location, _rotation, scale = camera.matrix_world.decompose()
            camera.matrix_world = Matrix.LocRotScale(location, self._origin.to_quaternion(), scale)
        else:
            camera.matrix_world = self._origin.copy()

    # --- 受信していないとき: カメラを直接動かす (モーダルのタイマーから)

    def step_camera(self, context):
        now = time.monotonic()
        last, self._last = self._last, now
        if context.scene.trkar.dof_autofocus and tracking.current_pose() is None:
            self._lens_changed()  # オートフォーカスをじんわり寄せる (受信していないとき)
        if last is None or not self.active or tracking.current_pose() is not None:
            return
        dt = min(now - last, 0.1)
        settings = context.scene.trkar
        camera = settings.camera or context.scene.camera
        if camera is None:
            return
        if self._origin is None:
            self._origin = camera.matrix_world.copy()  # ALL RST で戻す所
        location, rotation, scale = camera.matrix_world.decompose()
        forward = rotation @ Vector((0.0, 0.0, -1.0))
        forward.z = 0.0
        forward = forward.normalized() if forward.length > 1e-6 else Vector((0.0, 1.0, 0.0))
        right = Vector((forward.y, -forward.x, 0.0))
        location += (right * self.move[0] + forward * self.move[1] + Vector((0.0, 0.0, self.crane))) \
            * self.move_speed * dt * settings.scale
        turn = math.radians(self.rotate_speed * dt)
        rotation = Quaternion((0.0, 0.0, 1.0), -self.look[0] * turn) @ rotation
        rotation = rotation @ Quaternion((1.0, 0.0, 0.0), self.look[1] * turn)
        camera.matrix_world = Matrix.LocRotScale(location, rotation, scale)

    # --- 描画

    def draw(self):
        area = bpy.context.area
        if area is None or area.as_pointer() != self.area_pointer or self.in_panel:
            return
        lay = self.layout(bpy.context.region, area)
        ui, r = lay["ui"], lay["r"]
        shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        gpu.state.blend_set("ALPHA")
        gpu.state.line_width_set(max(1.0, ui))
        cx, cy = lay["move"]
        _disc(shader, cx, cy, r, BASE)
        _ring(shader, cx, cy, r, ACCENT)
        _lines(shader, [(cx - r + 6 * ui, cy), (cx + r - 6 * ui, cy), (cx, cy - r + 6 * ui), (cx, cy + r - 6 * ui)], DIM)
        kx, ky = _uncurve(self.mouse_move[0]), _uncurve(self.mouse_move[1])
        _disc(shader, cx + kx * (r - 14 * ui), cy + ky * (r - 14 * ui), 14 * ui,
              ACCENT if self.dragging == "move" else DIM)
        values = {"crane": self.mouse_crane, "pan": self.mouse_look[0], "tilt": self.mouse_look[1]}
        for name, ((x, y, w, h), vertical) in lay["sliders"].items():
            _rect(shader, x, y, w, h, BASE)
            _rect_lines(shader, x, y, w, h, ACCENT)
            knob = (w if vertical else h) / 2 - 2 * ui
            offset = _uncurve(values[name]) * ((h if vertical else w) / 2 - knob - 2 * ui)
            px, py = (x + w / 2, y + h / 2 + offset) if vertical else (x + w / 2 + offset, y + h / 2)
            _disc(shader, px, py, knob, ACCENT if self.dragging == name else DIM)
        buttons = [(rect, i == self.speed) for i, rect in enumerate(lay["speeds"])]
        buttons += [(lay["key"], self.keyboard), (lay["lens"], self.lens_open)]
        buttons += [(rect, False) for rect in lay["resets"].values()]
        settings = bpy.context.scene.trkar
        if self.lens_open:
            for (value, _label), rect in zip(FSTOPS, lay["fstops"]):
                buttons.append((rect, abs(settings.ctl_fstop - value) < 1e-3))
            buttons.append((lay["focal"][1], settings.ctl_lens == 0.0))
            buttons.append((lay["focus"][1], settings.dof_autofocus))
            focal, _dof, _fstop, focus = self.lens_values()
            for name, value, (low, high) in (("focal", settings.ctl_lens or focal, FOCAL_RANGE),
                                             ("focus", focus, FOCUS_RANGE)):
                (sx, sy, sw, sh), _chip, _y = lay[name]
                _rect(shader, sx, sy, sw, sh, BASE)
                _rect_lines(shader, sx, sy, sw, sh, ACCENT)
                t = (math.log(max(low, min(high, value))) - math.log(low)) / (math.log(high) - math.log(low)) if value > 0 else 0
                _disc(shader, sx + t * sw, sy + sh / 2, sh / 2 + 2 * ui, ACCENT if self.dragging == name else DIM)
        for (bx, by, bw, bh), on in buttons:
            _rect(shader, bx, by, bw, bh, ACCENT if on else BASE)
            _rect_lines(shader, bx, by, bw, bh, ACCENT)
        bx, by, bw, bh = lay["close"]
        _rect(shader, bx, by, bw, bh, BASE)
        _lines(shader, [(bx + 5 * ui, by + 5 * ui), (bx + bw - 5 * ui, by + bh - 5 * ui),
                        (bx + 5 * ui, by + bh - 5 * ui), (bx + bw - 5 * ui, by + 5 * ui)], ACCENT)
        gpu.state.blend_set("NONE")
        font = 0
        blf.size(font, 10 * ui)
        dark = (0.05, 0.06, 0.07, 1.0)
        _text(font, "MOVE", cx - blf.dimensions(font, "MOVE")[0] / 2, cy - r - 14 * ui, DIM)
        for name, ((x, y, w, h), vertical) in lay["sliders"].items():
            label = name.upper()
            _text(font, label, x + w / 2 - blf.dimensions(font, label)[0] / 2,
                  (y - 14 * ui) if vertical else (y - 14 * ui - (r - h / 2)), DIM)
        labels = [(label, rect, i == self.speed) for i, ((label, _m, _r), rect) in enumerate(zip(SPEEDS, lay["speeds"]))]
        labels += [("KEY", lay["key"], self.keyboard), ("LENS", lay["lens"], self.lens_open),
                   ("ROT RST", lay["resets"]["rotation"], False), ("ALL RST", lay["resets"]["all"], False)]
        if self.lens_open:
            labels += [(label, rect, abs(settings.ctl_fstop - value) < 1e-3)
                       for (value, label), rect in zip(FSTOPS, lay["fstops"])]
            labels += [("AR", lay["focal"][1], settings.ctl_lens == 0.0), ("AF", lay["focus"][1], settings.dof_autofocus)]
            for name in ("focal", "focus"):
                _text(font, name.upper(), lay["left"], lay[name][2] + 6 * ui, DIM)
            focal, dof, fstop, focus = self.lens_values()
            info = "%.0fmm  %s" % (focal, ("F%.1f @%.2fm%s" % (fstop, focus, " AF" if settings.dof_autofocus else ""))
                                   if dof else "DOF OFF")
            _text(font, info, lay["left"], lay["lens_info"], ACCENT)
        for label, (bx, by, bw, bh), on in labels:
            tw, th = blf.dimensions(font, label)
            _text(font, label, bx + (bw - tw) / 2, by + (bh - th) / 2, dark if on else ACCENT)
        mode = _t("Offset") if tracking.current_pose() is not None else _t("Camera directly")
        rx, ry, rw, rh = lay["resets"]["all"]
        _text(font, mode, rx + rw + 8 * ui, ry + 5 * ui, DIM)
        if self.keyboard:
            _text(font, "WASD / QE / ←→↑↓ / 1-3", lay["left"], lay["top"] + 26 * ui, DIM)

controller = Controller()


def _clamp(v):
    return max(-1.0, min(1.0, v))


def _inside(rect, x, y):
    rx, ry, w, h = rect
    return rx <= x <= rx + w and ry <= y <= ry + h


def _curve(v):
    return v * abs(v)


def _uncurve(v):
    return math.copysign(math.sqrt(abs(v)), v)


def _circle(cx, cy, r, n=32):
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]


def _disc(shader, cx, cy, r, color):
    points = [(cx, cy)] + _circle(cx, cy, r) + [(cx + r, cy)]
    _batch(shader, "TRI_FAN", points, color)


def _ring(shader, cx, cy, r, color):
    _batch(shader, "LINE_LOOP", _circle(cx, cy, r), color)


def _rect(shader, x, y, w, h, color):
    _batch(shader, "TRI_FAN", [(x, y), (x + w, y), (x + w, y + h), (x, y + h)], color)


def _rect_lines(shader, x, y, w, h, color):
    _batch(shader, "LINE_LOOP", [(x, y), (x + w, y), (x + w, y + h), (x, y + h)], color)


def _lines(shader, points, color):
    _batch(shader, "LINES", points, color)


def _batch(shader, kind, points, color):
    batch = batch_for_shader(shader, kind, {"pos": points})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _text(font, text, x, y, color):
    blf.color(font, *color)
    blf.position(font, x, y, 0)
    blf.draw(font, text)


class TRKAR_OT_view_controller(bpy.types.Operator):
    bl_idname = "trkar.view_controller"
    bl_label = "View Controller"
    bl_description = ("Show a small controller (MOVE / CRANE / PAN / TILT) at the bottom right of the 3D view. Press again or × to close. While receiving it moves the \"Blender Offset\"; otherwise it moves the camera directly")

    def invoke(self, context, _event):
        if controller.running:
            controller.stop()
            return {"FINISHED"}
        from . import _view3d  # 3D ビューの外 (プロパティエディター) から押されたときの 3D ビュー
        window, area = _view3d(context)
        if area is None:
            self.report({"ERROR"}, _t("No 3D view"))
            return {"CANCELLED"}
        controller.start(area)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if not controller.running:
            return self._finish(context)
        area = next((a for w in context.window_manager.windows for a in w.screen.areas
                     if a.as_pointer() == controller.area_pointer), None)
        if area is None:
            controller.stop()
            return self._finish(context)
        if event.type in ("MOUSEMOVE", "LEFTMOUSE", "WINDOW_DEACTIVATE") and controller.release_panel():
            # パネルのスライダーを離した (つまんでいる間はマウスの動きがここまで届かない)
            area.tag_redraw()
        if event.type == "WINDOW_DEACTIVATE":
            controller.release_keys()
            return {"PASS_THROUGH"}
        region = next((r for r in area.regions if r.type == "WINDOW"), None)
        inside = region is not None and region.x <= event.mouse_x < region.x + region.width \
            and region.y <= event.mouse_y < region.y + region.height
        # キーを離したのはどこでも受ける (押したまま外へ出たとき止まるように)。押すのはこの 3D ビューの上だけ
        if (event.value == "RELEASE" and event.type in controller.keys) or (
                inside and event.type in KEYS or inside and event.type in SPEED_KEYS):
            if not event.is_repeat and controller.handle_key(event):
                area.tag_redraw()
                return {"RUNNING_MODAL"}
            if event.is_repeat and controller.keyboard and event.type in KEYS:
                return {"RUNNING_MODAL"}  # 押しっぱなしの繰り返しも Blender に渡さない
        if controller.in_panel:
            return {"PASS_THROUGH"}
        if region is not None and (inside or controller.dragging is not None) and controller.handle_event(event, region, area):
            area.tag_redraw()
            if not controller.running:
                return self._finish(context)
            return {"RUNNING_MODAL"}
        return {"PASS_THROUGH"}

    def _finish(self, context):
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
        return {"FINISHED"}


# --- パネルの中に出すとき

STICK_NAMES = ("move_y", "move_x", "crane", "pan", "tilt")
STICK_LABELS = {"move_y": "Forward / Back", "move_x": "Left / Right", "crane": "Up / Down (CRANE)", "pan": "Pan", "tilt": "Tilt"}


def _stick_update(self, _context):
    controller.stick_from_panel(self)


def _place_update(_self, context):
    controller.release()
    controller.release_panel()
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _stick(name):
    return bpy.props.FloatProperty(name=STICK_LABELS[name], default=0.0, min=-1.0, max=1.0, update=_stick_update,
                                   description=_t("Keeps moving at a speed set by how far you drag it while you hold it (returns to 0 and stops when released)"))


class TRKAR_StickProps(bpy.types.PropertyGroup):
    place: bpy.props.EnumProperty(
        name="Placement", default="PANEL", update=_place_update,
        items=(("PANEL", "In the Panel", "Show it as sliders in this panel"),
               ("VIEW", "View Bottom Right", "Draw sticks at the bottom right of the 3D view")))
    speed: bpy.props.EnumProperty(
        name="Speed", default="1",
        items=[(str(i), label, "At full tilt: %g m/s, %g°/s" % (move, turn))
               for i, (label, move, turn) in enumerate(SPEEDS)])
    keyboard: bpy.props.BoolProperty(
        name="Keyboard", default=True,
        description="While the mouse is over the 3D view: W / S forward / back, A / D left / right, E / Q up / down, ← / → pan, ↑ / ↓ tilt, 1 / 2 / 3 speed")
    move_y: _stick("move_y")
    move_x: _stick("move_x")
    crane: _stick("crane")
    pan: _stick("pan")
    tilt: _stick("tilt")


class TRKAR_OT_view_reset(bpy.types.Operator):
    bl_idname = "trkar.view_reset"
    bl_label = "Reset with the Controller"
    bl_description = "While receiving, resets the \"Blender Offset\"; otherwise returns the camera to where it was before the controller moved it"
    target: bpy.props.EnumProperty(items=(("ROTATION", "Rotation Only", ""), ("ALL", "All", "")))

    def execute(self, context):
        controller.reset(context, rotation_only=self.target == "ROTATION")
        return {"FINISHED"}


def draw_panel(layout, context):
    """「Blender でオフセット」のパネルの中のコントローラー (開いていて、置き場所がパネルのとき)。"""
    props = context.window_manager.trkar_stick
    if not controller.running:
        return
    layout.row(align=True).prop(props, "place", expand=True)
    if props.place != "PANEL":
        return
    box = layout.box()
    row = box.row(align=True)
    row.prop(props, "speed", expand=True)
    row.prop(props, "keyboard", text="KEY", toggle=True)
    col = box.column(align=True)
    for name in STICK_NAMES:
        col.prop(props, name, slider=True)
    row = box.row(align=True)
    row.operator("trkar.view_reset", text="ROT RST").target = "ROTATION"
    row.operator("trkar.view_reset", text="ALL RST").target = "ALL"
    row.prop(context.scene.trkar, "dof_autofocus", text="AF", toggle=True)


classes = (TRKAR_StickProps, TRKAR_OT_view_reset, TRKAR_OT_view_controller)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.trkar_stick = bpy.props.PointerProperty(type=TRKAR_StickProps)


def unregister():
    controller.stop()
    del bpy.types.WindowManager.trkar_stick
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
