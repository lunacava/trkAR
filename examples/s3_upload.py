#!/usr/bin/env python3
# SPDX-License-Identifier: CC0-1.0  (自由にコピー・改変してよい)
"""trkAR のテイクを Amazon S3 に上げるフックの見本 (INTEGRATION.md の 1 章)。

Blender の「テイクのあとに流すコマンド」に、たとえば次のように書きます:

    python3 ~/trkar/s3_upload.py {take_dir} {event} --bucket my-bucket --prefix trkar/takes

- aws CLI (https://aws.amazon.com/cli/) を使います。認証は AWS のプロファイル (--profile か AWS_PROFILE) で。
  キーをこのファイルやコマンドに書かないでください
- `aws s3 sync` なので、録画のあと (recorded) と書き出しのあと (exported) の 2 回流れても、2 回目は
  増えたファイル (render.mp4 など) だけが上がります
- hook.json / hook.log (trkAR が書く) と、控えの render_*.mp4 は上げません (--with-kept で上げる)
- 最後の行に {"message", "url"} の JSON を出すと、Blender のパネルに表示されます
- Python 3.8 以降の標準ライブラリだけで動きます
"""

import argparse
import json
import os
import shutil
import subprocess
import sys


def main():
    ap = argparse.ArgumentParser(description="trkAR のテイクを S3 に上げる")
    ap.add_argument("take_dir", nargs="?", default=os.environ.get("TRKAR_TAKE_DIR", ""))
    ap.add_argument("event", nargs="?", default=os.environ.get("TRKAR_EVENT", ""))
    ap.add_argument("--bucket", default=os.environ.get("TRKAR_S3_BUCKET", ""), help="バケット名")
    ap.add_argument("--prefix", default=os.environ.get("TRKAR_S3_PREFIX", "trkar/takes"), help="バケットの中の置き場")
    ap.add_argument("--profile", default="", help="AWS のプロファイル (空なら AWS_PROFILE か default)")
    ap.add_argument("--region", default="", help="リージョン (空なら プロファイルの設定)")
    ap.add_argument("--aws", default=shutil.which("aws") or "aws", help="aws コマンドのパス")
    ap.add_argument("--with-kept", action="store_true", help="控えの render_<.blend の名前>.mp4 も上げる")
    args = ap.parse_args()

    take_dir = os.path.abspath(os.path.expanduser(args.take_dir))
    if not os.path.isfile(os.path.join(take_dir, "take.json")):
        return fail("テイクのフォルダではありません: %s" % take_dir)
    if not args.bucket:
        return fail("--bucket (か TRKAR_S3_BUCKET) を指定してください")

    name = os.path.basename(take_dir.rstrip(os.sep))
    dest = "s3://%s/%s/%s/" % (args.bucket, args.prefix.strip("/"), name)
    cmd = [args.aws, "s3", "sync", take_dir, dest, "--no-progress",
           "--exclude", "hook.json", "--exclude", "hook.log"]
    if not args.with_kept:
        cmd += ["--exclude", "render_*"]
    if args.profile:
        cmd += ["--profile", args.profile]
    if args.region:
        cmd += ["--region", args.region]

    print("event=%s  %s -> %s" % (args.event or "?", take_dir, dest), flush=True)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        return fail("aws CLI を起動できません (%s)。--aws でパスを指定してください" % exc)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        last = (proc.stderr.strip().splitlines() or ["終了コード %d" % proc.returncode])[-1]
        return fail("aws s3 sync が失敗: %s" % last[:200], proc.returncode)

    uploaded = sum(1 for line in proc.stdout.splitlines() if line.startswith("upload:"))
    url = "https://s3.console.aws.amazon.com/s3/buckets/%s?prefix=%s/%s/" % (
        args.bucket, args.prefix.strip("/"), name)
    print(json.dumps({"message": "S3 に %d ファイル (%s)" % (uploaded, args.event or "-"), "url": url},
                     ensure_ascii=False))
    return 0


def fail(message, code=1):
    print(json.dumps({"message": message}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
