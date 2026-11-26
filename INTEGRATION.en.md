# trkAR Integration Spec

This is the English version of INTEGRATION.md (Japanese). If they differ, the Japanese version is authoritative.

This spec describes how to connect trkAR (iPhone / iPad app) and trkAR Camera (Blender add-on) to your own cloud or tools.
trkAR itself sends nothing to any cloud. Integrations with S3, Google Drive, Frame.io, an in-house NAS, and so on are
built by you, using the **integration points** described here. It is written in enough detail that you can give this file
to a coding agent such as Claude and ask it to "write a script that connects this to my ○○" (see also `CLAUDE.md`).

- Spec version: 1 (same as the hook's `TRKAR_HOOK_VERSION`). Fields may be added, but their meaning will not change
- Applies to: trkAR Camera 0.9.11 or later, trkAR 1.1 or later (for combinations, see [section 5](#5-app-and-add-on-compatibility))

There are four integration points.

| Integration point | Direction | Use it to |
| --- | --- | --- |
| [1. After-take command (hook)](#1-after-take-command-hook) | Blender → your script | Upload recorded takes or exported videos somewhere, send notifications |
| [2. Take folder](#2-take-folder) | Files | The data layout read by hooks and other tools |
| [3. Automation Port](#3-automation-port) | Your tool → Blender | Open scenes, select takes, and export from outside |
| [4. Data from the iPhone](#4-data-from-the-iphone-osc-ndi-udp) | iPhone → PC | Receive directly in TouchDesigner or similar, without Blender |

Version combinations of the app and add-on are summarized in [section 5](#5-app-and-add-on-compatibility).

---

## 1. After-Take Command (Hook)

Set it in Blender under `Edit > Preferences > Add-ons > trkAR Camera` in "After-Take Command (Integration)", or in
the sidebar under `Recording & Takes > Recording & Export Settings`.

| Setting | Default | Meaning |
| --- | --- | --- |
| Command | (empty) | The command to run. If empty, nothing runs |
| After Recording Stops | On | Run on `recorded` |
| After Export | On | Run on `exported` |
| After Generation Inputs | On | Run on `for_generation` (0.11.9+) |
| Timeout (min) | 30 | Stop the command after this time |

### When it runs

| event | Timing | What exists at that point |
| --- | --- | --- |
| `recorded` | When recording (REC) stops. Same when stopped with the iPhone's REC button | `take.json`, `actions.json`, `video/`, and (if present) `alpha/`, `depth/` |
| `exported` | When a take export finishes completely (does not run if the export is stopped partway) | The above plus `render.mp4` or `render/`. `render` is added to `take.json` |
| `for_generation` | When Export Generation Inputs finishes (0.11.9+) | `for_generation/` (section 6) |

"Run Command" in the panel runs it again for the selected take (`exported` if it has been exported, otherwise `recorded`).

### Writing the command

- A single-line string. The placeholders below are substituted, then the string is split into arguments on whitespace and run (no shell is used.
  If you need pipes, redirection, or `&&`, put them inside a script). Enclose paths containing spaces in `"..."`

  | Placeholder | Replaced with |
  | --- | --- |
  | `{take_dir}` | Absolute path of the take folder |
  | `{event}` | `recorded`, `exported` or `for_generation` |
  | `{render}` | Path of `render.mp4`. For a PNG sequence, the path of `render/`. Empty string if none |
  | `{take_json}` | Path of `take.json` |
  | `{for_generation}` | Path of `for_generation/` (section 6). Empty string if none |

- `~` is expanded to your home folder
- Example: `python3 ~/trkar_upload.py {take_dir} {event}`

### What is passed

The working directory is the take folder. The following are added to the parent (Blender) environment variables and passed on.

| Environment variable | Content |
| --- | --- |
| `TRKAR_HOOK_VERSION` | Version of this spec (`1`) |
| `TRKAR_EVENT` | `recorded` / `exported` / `for_generation` |
| `TRKAR_TAKE_DIR` | Take folder |
| `TRKAR_TAKE_JSON` | Path of `take.json` |
| `TRKAR_RENDER` | Path of `render.mp4` or `render/`. Empty if none |
| `TRKAR_FOR_GENERATION` | Path of `for_generation/`. Empty if none |
| `TRKAR_BLEND` | Path of the open .blend (empty if not saved) |
| `TRKAR_ADDON_VERSION` | Add-on version (e.g. `0.9.11`) |

Blender launched from Finder or the Dock has a short `PATH`, so `/opt/homebrew/bin`, `/usr/local/bin`, and `~/.local/bin` are
appended to the end of `PATH`. Write commands in other locations with their full path.

### Returning results

- **Exit code 0 means success**; anything else is a failure and is shown in red in the panel
- If **the last line of standard output is a JSON object**, its `message` (short description) and `url` (URL to open the result) are shown in the panel.
  If `url` is present, an "Open" button appears in the panel
  ```json
  {"message": "231 files to S3", "url": "https://example.com/takes/take_20261021_120000"}
  ```
- Standard output and standard error are appended to `<take>/hook.log` (open it with "Log" in the panel)
- The result is kept in `<take>/hook.json`

  ```json
  {"event": "exported", "exit_code": 0, "started_utc": "2026-10-21T12:14:23Z",
   "finished_utc": "2026-10-21T12:14:31Z", "seconds": 8.1,
   "message": "231 files to S3", "url": "https://example.com/takes/take_20261021_120000"}
  ```

### Guidelines

- Commands run one at a time, in the order they arrive. They do not block Blender. Long-running work is fine, but set the timeout to match
- The same take gets two runs: `recorded` and `exported`. **Make the second run add only the new files (render.mp4 and so on)**
- Do not write credentials (keys or passwords) directly in the command or script. For AWS, use a profile (`AWS_PROFILE`) or
  `~/.aws/credentials`; for other services, use the keychain or environment variables
- Do not modify the contents of the take folder (`hook.json` and `hook.log` are written by trkAR). Put working files elsewhere

---

## 2. Take Folder

Takes are stored one folder per take in "Save To" under `Recording & Takes > Recording & Export Settings`
(the default is `trkar_takes/` next to the .blend. If the .blend is not saved, `~/Movies/trkAR Takes` on macOS and `~/Videos/trkAR Takes` on Windows.
Before add-on 0.10.0 the defaults were `artrk_takes/` and `ARtrk Takes`; if that folder exists and the new one does not, it keeps using the old one).
The folder name is `take_YYYYMMDD_HHMMSS` (date and time recording started, in the PC's local time).

```
take_20261021_120000/
  take.json            Take info and provenance (below)
  actions.json         Camera motion (F-curves of Blender actions)
  video/000001.jpg     Color of the live video (per frame, 1-based, 6 digits)
  alpha/000001.png     Alpha of the live video (black and white, 8bit). Only when video with alpha was being received
  depth/000001.npz     Shape of the Depth Screen (only when depth was being received)
  render.mp4           Export (MP4 H.264). When PNG sequence is selected, render/0001.png …
  render_<.blend name>.mp4   Kept copy of an export (one more each time you export with a different background)
  hook.json, hook.log  Hook result and log (section 1)
  for_generation/      Generation inputs (0.10.20 or later, when you press `Export Generation Inputs`). Contents: manifest.json, see
                       https://lunacava.github.io/trkAR/generation.html. Earlier ones are kept as for_generation_<date-time>/
```

### take.json

Fields whose value is unknown are `null`. Fields may be added (skip fields you do not know).

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | string | Same as the folder name |
| `fps` | number | Frame rate at recording time |
| `frame_start`, `frame_end` | int | Range on the Blender timeline |
| `frames` | int | Number of frames written (number of images in `video/`) |
| `dropped` | int | Number of frames filled with the previous frame because writing could not keep up |
| `video`, `alpha` | bool | Whether `video/` and `alpha/` exist |
| `camera` | string | Object name of the camera that was moved |
| `action`, `lens_action` | string | Action names for the camera motion and the lens (match the keys in `actions.json`) |
| `recorded_at` | string | Recording start (ISO 8601, PC local time with offset) |
| `slate` | object | `production`, `scene`, `cut`, `take` (int, 0 = not set), `label` (e.g. `S01C01T01`) |
| `location` | object | `blend` (name of the background .blend), `bpy_scene` (Blender scene name) |
| `environment` | object | `site` (shooting environment), `host` (PC nickname), `hostname`, `os`, `blender`, `addon` |
| `device` | object | Device that shot the take. `system` (`trkAR`; `ARtrk` before 0.10.0), `model`, `identifier`, `os`, `app`, `device_name`, `client_id`, `fps`, `hfov` (horizontal field of view, degrees), `tc_source` (`internal` or `ltc`) |
| `people` | object | `creator`, `operator` |
| `render` | object | Only after export. `format` (`MP4`/`PNG`), `width`, `height`, `fps`, `frame_start`, `frame_end`, `blend`, `kept` (file name of the kept copy) |
| `camera_offsets` | object | 0.9.12 or later. Offsets applied to the camera when recording started. `mode` (`RELATIVE`/`ABSOLUTE`), `scale`, `blender` (Blender-side offset: `location`, `pan_deg`, `tilt_deg`, `roll_deg`, `lens_mm`), `iphone_rig` (iPhone CTL: `position`, `yaw_deg`, `tilt_deg`, `roll_deg`, `focal_mm`, `fstop`, `focus_distance`; null if none), `camera_parent`. **All of these are already baked into the camera motion (`actions.json`)** and are not re-applied on playback (they record which offsets the take was recorded with) |
| `placement` | object | 0.9.12 or later. Where the take was placed with "Background Offset" during playback (per take). `matrix` (4×4 world matrix of the anchor Empty), `front_distance` (the "Near Distance" that decided where the person is). If absent, the take is played back and exported at its recorded position |

### actions.json

```json
{"action": {"name": "trkAR take_…", "curves": [
   {"data_path": "location", "array_index": 0,
    "points": [{"co": [frame, value], "interpolation": "BEZIER", "easing": "AUTO",
                "handle_left": [x, y], "handle_right": [x, y], ...}, ...]}, ...]},
 "lens_action": {"name": "trkAR take_… Lens", "curves": [{"data_path": "lens", ...}]}}
```

`data_path` is a Blender camera property such as `location` (m, Blender world coordinates, Z up), `rotation_euler` (radians), or `lens` (mm).

### depth/NNNNNN.npz

Readable with NumPy's `np.load`. It contains `co` (vertex positions, float32; use `co.reshape(-1, 3)` to get x y z rows),
`face_valid` (whether each face is valid, uint8), `mask` (person mask, uint8, 0–255), `key` (`[width, height, decimation]` of the depth grid),
and `meta` (a JSON string).
If you want to build point clouds from depth yourself, receiving the UDP from section 4 directly is easier.

---

## 3. Automation Port

An interface for operating Blender from outside tools (agents such as Claude, your own scripts, Griptape Nodes nodes).
Open it in the sidebar under `Recording & Takes > Automation Port`. It **listens on 127.0.0.1 only** and accepts only a fixed set of
commands (arbitrary Python is not accepted). The default port is 9310.

```
POST http://127.0.0.1:9310/command   {"command": "<command>", "args": {...}}
  → {"ok": true, "result": {...}}  /  {"ok": false, "error": "..."}
GET  http://127.0.0.1:9310/state     shorthand for state
```

| Command | args | What it does |
| --- | --- | --- |
| `state` | none | Current state (open .blend, selected take, whether playing or recording, export progress, hook result) |
| `open_scene` | `path` | Open a .blend (refused while recording or exporting) |
| `list_takes` | none | List of take folders (`name`, `path`, `frames`, `fps`, `playable`, `render`, `kept`) |
| `select_take` | `name` (latest if empty) | Select a take and make it ready to play |
| `place_anchor` | `location` [x, y, z], `heading_deg` (both optional) | Take anchor within the background (floor height and heading) |
| `remove_anchor` | none | Remove the anchor |
| `configure` | Export settings | Only `take_render_mode` (`VIEWPORT`/`RENDER`), `take_render_format` (`MP4`/`PNG`), `take_render_size`, `take_use_range`, `take_range_start`, `take_range_end`, `take_range_mode`, `take_range_seconds`, `take_render_samples` can be changed |
| `export_take` | none | Start an export and return immediately. To detect the end, wait for `export.running` in `state` to become false |

When an export finishes, the hook (section 1) also runs, so you can chain "export via the Automation Port → upload via the hook".

---

## 4. Data from the iPhone (OSC, NDI, UDP)

The formats for receiving data directly in TouchDesigner or your own tools, without Blender. The destination is
`PC IP Address` / `Port` (OSC, default 8000) in the iPhone settings screen (depth UDP goes to port 9100 on the same PC).

### Coordinate system

ARKit's coordinate system: right-handed, Y up, camera forward is -Z, units in meters. Rotation is Euler angles (degrees),
applied in the order roll (Z) → pitch (X) → yaw (Y) (R = Ry · Rx · Rz).
To bring it into Blender, rotate the world +90° around the X axis ((x, y, z) → (x, −z, y)).

### OSC (UDP)

| Address | Arguments | Rate |
| --- | --- | --- |
| `/client/current` | `clientID` (s), `appMode` (i, always 0), `cameraID` (i), `px py pz` (f, m), `rx ry rz` (f, degrees), `tcH tcM tcS tcF` (i, timecode), `fps` (i, FPS type), `floorHeight` (f, height from SET GND in m, 0 if not set), `hFOV` (f, horizontal field of view in degrees), `frameTimestamp` (d, ARFrame.timestamp in seconds), `tcSeconds` (d, seconds since midnight), `tcSource` (i, 0 internal clock / 1 LTC) | Every frame |
| `/client/rig` | `clientID`, `cameraID`, `tx ty tz` (m), `yaw tilt roll` (degrees), `focalMM` (0 = follow the AR field of view), `fStop` (0 = no depth of field), `focusDistance`, `autoFocus` (i, 0 / 1, 1.1.3+), `afSerial` (i, number of times AF was toggled or focus was touched, 1.1.3+) | Every frame |
| `/client/remote` | `clientID`, the other device's `cameraID`, `moveX moveY crane pan tilt roll` (f, stick -1 to 1), `moveSpeed` (m/s), `rotateSpeed` (degrees/s), `focalMM fStop focusDistance`, `poseReset`, `rotationReset` (i, count of ALL RST / ROT RST), `autoFocus`, `afSerial` (1.1.3+) | About 30Hz (when CTL's TARGET is another camera) |
| `/client/light` | `clientID`, `cameraID`, `ambientIntensity` (lm), `ambientColorTemperature` (K), `dirX dirY dirZ`, `dirIntensity`, `frameTimestamp` (d), `originYaw` (degrees), 27 spherical harmonics (front camera only) (1.1.2+) | 10Hz |
| `/client/rec` | `clientID`, `start` (i, 1 start / 0 stop) | REC button |
| `/client/info` | `clientID`, model identifier (e.g. `iPhone17,1`), OS name, OS version, app version, build, device name (all s) | When sending starts and every few seconds |
| `/worldanchor/match` | `clientID` | ANC button |

Blender → device: `/trkar/focus` [`cameraID`, `autoFocus` (0 / 1), focus distance (m), the received `afSerial`] at about 10Hz (add-on 0.10.7+). This makes the focus display in CTL's LENS match the CG camera.

Arguments may be appended at the end (the existing order from the front does not change). `frameTimestamp` uses the same clock as the NDI video timecode
(= ARFrame.timestamp × 10⁷, in 100ns units), so you can match pose and video more finely than per frame.

### NDI

| Source name | Content |
| --- | --- |
| `<device name> (trkAR)` | Camera video (CAM). With alpha if the person mask is selected |
| `<device name> (trkAR-Depth)` | Depth (DPT, 16bit). Metadata is `<trkARDepth …/>`. NDI compression is applied, so use the UDP below if you need exact values |

Up to app 1.1.1 these were `(ARtrk)`, `(ARtrk-Depth)`, and `<ARtrkDepth …/>` (before the names were unified to trkAR). Receivers should accept both.

NDI® is a registered trademark of Vizrt NDI AB. For details, see [ndi.video](https://ndi.video/).

### Depth (UDP, uncompressed)

The default port is 9100 (destination is `PC IP Address`). Each frame is split into packets by rows (little-endian).

| Offset | Size | Content |
| --- | --- | --- |
| 0 | 4 | magic `ARD3` |
| 4 | 4 | `frame_id` (UInt32) |
| 8 | 2 | `width` (UInt16) |
| 10 | 2 | `height` (UInt16) |
| 12 | 2 | `row_start` (UInt16) |
| 14 | 2 | `row_count` (UInt16) |
| 16 | 2 | `packet_index` (UInt16) |
| 18 | 2 | `packet_count` (UInt16) |
| 20 | 16 | `fx fy cx cy` (Float32 × 4, at depth resolution) |
| 36 | 4 | `flags` (bit 0 portrait, bit 1 no confidence, bit 2 person mask only) |
| 40 | 8 | `timecode` (Int64, 100ns. Same clock as the NDI video) |
| 48 | … | `row_count × width` UInt16 values. Bits 0–12 depth (mm, 0 = invalid), bit 13 person, bits 14–15 confidence (0 low / 1 medium / 2 high) |

Discard frames for which not all `packet_count` packets arrived.

---

## 5. App and Add-on Compatibility

The iPhone app (trkAR) and the Blender add-on (trkAR Camera) are connected only through the OSC, NDI, and UDP formats in section 4.
**As long as the format is the same, any combination of app and add-on versions works.** You can update either one first.

| Format version | App | Add-on | Changes |
| --- | --- | --- | --- |
| 1 | 1.1, 1.1.1 | 0.9.11, 0.9.12 | (first version) |
| 1 (renamed to trkAR) | 1.1.2 | 0.10.0 | Unified the NDI source names and metadata, take names, and the add-on id (`artrk_camera` → `trkar_camera`) to trkAR. OSC and UDP are unchanged |
| 1 (remote control, AF) | 1.1.3 | 0.10.17 | Added `/client/remote` and `/trkar/focus`, and appended `autoFocus` and `afSerial` to the end of `/client/rig` (only appended, so combinations with older versions still work. Only remote control and AF need the new versions of both) |

- 1.1 → 1.1.1 changed only the UI (REC button and so on), and 0.9.11 → 0.9.12 changed only the take anchor and recording; both stay on format 1
- Add-on 0.10.0 also reads the old names (`(ARtrk)`, `<ARtrkDepth>`, `artrk_takes/`, settings in .blend files saved with 0.9.x).
  With add-on 0.9.x and app 1.1.2, only the depth **NDI** cannot be read (because the metadata name changed; depth UDP can be read)
- Because the add-on id changed, if you have 0.9.x installed, install 0.10.0 first and then remove 0.9.x (`trkAR Camera`, id `artrk_camera`)
- Rules for changing the format: OSC arguments are **only appended at the end**; the existing order from the front does not change. Receivers skip unknown trailing arguments,
  and keep working as before with older senders that do not send the appended arguments. Depth UDP is identified by its magic (`ARD1` to `ARD3`), and all of them can be read
- When the format changes, add a row to this table and write the minimum app and add-on combination here
- Which versions a take was recorded with is kept in the take's `take.json`, in `device.app` (app version) and `environment.addon` (add-on version)

## 6. Generation Inputs (for_generation/)

The add-on's `Export Generation Inputs` writes the set of files for video generation models that take references into
`<take>/for_generation/` (exporting again keeps the previous one as `for_generation_<date>/`).
When you connect your own cloud or a video generation service, pass this folder as a whole.
For a hook, the `for_generation` event gives this folder in `{for_generation}`.

| File | What it is | How to give it to a model (`input` in the manifest) |
| --- | --- | --- |
| `reference.mp4` | The set rendered in one flat gray clay color with the take's camera move. The person is a white outline taken from the live video, filled black | Reference video (`reference_video`). `<Video 1>` in the prompt |
| `layout.png` | A 2×2 image looking forward, right, back and left from where the person stands (the location) | Reference image (`reference_image`). `<Picture 1>` |
| `actor.jpg` | A frame where the person is large, sharp and well exposed (gray outside the person) | Reference image (`reference_image`). `<Picture 2>` |
| `first_frame.png` | The first frame of the range | First frame (`first_frame_image`) |
| `camera.json` | Camera position, rotation and field of view per frame (Blender world, Z up, meters) | Camera path (`camera_path`) |
| `prompt.txt` | A prompt template (in English) that points to the files above. Replace the last line, `[Write the shot here: …]` | Text (`text`) |
| `manifest.json` | The list below | For scripts |

The shape of `manifest.json` (`version` is currently `1`. Fields may be added; the meaning of existing fields does not change):

```json
{
  "version": 1,
  "made_by": "trkAR Camera",
  "take": "take_20261106_173030",
  "frame_start": 260, "frame_end": 499, "fps": 24.0, "seconds": 10.0,
  "time_offset": 0,
  "blend": "subway_platform_v4_20s",
  "model": "Any video generation model that accepts a reference video and reference images ...",
  "files": {
    "reference.mp4": {"role": "...", "input": "reference_video", "width": 960, "height": 544, "frames": 240},
    "layout.png": {"role": "...", "input": "reference_image"},
    "actor.jpg": {"role": "...", "input": "reference_image"},
    "first_frame.png": {"role": "...", "input": "first_frame_image"},
    "camera.json": {"role": "...", "input": "camera_path"},
    "prompt.txt": {"role": "...", "input": "text"}
  },
  "seconds_to_make": 42.0
}
```

- `frame_start` / `frame_end` are in the background CG's time. The take's frame numbers are these minus `time_offset`
- Files not in `files` were not written (for example `actor.jpg` for a take without a person mask, `layout.png` when the layout is off)
- All paths are relative to this folder. You can move the whole folder elsewhere (S3 and so on) and use it as it is
- Models name references differently (`<Video 1>`, `Video 1`, `@video1`, `[ref1]` and so on) and take different numbers of them.
  Rewrite `prompt.txt` on the sending side
- `actor.jpg` and `reference.mp4` contain the people in the take. Get their consent before sending them to an outside service.
  Some services refuse faces of real people

---

## Examples

- [`examples/s3_upload.py`](examples/s3_upload.py): A hook that uploads a take to Amazon S3 (uses the aws CLI)
- [`examples/fal_h3_generate.py`](examples/fal_h3_generate.py): Sends `for_generation/` to MiniMax H3 on fal.ai and downloads the result (as a `for_generation` hook or by hand. Charged to your own fal.ai account) `--look <image>` sends a photoreal look sheet as the location reference instead of `layout.png`.

When building your own integration, the quickest way is to copy an example and rewrite the destination and authentication parts.

---

This document is licensed under CC BY 4.0 (© 2026 lunacava / trkAR), and the example code under Apache-2.0.
**If you use it, we'd be glad to hear from you** (optional): [the "Used by" issue](https://github.com/lunacava/trkAR/issues/new?template=used-by.md)
