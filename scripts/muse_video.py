#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""muse 生视频 CLI（Nova AI / router.yizzav.com）

用法：
    python muse_video.py quote    --duration 50
    python muse_video.py submit   --prompt-file p.txt --duration 50 --aspect 16:9 \
                                  --first a.jpg --last b.jpg --ref board.jpg --yes
    python muse_video.py wait     --id task_xxx --timeout 3600
    python muse_video.py download --id task_xxx --out ./out --name 片名
    python muse_video.py run      ...（submit + wait + download）

时长 1–60 秒；>10 秒由平台自动拆成每段 <=10 秒、段间末帧续接、最后拼成一条 MP4。
"""
import argparse
import base64
import hashlib
import json
import mimetypes
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(SKILL_DIR, "config.json")
LEDGER_PATH = os.path.join(SKILL_DIR, "ledger.json")
APPROVE_FILE = ".muse-approve.json"
APPROVE_TTL = 12 * 3600   # 用户点头 12 小时内有效；超时要重新给他看价格

DEFAULT_BASE = "https://router.yizzav.com/v1"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
# (上限秒数, 模型号, 价格 CNY)  —— 按档计费，不是按秒
TIERS = [
    (10, "muse-video-10s", 0.20),
    (20, "muse-video-20s", 0.40),
    (30, "muse-video-30s", 0.60),
    (40, "muse-video-40s", 0.80),
    (50, "muse-video-50s", 1.00),
    (60, "muse-video-60s", 1.20),
]


def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    cfg.setdefault("api_base", DEFAULT_BASE)
    cfg["api_key"] = os.environ.get("MUSE_API_KEY") or cfg.get("api_key", "")
    return cfg


def tier_for(duration):
    for limit, model, price in TIERS:
        if duration <= limit:
            return limit, model, price
    raise SystemExit("时长超过 60 秒，平台不支持")


def die(msg, code=1):
    print("[x] " + msg)
    raise SystemExit(code)


# ---------------------------------------------------------------- 提交账本
# 全局一份，不按项目分。目的只有一个：**禁止重复提交**。
#   queued / in_progress  -> 谁都不许再发
#   failed 且 refund != confirmed -> 不许再发（钱还没回来）
#   completed 且同项目 -> 要带 --replace 旧 id 才许发
def load_ledger():
    if not os.path.exists(LEDGER_PATH):
        return {"tasks": []}
    with open(LEDGER_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_ledger(led):
    tmp = LEDGER_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(led, f, ensure_ascii=False, indent=1)
    os.replace(tmp, LEDGER_PATH)


def find_task(led, tid):
    for t in led["tasks"]:
        if t.get("id") == tid:
            return t
    return None


def refresh_ledger(cfg, led=None, verbose=True):
    """把还没结案的任务状态刷一遍。"""
    led = led or load_ledger()
    changed = False
    for t in led["tasks"]:
        if t.get("status") in ("queued", "in_progress"):
            url = cfg["api_base"].rstrip("/") + "/videos/" + t["id"]
            status, resp = http_json("GET", url, cfg["api_key"], timeout=60)
            if status >= 400:
                if verbose:
                    print("  %s 查询失败 HTTP %s" % (t["id"], status))
                continue
            st = resp.get("status", t["status"])
            if st != t.get("status"):
                t["status"] = st
                t["checked"] = time.strftime("%Y-%m-%d %H:%M:%S")
                changed = True
            if st == "failed" and t.get("refund") != "confirmed":
                t["refund"] = "pending"
                t["error"] = json.dumps(resp.get("error") or {}, ensure_ascii=False)[:400]
            if st == "completed":
                t["completed"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if verbose:
                print("  %s  %s%s" % (t["id"], st,
                                      "" if resp.get("progress") is None
                                      else " %s%%" % resp.get("progress")))
    if changed:
        save_ledger(led)
    return led


def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(args):
    """必须和 preflight.py 的 fingerprint 完全一致。"""
    h = hashlib.sha256()
    with open(args.prompt_file, "rb") as f:
        h.update(f.read())
    for v in (args.duration, args.aspect, args.style or "", args.camera or "", args.negative or ""):
        h.update(("|%s" % v).encode("utf-8"))
    for p in [args.first, args.last] + list(args.ref or []):
        h.update(("|%s" % (sha_file(p) if p and os.path.exists(p) else "")).encode("utf-8"))
    return h.hexdigest()


def gate(args):
    """提交闸门：令牌 + 账本，任一不过就不许发。返回 (led, project)。"""
    tok_path = os.path.join(os.path.dirname(os.path.abspath(args.prompt_file)), ".muse-preflight.json")
    if not os.path.exists(tok_path):
        die("没有提交令牌：先跑 preflight.py 做终极检查\n"
            "    python %s/preflight.py --prompt %s --duration %d --segs %d --lines 台词卡.txt ..."
            % (os.path.join(SKILL_DIR, "scripts"), args.prompt_file, args.duration,
               (args.duration + 9) // 10), 2)
    with open(tok_path, "r", encoding="utf-8") as f:
        tok = json.load(f)
    if tok.get("duration") != args.duration:
        die("令牌是 %s 秒的，这次提交 %d 秒——重跑 preflight" % (tok.get("duration"), args.duration), 2)
    fp = fingerprint(args)
    if tok.get("fingerprint") != fp:
        die("材料在 preflight 之后改过了（prompt / 图片 / 参数任一变动都会失效）——重跑 preflight", 2)
    # 中级检查也进闸门：审片单指纹对不上 = 用户看过的不是现在要提交的东西
    rj = os.path.join(os.path.dirname(os.path.abspath(args.prompt_file)), ".muse-review.json")
    if not os.path.exists(rj):
        die("没有中级检查的审片单：先跑 review_sheet.py 并把结果给用户看", 2)
    try:
        with open(rj, encoding="utf-8") as f:
            rev = json.load(f)
    except (OSError, ValueError):
        die("审片单文件读不了：%s" % rj, 2)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import review_sheet as _rs
    imgs = ([args.first] if args.first else []) + ([args.last] if args.last else []) + list(args.ref or [])
    if rev.get("sheet_sha") != _rs.sheet_sha(tok.get("script"), args.prompt_file, imgs, args.duration):
        die("材料在审片单之后改过了（脚本卡 / prompt / 图片 / 时长任一变动）——"
            "重跑 review_sheet.py 再给用户看", 2)
    if rev.get("blockers"):
        die("审片单上还有 %d 项必改没处理——先问用户改不改，不要自己改" % len(rev["blockers"]), 2)

    # 文案层也进闸门：脚本卡改了，令牌照样失效
    sp, ssha = tok.get("script"), tok.get("script_sha")
    if sp and ssha:
        if not os.path.exists(sp):
            die("脚本卡不见了：%s —— 重跑 preflight" % sp, 2)
        if sha_file(sp) != ssha:
            die("脚本卡在 preflight 之后改过了（台词或画面动过）——重跑 preflight", 2)

    project = os.path.abspath(args.project or os.path.dirname(os.path.abspath(args.prompt_file)))
    cfg = load_config()
    led = refresh_ledger(cfg, verbose=False) if cfg["api_key"] else load_ledger()

    running = [t for t in led["tasks"] if t.get("status") in ("queued", "in_progress")]
    if running:
        die("已有任务在跑，禁止重复提交：\n" + "\n".join(
            "    %s  %s  %.0f 分钟前" % (t["id"], t["status"], (time.time() - t.get("ts_epoch", time.time())) / 60)
            for t in running) + "\n  先把这条跑完（wait），不要另起一条", 3)

    unpaid = [t for t in led["tasks"] if t.get("status") == "failed" and t.get("refund") != "confirmed"]
    if unpaid:
        die("有失败任务还没确认退款，不许开新任务：\n" + "\n".join(
            "    %s  %s" % (t["id"], t.get("error", "")[:80]) for t in unpaid)
            + "\n  去 /wallet 看余额，然后：muse_video.py refund --id <id> --balance-before A --balance-after B", 3)

    done = [t for t in led["tasks"] if t.get("status") == "completed"
            and t.get("project") == project and not t.get("superseded_by")]
    if done:
        if not args.replace:
            die("本项目已经出过片，禁止无脑重拍：\n" + "\n".join(
                "    %s  %s  ¥%.2f" % (t["id"], t.get("created", ""), t.get("price", 0)) for t in done)
                + "\n  确实要重拍，带 --replace <旧id> --why \"为什么旧的不行\"", 3)
        old = find_task(led, args.replace)
        if not old:
            die("--replace 指定的任务不在账本里：%s" % args.replace, 3)
        if not args.why:
            die("--replace 必须同时给 --why，把重拍原因写清楚（这是唯一的钱袋子口）", 3)
        old["superseded_by"] = "pending"
        old["supersede_reason"] = args.why
        save_ledger(led)
        print("重拍：%s 标记为已取代（原因：%s）" % (args.replace, args.why))
    return led, project


def cmd_ledger(args):
    cfg = load_config()
    led = refresh_ledger(cfg) if (args.refresh and cfg["api_key"]) else load_ledger()
    if not led["tasks"]:
        print("账本空：还没提交过任务")
        return
    print("%-24s %-11s %-6s %-9s %s" % ("任务", "状态", "时长", "退款", "时间/项目"))
    for t in led["tasks"]:
        print("%-24s %-11s %-6s %-9s %s  %s" % (
            t["id"], t.get("status", "?"), "%ss" % t.get("duration", "?"),
            t.get("refund", "—"), t.get("created", ""), os.path.basename(t.get("project", ""))))
    spent = sum(t.get("price", 0) for t in led["tasks"] if t.get("status") != "failed"
                or t.get("refund") == "confirmed")
    print("\n累计已花（失败已退的不计）¥%.2f，余额以 /wallet 为准" % spent)


def cmd_refund(args):
    led = load_ledger()
    t = find_task(led, args.id)
    if not t:
        die("账本里没有这个任务：%s" % args.id, 2)
    if t.get("status") != "failed":
        die("这个任务状态是 %s，不是 failed——没失败就没有退款" % t.get("status"), 2)
    price = t.get("price", 0)
    if args.confirm:
        t["refund"] = "confirmed"
        t["refund_note"] = args.note or "人工确认"
        save_ledger(led)
        print("已标记退款确认（人工）：%s  ¥%.2f" % (args.id, price))
        return
    if args.balance_before is None or args.balance_after is None:
        die("要证明退款，给 --balance-before 和 --balance-after（/wallet 页面的值），"
            "或者由用户明确说了退款到账再 --confirm --note \"...\"", 2)
    delta = args.balance_after - args.balance_before
    print("余额变化 %.4f，本单价格 %.4f" % (delta, price))
    if abs(delta - price) <= max(0.02, price * 0.05) or delta >= price * 0.95:
        t["refund"] = "confirmed"
        t["refund_note"] = "余额 %.4f → %.4f（+%.4f）" % (args.balance_before, args.balance_after, delta)
        save_ledger(led)
        print("退款确认：%s（%s）" % (args.id, t["refund_note"]))
    else:
        die("余额变化和本单价格对不上，不算退款——别急着开新任务，先看账单", 2)



def http_json(method, url, key, body=None, timeout=120):
    data = None
    headers = {
        "Authorization": "Bearer " + key,
        "User-Agent": UA,
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:800]}
    except urllib.error.URLError as e:
        die("网络错误：%s" % e)


def as_image_value(path):
    """本地文件 -> data URL；http(s) 原样返回。"""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if path.startswith("data:"):
        return path
    if not os.path.exists(path):
        die("图片不存在：%s" % path)
    size = os.path.getsize(path)
    if size > 10 * 1024 * 1024:
        die("图片超过 10 MB：%s（%.1f MB）" % (path, size / 1048576))
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    print("    图片 %s -> %.0f KB base64" % (os.path.basename(path), len(b64) / 1024))
    return "data:%s;base64,%s" % (mime, b64)


def build_body(args):
    limit, model, price = tier_for(args.duration)
    prompt = open(args.prompt_file, "r", encoding="utf-8").read().strip()
    if not prompt:
        die("prompt 是空的")
    body = {
        "model": model,
        "prompt": prompt,
        "duration": args.duration,
        "aspect_ratio": args.aspect,
    }
    if args.style:
        body["style"] = args.style
    if args.camera:
        body["camera_movement"] = args.camera
    if args.negative:
        body["negative_prompt"] = args.negative
    total_images = 0
    if args.first:
        body["first_image_url"] = as_image_value(args.first)
        total_images += 1
    if args.last:
        if not args.first:
            die("给了 --last 就必须给 --first")
        body["last_image_url"] = as_image_value(args.last)
        total_images += 1
    refs = args.ref or []
    if refs:
        vals = [as_image_value(r) for r in refs]
        total_images += len(vals)
        body["reference_image_urls"] = vals
        if not args.first:
            print("⚠️  没传 --first：实测第一张参考图会被当成首帧（画面=那张图），"
                  "想自由构图必须显式传 --first")
    # 上限按档位不同：≤10 秒档合计 10 张，>10 秒档合计 9 张（2026-11-21 文档 + 接口实测）
    cap = 10 if args.duration <= 10 else 9
    if total_images > cap:
        die("图片合计 %d 张，超过上限 %d 张（%d 秒档）" % (total_images, cap, args.duration))
    return body, model, price, limit


def _review_sha(project):
    """读 .muse-review.json 的指纹；没有就返回 None。"""
    p = os.path.join(project, ".muse-review.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("sheet_sha")
    except Exception:
        return None


def check_approve(project, duration, price):
    """用户点头闸门：没有 quote --approve 的记录就不许花钱。

    由来：2026-11-21 折叠电动车那单，同一轮里先 quote 再 --yes 提交，用户根本没机会看价格，
    ¥1.00 花出去、出了条前 30 秒无声的片子。规则写在 SKILL.md 里没人执行，就做成机械闸门。
    """
    p = os.path.join(project, APPROVE_FILE)
    if not os.path.exists(p):
        die("没有用户点头记录（%s）。\n"
            "        流程：quote 看价 -> 把价格给用户 -> 他明确同意 -> "
            "quote --approve \"用户原话\" 落记录 -> 再 submit。\n"
            "        跳过这一步就是擅自花钱，不许。" % p, 2)
    try:
        with open(p, encoding="utf-8") as f:
            rec = json.load(f)
    except Exception as e:
        die("点头记录读不出来（%s）：%s" % (p, e), 2)
    if int(rec.get("duration", 0)) != int(duration):
        die("点头记录是 %s 秒档，本次要 %d 秒——档位不同、价格不同，重新 quote --approve"
            % (rec.get("duration"), duration), 2)
    if abs(float(rec.get("price", 0)) - float(price)) > 0.01:
        die("点头记录是 ¥%s，本次算出来 ¥%.2f，对不上，重新确认"
            % (rec.get("price"), price), 2)
    # 2026-11-21 用户定的规矩：「改完了还得问我啊，不是改完就能生视频了，写死了」
    # 点头记录必须绑定审片单指纹：改了任何东西 -> 审片单重跑 -> 指纹变 -> 这条点头作废。
    cur = _review_sha(project)
    rec_rev = rec.get("review_sha")
    if not rec_rev:
        die("这条点头记录没有绑定审片单——先跑 review_sheet.py，把审片单给用户看，"
            "再用 quote --approve \"用户原话\" 落记录", 2)
    if cur is None:
        die("找不到 .muse-review.json——先跑 review_sheet.py", 2)
    if rec_rev != cur:
        die("审片单在用户点头之后又变过（你改了脚本卡 / prompt / 图片 / 时长）。\n"
            "        规矩：改完必须重新给用户看一遍，再拿一次点头。不许改完直接提交。", 2)

    age = time.time() - float(rec.get("ts", 0))
    if age > APPROVE_TTL:
        die("点头记录是 %.1f 小时前的，已过期（上限 %d 小时）——价格和内容可能都变了，重新给用户看一遍"
            % (age / 3600.0, APPROVE_TTL // 3600), 2)
    print("用户点头 : 「%s」（%s，%.1f 小时前）"
          % (str(rec.get("quote", ""))[:50], rec.get("at", "?"), age / 3600.0))


def cmd_quote(args):
    limit, model, price = tier_for(args.duration)
    segs = (args.duration + 9) // 10 if args.duration > 10 else 1
    print("请求时长 : %d 秒" % args.duration)
    print("计费档位 : %d 秒档 -> %s" % (limit, model))
    print("本次费用 : ¥%.2f（按次计费，不按秒）" % price)
    print("平台拆段 : %d 段（每段 <=10 秒，段间末帧续接）" % segs)
    if args.duration != limit:
        print("提示     : 同档可写到 %d 秒不加钱——内容够就写满，不够就别硬凑" % limit)
    print("预计耗时 : %d–%d 分钟" % (segs * 3, segs * 6))
    if args.approve:
        d = os.path.abspath(args.dir)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, APPROVE_FILE)
        rev = _review_sha(d)
        if not rev:
            die("没有 .muse-review.json——先跑 review_sheet.py 把审片单给用户看，"
                "拿到他的点头再落记录。不许跳过审片单。", 2)
        rec = {"duration": args.duration, "price": price, "model": model,
               "quote": args.approve.strip(), "ts": time.time(),
               "review_sha": rev,
               "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        with open(p, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        print("用户点头 : 已记录 -> %s" % p)
        print("           原话「%s」" % args.approve.strip()[:60])
        print("           12 小时内有效，且只对 %d 秒档生效" % args.duration)


def cmd_submit(args):
    cfg = load_config()
    if not cfg["api_key"]:
        die("没有 API key：写进 config.json 的 api_key 或设 MUSE_API_KEY")
    body, model, price, limit = build_body(args)
    print("模型 %s / 时长 %d 秒 / 费用 ¥%.2f / 画幅 %s" % (model, args.duration, price, args.aspect))
    if args.dry_run:
        preview = dict(body)
        for k in ("first_image_url", "last_image_url"):
            if k in preview and preview[k].startswith("data:"):
                preview[k] = preview[k][:60] + "…(%d chars)" % len(preview[k])
        if "reference_image_urls" in preview:
            preview["reference_image_urls"] = [
                (v[:60] + "…" if v.startswith("data:") else v) for v in preview["reference_image_urls"]
            ]
        preview["prompt"] = preview["prompt"][:200] + "…"
        print(json.dumps(preview, ensure_ascii=False, indent=1))
        return
    if not args.yes:
        die("没带 --yes。先跑 quote 把价格给用户看，确认后再提交", 2)

    # 闸门：令牌（preflight 签发）+ 账本（禁止重复提交）
    led, project = gate(args)
    check_approve(project, args.duration, price)

    url = cfg["api_base"].rstrip("/") + "/videos"
    status, resp = http_json("POST", url, cfg["api_key"], body)
    print("HTTP %s" % status)
    if status >= 400:
        # 提交没成功，把刚才 --replace 打的「待取代」标记撤掉，别让账本缺一块
        if args.replace:
            led2 = load_ledger()
            for t in led2["tasks"]:
                if t.get("superseded_by") == "pending":
                    t["superseded_by"] = None
            save_ledger(led2)
        if status == 429:
            die("队列满（429）。**不要重试**，这条请求可能已被受理也可能没有——"
                "先跑 `muse_video.py ledger --refresh` 看有没有多出任务，再决定", 4)
        die("提交失败：%s" % json.dumps(resp, ensure_ascii=False)[:600])
    tid = resp.get("id") or resp.get("task_id") or (resp.get("data") or {}).get("id")
    if not tid:
        die("返回里没有任务 id：%s" % json.dumps(resp, ensure_ascii=False)[:600])
    print("任务 id : %s" % tid)
    print("状态    : %s" % resp.get("status", "queued"))
    out_dir = args.out or "."
    os.makedirs(out_dir, exist_ok=True)
    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "task_id": tid,
        "model": model,
        "duration": args.duration,
        "aspect_ratio": args.aspect,
        "price_cny": price,
        "prompt_file": os.path.abspath(args.prompt_file),
        "images": {
            "first": args.first,
            "last": args.last,
            "refs": args.ref,
        },
    }
    with open(os.path.join(out_dir, "muse-submit-%s.json" % tid), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    print("记账    : %s" % os.path.join(out_dir, "muse-submit-%s.json" % tid))

    led["tasks"].append({
        "id": tid, "project": project, "status": resp.get("status", "queued"),
        "duration": args.duration, "model": model, "price": price,
        "created": rec["ts"], "ts_epoch": time.time(), "refund": "—",
        "prompt": os.path.abspath(args.prompt_file),
        "prompt_sha": sha_file(args.prompt_file)[:16],
        "images": {"first": args.first, "last": args.last, "refs": args.ref or []},
        "out_dir": os.path.abspath(out_dir),
        "replaces": args.replace or None,
    })
    if args.replace:
        for t in led["tasks"]:
            if t.get("superseded_by") == "pending":
                t["superseded_by"] = tid
    save_ledger(led)
    print("账本    : %s" % LEDGER_PATH)
    print("下一步  : python muse_video.py wait --id %s" % tid)
    print("⚠️  这条在跑完之前不许再提交任何任务（闸门会拦）")


def cmd_wait(args):
    cfg = load_config()
    url = cfg["api_base"].rstrip("/") + "/videos/" + args.id
    start = time.time()
    last = None
    while True:
        status, resp = http_json("GET", url, cfg["api_key"], timeout=60)
        if status >= 400:
            die("查询失败 HTTP %s：%s" % (status, json.dumps(resp, ensure_ascii=False)[:400]))
        st = resp.get("status", "?")
        pg = resp.get("progress")
        line = "  %s  status=%s%s" % (
            time.strftime("%H:%M:%S"),
            st,
            "" if pg is None else " progress=%s%%" % pg,
        )
        if line != last:
            print(line, flush=True)
            last = line
        if st == "completed":
            print("完成，用时 %.1f 分钟" % ((time.time() - start) / 60))
            print(json.dumps(resp, ensure_ascii=False)[:800])
            led = load_ledger()
            t = find_task(led, args.id)
            if t:
                t["status"] = "completed"
                t["completed"] = time.strftime("%Y-%m-%d %H:%M:%S")
                save_ledger(led)
            print("下一步  : python muse_video.py download --id %s --out <目录> --name <片名>" % args.id)
            return
        if st == "failed":
            err = resp.get("error") or {}
            led = load_ledger()
            t = find_task(led, args.id)
            if t:
                t["status"] = "failed"
                t["refund"] = "pending"
                t["error"] = json.dumps(err, ensure_ascii=False)[:400]
                save_ledger(led)
            die("任务失败：%s\n失败会退款，但**在确认退款到账之前不许提交新任务**。"
                "去 /wallet 看余额，再跑：muse_video.py refund --id %s --balance-before A --balance-after B"
                % (json.dumps(err, ensure_ascii=False)[:400], args.id))
        if time.time() - start > args.timeout:
            die("超时 %d 秒，任务仍在跑。稍后再 wait，不要重新提交" % args.timeout)
        time.sleep(args.interval)


def probe(path):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration,size,bit_rate", "-show_entries",
             "stream=codec_type,codec_name,width,height,r_frame_rate",
             "-of", "json", path],
            stderr=subprocess.STDOUT,
        )
    except FileNotFoundError:
        print("  (没装 ffprobe，跳过实测)")
        return None
    except subprocess.CalledProcessError as e:
        print("  ffprobe 失败：%s" % e.output.decode("utf-8", "replace")[:300])
        return None
    return json.loads(out.decode("utf-8", "replace"))


def cmd_download(args):
    cfg = load_config()
    url = cfg["api_base"].rstrip("/") + "/videos/" + args.id + "/content"
    os.makedirs(args.out, exist_ok=True)
    name = args.name or args.id
    path = os.path.join(args.out, name + ".mp4")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + cfg["api_key"], "User-Agent": UA})
    print("下载中 …")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp, open(path, "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
    except urllib.error.HTTPError as e:
        die("下载失败 HTTP %s：%s" % (e.code, e.read().decode("utf-8", "replace")[:300]))
    size = os.path.getsize(path)
    print("已保存  : %s（%.2f MB）" % (path, size / 1048576))
    info = probe(path)
    if info:
        dur = float(info.get("format", {}).get("duration", 0))
        streams = info.get("streams", [])
        v = next((s for s in streams if s.get("codec_type") == "video"), {})
        a = next((s for s in streams if s.get("codec_type") == "audio"), None)
        print("实测时长: %.3f 秒" % dur)
        print("画面    : %sx%s @ %s" % (v.get("width"), v.get("height"), v.get("r_frame_rate")))
        print("音轨    : %s" % ("有（%s）" % a.get("codec_name") if a else "无"))
    ledger = os.path.join(args.out, "muse-ledger.jsonl")
    with open(ledger, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "task_id": args.id,
            "file": os.path.abspath(path),
            "bytes": size,
            "duration": (probe(path) or {}).get("format", {}).get("duration"),
        }, ensure_ascii=False) + "\n")
    print("记账    : %s" % ledger)


def main():
    ap = argparse.ArgumentParser(description="muse 生视频（router.yizzav.com）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("quote", help="报价，不花钱")
    q.add_argument("--duration", type=int, required=True)
    q.add_argument("--approve", metavar="用户原话",
                   help="用户明确同意这次价格的原话；写进 .muse-approve.json，submit 必须读到才能提交")
    q.add_argument("--dir", default=".", help="点头记录写到哪个项目目录，默认当前目录")
    q.set_defaults(func=cmd_quote)

    s = sub.add_parser("submit", help="提交任务")
    s.add_argument("--prompt-file", required=True)
    s.add_argument("--duration", type=int, required=True)
    s.add_argument("--aspect", default="16:9")
    s.add_argument("--first")
    s.add_argument("--last")
    s.add_argument("--ref", action="append")
    s.add_argument("--style")
    s.add_argument("--camera")
    s.add_argument("--negative")
    s.add_argument("--out", default=".")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--project", help="项目目录（账本按项目判重），默认 prompt 所在目录")
    s.add_argument("--replace", help="重拍时填旧任务 id；不给就不许重复提交")
    s.add_argument("--why", help="重拍原因，配合 --replace 使用")
    s.set_defaults(func=cmd_submit)

    lg = sub.add_parser("ledger", help="看提交账本（谁在跑、谁失败待退款）")
    lg.add_argument("--refresh", action="store_true", help="顺手把在跑任务的状态刷一遍")
    lg.set_defaults(func=cmd_ledger)

    rf = sub.add_parser("refund", help="确认失败任务的退款到账")
    rf.add_argument("--id", required=True)
    rf.add_argument("--balance-before", type=float)
    rf.add_argument("--balance-after", type=float)
    rf.add_argument("--confirm", action="store_true", help="用户明确说退款到账时人工确认")
    rf.add_argument("--note")
    rf.set_defaults(func=cmd_refund)

    w = sub.add_parser("wait", help="轮询到完成")
    w.add_argument("--id", required=True)
    w.add_argument("--timeout", type=int, default=3600)
    w.add_argument("--interval", type=int, default=10)
    w.set_defaults(func=cmd_wait)

    d = sub.add_parser("download", help="下载并验收")
    d.add_argument("--id", required=True)
    d.add_argument("--out", default=".")
    d.add_argument("--name")
    d.set_defaults(func=cmd_download)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
