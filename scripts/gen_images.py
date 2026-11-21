# -*- coding: utf-8 -*-
"""生图（gen_images）：给 muse 片子出分镜图和定妆图。

为什么技能里必须有这一步（2026-11-21 用户定的口径）：
    「资产图管够，生图我们的渠道很便宜，生视频是很贵的。」
    生图 ¥0.025/张，生视频 ¥0.20/10 秒 —— 一张图 ≈ 视频的 1/8 秒。
    所以图要生够（每拍 3 张候选 + 定妆 3 张 + 道具 1–2 张），视频只拍一次。

这个脚本只做一件事：调 /images/edits（多图参考），出图，下载到本地。
渠道配置从 JSON 读，**不把 key 写进本仓库**：

    export MUSE_IMG_CONF=~/.workbuddy/skills/image-gen-001/channels/huahuaapi.json
    # 或用 --conf 指定；文件里要有 base_url / api_key / default_model

用法:
    # 单张
    python gen_images.py --conf $CONF --out 图/定妆-A.png --prompt-file p.txt --image 人物.jpg

    # 批量（清单 JSON：[{"out":..,"prompt":..,"images":[..]}, ...]）
    python gen_images.py --conf $CONF --batch 清单.json

    # 只出 prompt 不出图（省钱，先看文案对不对）
    python gen_images.py --dry --batch 清单.json

代理：花花API 的图片 CDN 在大陆需要代理，默认试 127.0.0.1:7897，可用 --proxy 改或 --proxy none。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

DEFAULT_PROXY = "http://127.0.0.1:7897"


def conf(path):
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        return json.load(f)


def build_body(model, prompt, files, size=None, n=1):
    b = uuid.uuid4().hex
    parts = []

    def field(k, v):
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                      % (b, k, v)).encode("utf-8"))

    field("model", model)
    field("prompt", prompt)
    field("n", str(n))
    if size:
        field("size", size)
    for p in files:
        p = os.path.expanduser(p)
        ext = os.path.splitext(p)[1].lower()
        mime = "image/png" if ext == ".png" else "image/jpeg"
        with open(p, "rb") as f:
            data = f.read()
        parts.append(('--%s\r\nContent-Disposition: form-data; name="image[]"; '
                      'filename="%s"\r\nContent-Type: %s\r\n\r\n'
                      % (b, os.path.basename(p), mime)).encode("utf-8") + data + b"\r\n")
    parts.append(("--%s--\r\n" % b).encode("utf-8"))
    return b"".join(parts), "multipart/form-data; boundary=" + b


def opener(proxy):
    if proxy and proxy.lower() != "none":
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def gen_one(c, prompt, images, out, size, timeout, proxy, model=None):
    model = model or c.get("default_model")
    body, ctype = build_body(model, prompt, images, size)
    url = c["base_url"].rstrip("/") + "/images/edits"
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": "Bearer " + c["api_key"], "Content-Type": ctype,
        "User-Agent": "Mozilla/5.0"})
    t0 = time.time()
    try:
        raw = opener(proxy).open(req, timeout=timeout).read()
    except urllib.error.HTTPError as e:
        return False, "HTTP %s: %s" % (e.code, e.read()[:300].decode("utf-8", "replace"))
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return False, "返回不是 JSON：%s" % raw[:200]
    item = (data.get("data") or [{}])[0]
    img_url = item.get("url")
    b64 = item.get("b64_json")
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    if b64:
        import base64
        with open(out, "wb") as f:
            f.write(base64.b64decode(b64))
    elif img_url:
        # CDN 慢（10–15 KB/s），一次下完，不支持断点续传
        rq = urllib.request.Request(img_url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            blob = opener(proxy).open(rq, timeout=600).read()
        except Exception as e:
            return False, "下载失败 %s（URL 保留：%s）" % (e, img_url)
        with open(out, "wb") as f:
            f.write(blob)
    else:
        return False, "返回里没有 url / b64_json：%s" % json.dumps(data, ensure_ascii=False)[:300]
    if not (os.path.exists(out) and os.path.getsize(out) > 10000):
        return False, "文件太小，不像图：%s" % out
    return True, "%s  %.1fKB  %.0f 秒" % (out, os.path.getsize(out) / 1024, time.time() - t0)


def main():
    ap = argparse.ArgumentParser(description="生图：分镜图 / 定妆图")
    ap.add_argument("--conf", default=os.environ.get("MUSE_IMG_CONF", ""),
                    help="渠道配置 JSON（base_url / api_key / default_model）")
    ap.add_argument("--out")
    ap.add_argument("--prompt-file")
    ap.add_argument("--prompt")
    ap.add_argument("--image", action="append", default=[], help="参考图，可多张")
    ap.add_argument("--batch", help="批量清单 JSON")
    ap.add_argument("--size", default=None, help="如 1536x1024 / 1024x1024")
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--proxy", default=DEFAULT_PROXY)
    ap.add_argument("--dry", action="store_true", help="只打印，不调接口")
    a = ap.parse_args()

    jobs = []
    if a.batch:
        with open(a.batch, encoding="utf-8") as f:
            jobs = json.load(f)
    else:
        if not a.out:
            sys.exit("要么给 --out，要么给 --batch")
        pr = a.prompt
        if a.prompt_file:
            pr = open(a.prompt_file, encoding="utf-8").read().strip()
        if not pr:
            sys.exit("没有 prompt")
        jobs = [{"out": a.out, "prompt": pr, "images": a.image}]

    if a.dry:
        for j in jobs:
            print("=== %s  参考图 %d 张" % (j["out"], len(j.get("images") or [])))
            print(j["prompt"])
            print()
        print("共 %d 张（dry，未调用）" % len(jobs))
        return 0

    if not a.conf:
        sys.exit("没有渠道配置：给 --conf 或设 MUSE_IMG_CONF")
    c = conf(a.conf)
    ok = fail = 0
    for i, j in enumerate(jobs, 1):
        print("[%d/%d] %s" % (i, len(jobs), j["out"]), flush=True)
        good, msg = gen_one(c, j["prompt"], j.get("images") or [], j["out"],
                            j.get("size") or a.size, a.timeout, a.proxy, a.model)
        print(("  [ok]   " if good else "  [失败] ") + msg, flush=True)
        ok += good
        fail += (not good)
    print("\n成功 %d / 失败 %d" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
