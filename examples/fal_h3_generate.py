#!/usr/bin/env python3
# Copyright 2026 lunacava
# SPDX-License-Identifier: Apache-2.0  (examples/LICENSE. Keep examples/NOTICE when you redistribute)
# If you use it, we'd love to hear from you (optional): https://github.com/lunacava/trkAR/issues/new?template=used-by.md
"""Send trkAR's generation inputs (for_generation/) to MiniMax H3 on fal.ai and save the result.

An example of the "last step": the add-on writes for_generation/ (Export Generation Inputs), and this script
uploads reference.mp4 / layout.png / actor.jpg to fal.ai, asks MiniMax H3 (reference-to-video) to generate,
waits, and downloads the video into for_generation/results/<time>/.

Run it by hand:

    FAL_KEY=... python3 fal_h3_generate.py <take>/for_generation --shot "A detective in a long coat waits ..."

or as the add-on's hook (Edit > Preferences > Add-ons > trkAR Camera > After-take command, with
"After Generation Inputs" on):

    python3 ~/trkar/fal_h3_generate.py {for_generation} --event {event} --allow-external

- Costs money on your fal.ai account (H3 is about USD 0.06 per second of video at the time of writing; check
  https://fal.ai/models/minimax/h3/reference-to-video for the current price and terms).
- The key comes from the FAL_KEY environment variable. Do not write it in this file or in the command.
  (When Blender runs the hook, Blender's environment is used: set FAL_KEY for the app, or wrap this script.)
- It sends the person's photo and outline to an outside service. Get the consent of the people in the take,
  and pass --allow-external to say you have it (the hook does not send anything without it).
- The shot description: the last line of prompt.txt is a placeholder. Edit prompt.txt, or pass --shot.
- The prompt's <Video 1> / <Picture N> are rewritten to the names H3 uses (Video 1 / Image N).
- H3 takes 5 to 15 seconds. Set the take's range so that the inputs are in that length.
- The last line printed is {"message", "url"} JSON, which the add-on shows in its panel.
- Python 3.8+ standard library only.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

MODEL = "minimax/h3/reference-to-video"
QUEUE_URL = "https://queue.fal.run"
TOKEN_URL = "https://rest.fal.ai/storage/auth/token?storage_type=fal-cdn-v3"
CDN_URL = "https://v3.fal.media"
PLACEHOLDER = "[Write the shot here"
TIMEOUT_SECONDS = 1800


def http(method, url, headers, body=None, timeout=120):
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fal.ai https hosts)
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError("%s %s failed: HTTP %s %s" % (method, url.split("?")[0], exc.code, detail)) from exc


def done(message, url="", code=0):
    print(json.dumps({"message": message, "url": url}, ensure_ascii=False), flush=True)
    sys.exit(code)


def find_video(result):
    node = result.get("video") if isinstance(result, dict) else None
    if isinstance(node, dict) and isinstance(node.get("url"), str):
        return node["url"]
    stack = [result]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if isinstance(item.get("url"), str) and item["url"].startswith("http"):
                return item["url"]
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return None


def main():
    ap = argparse.ArgumentParser(description="Generate a video from trkAR's for_generation/ with MiniMax H3 on fal.ai")
    ap.add_argument("folder", nargs="?", default=os.environ.get("TRKAR_FOR_GENERATION", ""),
                    help="the for_generation/ folder (default: $TRKAR_FOR_GENERATION)")
    ap.add_argument("--event", default=os.environ.get("TRKAR_EVENT", ""),
                    help="the hook's event. Only for_generation (or empty, when run by hand) generates")
    ap.add_argument("--shot", default="", help="the shot description, used instead of prompt.txt's last line")
    ap.add_argument("--allow-external", action="store_true",
                    help="you have the consent of the people in the take to send their image to fal.ai")
    ap.add_argument("--resolution", default="768P", choices=["768P", "1080P"])
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    if args.event and args.event != "for_generation":
        done("skipped (event %s)" % args.event)
    folder = os.path.abspath(os.path.expanduser(args.folder or ""))
    manifest_path = os.path.join(folder, "manifest.json")
    if not os.path.isfile(manifest_path):
        done("for_generation/manifest.json not found: %s" % folder, code=2)
    if not args.allow_external:
        done("not sent: pass --allow-external once you have the consent of the people in the take", code=2)
    key = os.environ.get("FAL_KEY", "")
    if not key:
        done("FAL_KEY is not set", code=2)
    manifest = json.load(open(manifest_path, encoding="utf-8"))
    seconds = float(manifest.get("seconds") or 0)
    duration = int(round(seconds))
    if not 5 <= duration <= 15:
        done("H3 takes 5 to 15 seconds; these inputs are %.1f s. Change the take's range and export again" % seconds,
             code=2)

    prompt = open(os.path.join(folder, "prompt.txt"), encoding="utf-8").read().strip()
    lines = prompt.splitlines()
    if args.shot:
        lines = [line for line in lines if not line.startswith(PLACEHOLDER)] + ["", args.shot.strip()]
    elif any(line.startswith(PLACEHOLDER) for line in lines):
        done("write the shot in prompt.txt (its last line is a placeholder), or pass --shot", code=2)
    prompt = re.sub(r"<Picture (\d+)>", r"Image \1", re.sub(r"<Video (\d+)>", r"Video \1", "\n".join(lines).strip()))

    images = [name for name in ("layout.png", "actor.jpg") if os.path.isfile(os.path.join(folder, name))]
    files = images + ["reference.mp4"]
    print("uploading %s to fal.ai" % ", ".join(files), flush=True)
    token = http("POST", TOKEN_URL, {"Authorization": "Key " + key, "Content-Type": "application/json",
                                     "Accept": "application/json"}, b"{}")
    urls = []
    for name in files:
        kind = "video/mp4" if name.endswith(".mp4") else "image/png" if name.endswith(".png") else "image/jpeg"
        with open(os.path.join(folder, name), "rb") as f:
            body = f.read()
        uploaded = http("POST", CDN_URL + "/files/upload",
                        {"Authorization": "%s %s" % (token["token_type"], token["token"]), "X-Fal-File-Name": name,
                         "Content-Type": kind}, body, timeout=600)
        urls.append(uploaded["access_url"])

    auth = {"Authorization": "Key " + key, "Content-Type": "application/json"}
    arguments = {"prompt": prompt, "duration": duration, "resolution": args.resolution, "aspect_ratio": "16:9",
                 "seed": args.seed, "prompt_expansion_mode": "disabled",
                 "reference_image_urls": urls[:-1], "reference_video_urls": urls[-1:]}
    submitted = http("POST", "%s/%s" % (QUEUE_URL, MODEL), auth, json.dumps(arguments).encode("utf-8"))
    print("submitted %s (%d s, %s)" % (submitted.get("request_id"), duration, args.resolution), flush=True)
    started = time.time()
    while True:
        status = http("GET", submitted["status_url"], auth)
        if status.get("status") == "COMPLETED":
            break
        if time.time() - started > TIMEOUT_SECONDS:
            done("fal.ai did not finish in %d minutes (request %s)" % (TIMEOUT_SECONDS // 60, submitted.get("request_id")),
                 code=1)
        time.sleep(5)
    result = http("GET", submitted["response_url"], auth)
    video = find_video(result)
    if not video:
        done("no video in the result: %s" % json.dumps(result)[:300], code=1)

    out = os.path.join(folder, "results", time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out, exist_ok=True)
    urllib.request.urlretrieve(video, os.path.join(out, "h3.mp4"))  # noqa: S310
    with open(os.path.join(out, "request.json"), "w", encoding="utf-8") as f:
        json.dump({"model": MODEL, "arguments": dict(arguments, reference_image_urls=images,
                                                     reference_video_urls=["reference.mp4"]),
                   "request_id": submitted.get("request_id"), "seconds": round(time.time() - started, 1)},
                  f, ensure_ascii=False, indent=2)
    done("generated %s (%.0f s)" % (os.path.relpath(os.path.join(out, "h3.mp4"), folder), time.time() - started), video)


if __name__ == "__main__":
    main()
