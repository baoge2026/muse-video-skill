# -*- coding: utf-8 -*-
"""提交前终极检查（preflight）：材料齐、prompt 对、台词账平、账本干净，才允许提交。

用法:
    python preflight.py --prompt p.txt --duration 50 --segs 5 \
        --lines 台词卡.txt --first 首帧.jpg --last 尾帧.jpg \
        --ref 人物.jpg --ref 道具.jpg \
        [--aspect 16:9] [--style "..."] [--camera "..."] [--negative "..."] \
        [--project 项目目录] [--profile full|test]

通过后会在 prompt 同目录写 `.muse-preflight.json`，里面是所有输入材料的指纹。
`muse_video.py submit` 会重算指纹，**对不上就拒绝提交**——防止「检查完又改 prompt 直接发」。

检查分六组:
    A 计费   时长/档位/价格
    B prompt 时间轴标签、每段语速额度标注（字数必须等于该段引号内台词字数）、
             光照卡、风格锚、声轨开关、禁用写法
    C 台词   台词卡跑 check_words 全套时间线核算，且每段字数与 prompt 一致
    D 素材   张数上限、单张 10MB、首帧、重复图
    E 账本   在跑任务 / 未确认退款 / 同项目已出片
    F 结论   全绿才写令牌

--profile test 只放宽 B/C 两组的「时间轴标签 + 额度标注」（念稿测试这类单镜头片没有多段时间轴），
素材、账本、声轨开关照样查。正片一律 full。
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_words as cw  # noqa: E402

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER_PATH = os.path.join(SKILL_DIR, "ledger.json")
TIERS = [(10, "muse-video-10s", 0.20), (20, "muse-video-20s", 0.40), (30, "muse-video-30s", 0.60),
         (40, "muse-video-40s", 0.80), (50, "muse-video-50s", 1.00), (60, "muse-video-60s", 1.20)]

SEG_HDR = re.compile(r"【\s*(\d+(?:\.\d+)?)\s*[-–—~至到]\s*(\d+(?:\.\d+)?)\s*秒\s*】")
QUOTA = re.compile(r"〔\s*本段\s*(\d+)\s*字\s*[·・.、\s]\s*(稳|快|紧|极限)\s*([\d.]+)\s*字/秒"
                   r"\s*[·・.、]?\s*约?\s*([\d.]+)\s*秒\s*〕")
QUOTE = re.compile(r"[“\"]([^”\"]{1,200})[”\"]")
TIMECODE = re.compile(r"第\s*[0-9一二三四五六七八九十]+\s*(?:\.\d+)?\s*秒\s*(?:开口|开口说|开始说|起说)")
CODING = re.compile(r"(?<![A-Za-z])[A-Z]\d{1,2}(?!\d)")


class Report:
    def __init__(self):
        self.items = []          # (level, group, msg)  level: ok/warn/err

    def ok(self, g, m):
        self.items.append(("ok", g, m))

    def warn(self, g, m):
        self.items.append(("warn", g, m))

    def err(self, g, m):
        self.items.append(("err", g, m))

    def errors(self):
        return [i for i in self.items if i[0] == "err"]

    def warns(self):
        return [i for i in self.items if i[0] == "warn"]

    def dump(self):
        icon = {"ok": "[ok]  ", "warn": "[提醒]", "err": "[必改]"}
        for lv, g, m in self.items:
            print("%s %s %s" % (icon[lv], g, m))


def tier_of(duration):
    for lim, model, price in TIERS:
        if duration <= lim:
            return lim, model, price
    raise SystemExit("时长超过 60 秒，平台不支持")


def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(prompt_file, duration, aspect, first, last, refs, style, camera, negative):
    h = hashlib.sha256()
    with open(prompt_file, "rb") as f:
        h.update(f.read())
    for v in (duration, aspect, style or "", camera or "", negative or ""):
        h.update(("|%s" % v).encode("utf-8"))
    for p in [first, last] + list(refs or []):
        h.update(("|%s" % (sha_file(p) if p and os.path.exists(p) else "")).encode("utf-8"))
    return h.hexdigest()


def probe_size(path):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
            stderr=subprocess.DEVNULL, timeout=20)
        w, h = out.decode().strip().split(",")[:2]
        return int(w), int(h)
    except Exception:
        return None


def check_billing(rep, duration):
    lim, model, price = tier_of(duration)
    rep.ok("A 计费", "时长 %d 秒 → %s → ¥%.2f（同档可写到 %d 秒不加钱）" % (duration, model, price, lim))
    return lim, model, price


def parse_prompt(text):
    """返回 [(start,end,block_text)]，按【A-B秒】切块。"""
    blocks, marks = [], []
    for m in SEG_HDR.finditer(text):
        marks.append((float(m.group(1)), float(m.group(2)), m.start(), m.end()))
    for i, (a, b, _s, e) in enumerate(marks):
        nxt = marks[i + 1][2] if i + 1 < len(marks) else len(text)
        blocks.append((a, b, text[e:nxt]))
    return blocks


def spoken_chars(block):
    """该段所有引号内台词的字数（去掉舞台提示括号）。"""
    return sum(cw.count(x) for x in QUOTE.findall(block))


def check_prompt(rep, text, duration, segs, lines_file, profile):
    if not text.strip():
        rep.err("B prompt", "prompt 是空的")
        return None

    # 光照卡 / 风格锚 / 负面
    if "光照卡" not in text and "光照" not in text:
        rep.err("B prompt", "没有光照卡——逐段亮度会漂（实测跨任务起点亮度差 37.6%）")
    else:
        need = [("色温", "色温"), ("光比", "光比"), ("投影", "投影方向")]
        miss = [n for k, n in need if k not in text]
        if miss:
            rep.warn("B prompt", "光照卡缺量化项：%s——只写「光线统一」没用，要给数值" % "、".join(miss))
        else:
            rep.ok("B prompt", "光照卡有量化项（色温/光比/投影）")
    if "风格" not in text and "质感" not in text:
        rep.warn("B prompt", "没有风格锚，画风可能在段间漂移")

    # 禁用写法
    tc = TIMECODE.findall(text)
    if tc:
        rep.warn("B prompt", "出现时间码「%s」——实测它不照时间码开口，只按字数念，白占篇幅" % tc[0])
    cd = CODING.findall(text)
    if cd:
        rep.warn("B prompt", "出现编号 %s——prompt 里要用称呼（「青灰官袍的官员」），编号只活在资产库里"
                 % ",".join(sorted(set(cd))[:5]))

    blocks = parse_prompt(text)
    if profile == "test":
        if not blocks:
            rep.ok("B prompt", "test 模式：不要求多段时间轴")
        return blocks

    # 时间轴
    if len(blocks) != segs:
        rep.err("B prompt", "时间轴标签 %d 个，但 duration=%d 要拆 %d 段——标签必须一段一个，"
                            "平台就是照这个推进" % (len(blocks), duration, segs))
    else:
        bad = []
        for i, (a, b, _) in enumerate(blocks):
            exp_a, exp_b = i * 10, min((i + 1) * 10, duration)
            if abs(a - exp_a) > 0.01 or abs(b - exp_b) > 0.01:
                bad.append("第%d段标成 %.0f-%.0f，应为 %.0f-%.0f" % (i + 1, a, b, exp_a, exp_b))
        if bad:
            rep.err("B prompt", "时间轴不连续：" + "；".join(bad))
        else:
            rep.ok("B prompt", "时间轴 %d 段连续，0–%d 秒对齐" % (len(blocks), duration))

    # 每段：额度标注 + 台词字数 + 声轨开关
    total_declared = 0
    for i, (a, b, block) in enumerate(blocks, 1):
        seg_len = b - a
        spoken = spoken_chars(block)
        qs = QUOTA.findall(block)
        if not qs:
            if spoken > 0:
                rep.err("B prompt", "第%d段有 %d 字台词，但没写语速额度标注"
                                    "〔本段 N 字 · 档位 · 约 X 秒〕——不许给没设语速的段落放任多少字"
                        % (i, spoken))
            else:
                if not re.search(r"此段无人声|无人开口|无对白|无台词", block):
                    rep.err("B prompt", "第%d段既没台词也没写「此段无人声」——模型会自己加人声" % i)
            continue
        if len(qs) > 1:
            rep.err("B prompt", "第%d段有 %d 个额度标注，一段只留一个" % (i, len(qs)))
        n, tier, rate, secs = qs[0]
        n, rate, secs = int(n), float(rate), float(secs)
        total_declared += n
        decl_rate = cw.TIER_RATE.get(tier)
        if decl_rate is None:
            rep.err("B prompt", "第%d段档位「%s」不认识" % (i, tier))
        elif abs(rate - decl_rate) > 0.05:
            rep.err("B prompt", "第%d段写 %s 档却标 %.1f 字/秒，实测该档是 %.1f" % (i, tier, rate, decl_rate))
        if abs(secs - n / max(rate, 0.01)) > 0.5:
            rep.err("B prompt", "第%d段 %.0f 字 ÷ %.1f 字/秒 = %.1f 秒，标注却写约 %.1f 秒"
                    % (i, n, rate, n / max(rate, 0.01), secs))
        if n != spoken:
            rep.err("B prompt", "第%d段标注 %d 字，段内引号台词实际 %d 字——标注和台词对不上"
                    % (i, n, spoken))
        if n > seg_len * cw.HARD_RATE + 0.5:
            rep.err("B prompt", "第%d段 %d 字 = %.1f 字/秒，超实测红线 %.1f，必吞字"
                    % (i, n, n / seg_len, cw.HARD_RATE))
        if n + 0.5 < spoken and spoken > 0:
            rep.warn("B prompt", "第%d段标注比实际台词少，模型按字数念、不看标注" % i)
        if spoken == 0 and not re.search(r"此段无人声|无人开口|无对白|无台词", block):
            rep.err("B prompt", "第%d段写了额度却没有台词，或漏了「此段无人声」" % i)
        # 声轨开关
        if spoken and not re.search(r"无人开口|口型|此段无人声|无对白", block):
            rep.warn("B prompt", "第%d段有人声但没写「画面中无人开口，无口型」这类开关" % i)

    if total_declared:
        rep.ok("B prompt", "全片台词标注合计 %d 字（%.1f 字/秒 平均）"
               % (total_declared, total_declared / duration))

    # 与台词卡交叉核对
    if lines_file:
        if not os.path.exists(lines_file):
            rep.err("C 台词", "台词卡不存在：%s" % lines_file)
        else:
            rows, probs = cw.load(lines_file, 10.0)
            for p in probs:
                rep.err("C 台词", p)
            cw.attach_context(rows)
            res = cw.analyze(rows, 10.0, segs)
            for e in res["errors"]:
                rep.err("C 台词", e)
            for w in res["warns"]:
                rep.warn("C 台词", w)
            if not res["errors"]:
                rep.ok("C 台词", "时间线核算通过：每句都在可用窗口内，片尾 %.1fs 收住"
                       % max((r["end"] for r in rows), default=0))
            # 每段字数必须和 prompt 标注一致
            mism = 0
            for s in res["segments"]:
                seg_lines = [r for r in rows if r["seg"] == s["seg"]]
                if not seg_lines:
                    continue
                if s["seg"] <= len(blocks):
                    spoken = spoken_chars(blocks[s["seg"] - 1][2])
                    if abs(spoken - s["chars"]) > 0.5:
                        mism += 1
                        rep.err("C 台词", "第%d段：台词卡 %d 字，prompt 引号内 %d 字——必须一致"
                                % (s["seg"], s["chars"], spoken))
            if not mism:
                rep.ok("C 台词", "台词卡与 prompt 每段字数一致（%d 段）" % len(res["segments"]))
    else:
        rep.warn("C 台词", "没给 --lines 台词卡，跳过时间线核算（正片必须给）")

    return blocks


def check_media(rep, duration, first, last, refs):
    cap = 10 if duration <= 10 else 9
    paths = ([("首帧", first)] if first else []) + ([("尾帧", last)] if last else []) \
        + [("参考图", r) for r in (refs or [])]
    if not paths:
        if duration > 10:
            rep.err("D 素材", "长片没给首帧——不给 first 时第一张参考图会顶当首帧，构图不受控")
        else:
            rep.warn("D 素材", "纯文生：风格必跑偏（实测跑成北欧海崖），正片必须给首帧或参考图")
    for kind, p in paths:
        if p.startswith("http"):
            continue
        if not os.path.exists(p):
            rep.err("D 素材", "%s不存在：%s" % (kind, p))
            continue
        mb = os.path.getsize(p) / 1048576
        if mb > 10:
            rep.err("D 素材", "%s %.1f MB 超 10 MB：%s" % (kind, mb, p))
        wh = probe_size(p)
        if wh and min(wh) < 480:
            rep.warn("D 素材", "%s 只有 %dx%d，太小学不出细节：%s" % (kind, wh[0], wh[1], p))
    if len(paths) > cap:
        rep.err("D 素材", "图片合计 %d 张 > 上限 %d 张（%d 秒档）" % (len(paths), cap, duration))
    else:
        rep.ok("D 素材", "图片 %d / %d 张，单张均 ≤10 MB" % (len(paths), cap))
    if last and not first:
        rep.err("D 素材", "给了尾帧却没给首帧——接口会拒绝")
    names = [os.path.abspath(p) for _k, p in paths if not p.startswith("http")]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        rep.warn("D 素材", "同一张图被重复使用（%d 处）——白占额度" % len(dup))
    for _k, p in paths:
        if p.lower().endswith((".gif", ".bmp", ".webp")):
            rep.warn("D 素材", "格式 %s 可能不被接受，转成 jpg/png" % os.path.splitext(p)[1])


def load_ledger():
    if not os.path.exists(LEDGER_PATH):
        return {"tasks": []}
    with open(LEDGER_PATH, encoding="utf-8") as f:
        return json.load(f)


def check_version(rep):
    """技能版本新鲜度：GitHub 是唯一真源，落后就拦提交（能连上时）。"""
    def g(*args, **kw):
        try:
            p = subprocess.run(("git",) + args, cwd=SKILL_DIR, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=kw.get("timeout", 10))
            return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return 127, "", "git 不可用或超时"

    code, _, _ = g("rev-parse", "--is-inside-work-tree", timeout=5)
    if code != 0:
        rep.ok("G 版本", "非 git 安装，跳过版本检查")
        return

    # 统一走 sync.py --check，避免两套分支/远端判定逻辑漂移
    sync_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync.py")
    try:
        p = subprocess.run([sys.executable, sync_py, "--check", "--json"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        info = json.loads(p.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        rep.warn("G 版本", "同步检查执行失败（%s），按本机版本继续" % str(e)[:60])
        return

    status = info.get("status", "")
    head = info.get("head", "?")
    remote = info.get("remote_head") or head

    if status == "behind":
        rep.err("G 版本",
                "本机 %s 落后 GitHub 上的 %s 共 %d 个提交——先跑 `python scripts/sync.py` "
                "更新再提交任务，否则是按旧规则算账、花钱"
                % (head, remote, info.get("behind", 0)))
    elif info.get("offline"):
        rep.warn("G 版本",
                 "连不上 GitHub，按本机 %s 的规则继续（离线，无法确认是否最新）" % head)
    elif info.get("pending_push"):
        rep.warn("G 版本",
                 "本机领先远端 %d 个提交（改动还没推）——push 之前别的机器拿不到这些规则"
                 % info.get("ahead", 0))
        rep.ok("G 版本", "技能已是最新 %s" % head)
    else:
        rep.ok("G 版本", "技能已与 GitHub 一致 %s" % head)

    _, st, _ = g("status", "--porcelain", timeout=10)
    dirty = [l for l in (st or "").splitlines() if l.strip()]
    if dirty:
        rep.warn("G 版本", "技能目录有 %d 个未提交改动——改完必须 commit+push，"
                           "否则别的机器拉不到这些优化" % len(dirty))


def check_ledger(rep, project, replace="", why=""):
    led = load_ledger()
    tasks = led.get("tasks", [])
    open_t = [t for t in tasks if t.get("status") in ("queued", "in_progress")]
    if open_t:
        for t in open_t:
            rep.err("E 账本", "已有任务在跑：%s（%s，%s）——禁止并发/重复提交，先 wait 完这一条"
                    % (t["id"], t.get("status"), t.get("created", "")))
    unpaid = [t for t in tasks if t.get("status") == "failed" and t.get("refund") != "confirmed"]
    if unpaid:
        for t in unpaid:
            rep.err("E 账本", "任务 %s 失败但退款未确认——先在 /wallet 看余额，"
                              "跑 `muse_video.py refund --id %s --balance-before A --balance-after B`"
                    % (t["id"], t["id"]))
    done = [t for t in tasks if t.get("status") == "completed" and t.get("project") == project
            and not t.get("superseded_by")]
    if done:
        replaced = next((t for t in done if t["id"] == replace), None)
        if replace and replaced and why:
            rep.ok("E 账本", "重拍：将取代 %s（原因：%s）——旧片存档，不覆盖" % (replace, why))
        else:
            for t in done:
                rep.err("E 账本", "本项目已出片：%s（%s，¥%.2f）——确认要重拍请带 "
                                  "`--replace %s --why \"原因\"`" %
                        (t["id"], t.get("created", ""), t.get("price", 0), t["id"]))
    if not (open_t or unpaid or done):
        rep.ok("E 账本", "无在跑任务、无待确认退款、本项目未出片")


def main():
    ap = argparse.ArgumentParser(description="muse 提交前终极检查")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--duration", type=int, required=True)
    ap.add_argument("--segs", type=int, default=0, help="段数，默认按 duration 算")
    ap.add_argument("--lines")
    ap.add_argument("--first")
    ap.add_argument("--last")
    ap.add_argument("--ref", action="append")
    ap.add_argument("--aspect", default="16:9")
    ap.add_argument("--style")
    ap.add_argument("--camera")
    ap.add_argument("--negative")
    ap.add_argument("--project", default="")
    ap.add_argument("--profile", choices=["full", "test"], default="full")
    ap.add_argument("--replace", default="", help="重拍时填旧任务 id")
    ap.add_argument("--why", default="", help="重拍原因，配合 --replace")
    ap.add_argument("--skip-version", action="store_true", help="跳过远端版本检查（离线时用）")
    args = ap.parse_args()

    segs = args.segs or ((args.duration + 9) // 10)
    project = os.path.abspath(args.project or os.path.dirname(os.path.abspath(args.prompt)))
    # 每次检查先把旧令牌删掉：任何一次没过都不能留着上一轮的绿牌
    tok_path = os.path.join(os.path.dirname(os.path.abspath(args.prompt)), ".muse-preflight.json")
    if os.path.exists(tok_path):
        os.remove(tok_path)
    rep = Report()
    check_billing(rep, args.duration)

    text = ""
    if not os.path.exists(args.prompt):
        rep.err("B prompt", "prompt 文件不存在：%s" % args.prompt)
    else:
        with open(args.prompt, encoding="utf-8") as f:
            text = f.read()
        check_prompt(rep, text, args.duration, segs, args.lines, args.profile)
    check_media(rep, args.duration, args.first, args.last, args.ref or [])
    check_ledger(rep, project, args.replace, args.why)
    if not args.skip_version:
        check_version(rep)

    if args.negative is not None:
        need = ["字幕", "水印"]
        miss = [n for n in need if n not in args.negative]
        if miss:
            rep.warn("D 素材", "negative 里缺 %s——实测会画进字幕/水印" % "、".join(miss))
    else:
        rep.warn("D 素材", "没设 negative_prompt，至少带「字幕, 水印, 文字, 现代建筑, 电线」")

    print()
    rep.dump()
    print()
    warns, errs = rep.warns(), rep.errors()
    print("—" * 60)
    print("必改 %d 项 / 提醒 %d 项" % (len(errs), len(warns)))
    if errs:
        print("\n没过，禁止提交。逐条改完再跑一次 preflight。")
        return 1

    token = {
        "ts": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        "fingerprint": fingerprint(args.prompt, args.duration, args.aspect,
                                   args.first, args.last, args.ref, args.style,
                                   args.camera, args.negative),
        "duration": args.duration, "segs": segs, "profile": args.profile,
        "project": project, "prompt": os.path.abspath(args.prompt),
        "lines": os.path.abspath(args.lines) if args.lines else None,
        "images": {"first": args.first, "last": args.last, "refs": args.ref or []},
        "replace": args.replace or None,
        "why": args.why or None,
        "warns": [i[2] for i in warns],
    }
    out = os.path.join(os.path.dirname(os.path.abspath(args.prompt)), ".muse-preflight.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(token, f, ensure_ascii=False, indent=1)
    print("全绿，已签发提交令牌：%s" % out)
    print("令牌绑定本次 prompt + 图片 + 参数指纹；改了任何一项都会失效，必须重跑。")
    print("指纹 %s" % token["fingerprint"][:16])
    return 0


if __name__ == "__main__":
    sys.exit(main())
