# -*- coding: utf-8 -*-
"""拍摄计划（plan_shoot）：从脚本卡算出「拍几拍、多少钱、要生哪些图」。

为什么要有这个脚本（2026-11-21 用户原话）：

    「文案质量、视频时间、资产图参考图，要么该生成的多的我不讲就只生成一两张，
      要么情节非常拖沓浪费钱，我很无语。资产图管够，生图我们的渠道很便宜，生视频是很贵的。」

两条成本事实决定了整条流水线该往哪使劲：

    生图  ¥0.0035–0.02 / 张        ← 便宜，管够，用来试错
    生视频 ¥0.20 / 10 秒 = ¥1.20 / 分钟  ← 贵，一秒钟顶 50 张图

所以：**图管够，视频管少。**
这个脚本把「该生几张图」从"看心情"变成"照清单执行"——
它会按脚本卡逐拍列出分镜图 prompt、人物定妆图、道具图，并算出 API 只能收 9 张时该挑哪 9 张。

用法:
    python plan_shoot.py 脚本卡.txt --segs 5
    python plan_shoot.py 脚本卡.txt --segs 5 --prompt p.txt --out 拍摄计划.md
    python plan_shoot.py 脚本卡.txt --segs 5 --candidates 3 --img-price 0.0035 --json

退出码：0 = 计划成立；1 = 有必改项（拍数超默认、缺人物设定等）；2 = 读不了。
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_words as cw   # noqa: E402
import check_story as cs   # noqa: E402

TIERS = [(10, "muse-video-10s", 0.20), (20, "muse-video-20s", 0.40),
         (30, "muse-video-30s", 0.60), (40, "muse-video-40s", 0.80),
         (50, "muse-video-50s", 1.00), (60, "muse-video-60s", 1.20)]

# 默认拍数。用户反复说「拖沓、浪费钱」，所以默认值压到 3 拍，往上要理由。
DEFAULT_BEATS = 3
MAX_BEATS = 5          # 6 拍（60 秒）需要显式 --allow-60

IMG_CAP = {10: 10, 999: 9}   # 首帧+尾帧+参考图合计上限


def tier_of(duration):
    for lim, model, price in TIERS:
        if duration <= lim:
            return lim, model, price
    raise SystemExit("时长超过 60 秒，平台不支持")


def grab_block(prompt_text, head):
    """从 prompt 里抠出【风格锚】/【光照卡】那一段。"""
    m = re.search(r"【%s[^】]*】\s*(.+?)(?=\n\s*\n|\n【|\Z)" % re.escape(head),
                  prompt_text, re.S)
    return m.group(1).strip() if m else ""


def main():
    ap = argparse.ArgumentParser(description="拍摄计划：拍数 / 报价 / 要生哪些图")
    ap.add_argument("script_card")
    ap.add_argument("--segs", type=int, default=0)
    ap.add_argument("--duration", type=float, default=0,
                    help="片长总秒数；不给就按拍数×10。给了就按平台真实拆段算（25→9+8+8）")
    ap.add_argument("--prompt", help="已有 prompt，用它的风格锚和光照卡拼图 prompt")
    ap.add_argument("--candidates", type=int, default=3, help="每拍生成几张候选分镜（默认 3）")
    ap.add_argument("--img-price", type=float, default=0.0035, help="单张图价格（默认 ¥0.0035）")
    ap.add_argument("--arc", choices=["drama", "kids"], default="drama")
    ap.add_argument("--allow-60", action="store_true", help="允许 6 拍 60 秒（默认不许）")
    ap.add_argument("--out", help="把计划写成 markdown")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.script_card):
        print("脚本卡不存在：%s" % a.script_card, file=sys.stderr)
        return 2
    meta, rows, problems = cs.load(a.script_card)
    if problems:
        for p in problems:
            print("[必改] 格式 %s" % p, file=sys.stderr)
        return 1

    bs = cs.beats(rows)
    beats = len(bs)
    duration = a.duration or (a.segs or beats) * 10
    lens = cw.split_segments(duration)
    lim, model, vprice = tier_of(duration)

    rep = cs.Report()
    # ── 拍数闸门：默认 3 拍 ────────────────────────────────────────
    if len(lens) != beats:
        rep.err("拍数", "%g 秒会被平台拆成 %d 段（%s），但脚本卡是 %d 拍——"
                        "拍数必须等于段数" % (duration, len(lens),
                                              "+".join("%g" % x for x in lens), beats))
    if beats > MAX_BEATS and not a.allow_60:
        rep.err("拍数", "%d 拍 = %d 秒 = ¥%.2f。超过 %d 拍要显式 --allow-60，"
                        "并且先回答「删掉哪一拍故事还成立」" % (beats, duration, vprice, MAX_BEATS))
    elif beats > DEFAULT_BEATS:
        rep.warn("拍数", "%d 拍 = %d 秒 = ¥%.2f。默认是 %d 拍（¥%.2f）——"
                         "多出来的 %.2f 元要能说出多给了观众什么" %
                (beats, duration, vprice, DEFAULT_BEATS, tier_of(DEFAULT_BEATS * 10)[2],
                 vprice - tier_of(DEFAULT_BEATS * 10)[2]))
    else:
        rep.ok("拍数", "%d 拍 = %g 秒 = ¥%.2f（默认档位，不拖沓；拆成 %s）"
               % (beats, duration, vprice, "+".join("%g" % x for x in lens)))

    # ── 风格锚 / 光照卡 ────────────────────────────────────────────
    style = (meta.get("风格锚") or "").strip()
    light = (meta.get("光照卡") or "").strip()
    if a.prompt and os.path.exists(a.prompt):
        pt = open(a.prompt, encoding="utf-8").read()
        style = style or grab_block(pt, "风格锚")
        light = light or grab_block(pt, "光照卡")
    if not style:
        rep.warn("风格", "没有风格锚（用 --prompt 传已有 prompt，或在脚本卡里加【风格锚】）——"
                         "分镜图和成片的画风会不一致")
    light_short = ""
    if light:
        keep = [l.strip() for l in light.splitlines()
                if any(k in l for k in ("时段", "色温", "主光", "投影", "天空", "环境"))]
        light_short = "；".join(keep)

    # ── 分镜图清单 ────────────────────────────────────────────────
    shots = []
    for seg, role, visual, seg_rows in bs:
        clauses = [c.strip() for c in re.split(r"[；;。→]", visual or "") if c.strip()]
        # 机位那一句单独拿出来，不当作"定格瞬间"
        cam = [c for c in clauses if ("机位" in c or "景别" in c or "平视" in c
                                      or "俯拍" in c or "仰拍" in c or "跟住" in c)]
        acts = [c for c in clauses if c not in cam] or clauses
        for k in range(a.candidates):
            tag = "ABCDE"[k] if k < 5 else str(k + 1)
            # 候选不是同一张图重复三遍：每个候选定格在动作链的**不同瞬间**
            moment = acts[k] if k < len(acts) else acts[-1]
            lead = acts[0] if (k > 0 and len(acts) > 1) else ""
            scene = ("场景起手：" + lead + "；" if lead and lead != moment else "")
            shots.append({
                "kind": "分镜", "seg": seg, "role": role, "variant": tag,
                "name": "拍%02d-%s" % (seg, tag),
                "moment": moment,
                "prompt": "｜".join(x for x in [
                    style,
                    light_short,
                    scene + "画面定格在这一瞬间：" + moment,
                    "；".join(cam),
                    "单张实景帧，16:9 横构图，主体完整、边缘清晰",
                    "无字幕、无水印、无任何文字、不是多格资产板、没有分格线和编号",
                ] if x),
            })

    # ── 定妆图 / 道具图（从脚本卡的【人物】【道具】读）──────────────
    cast = []
    for key in ("人物", "角色", "道具"):
        v = meta.get(key, "")
        if not v:
            continue
        for item in re.split(r"[；;\n]", v):
            item = item.strip()
            if not item:
                continue
            cast.append({
                "kind": key, "seg": 0, "role": key, "variant": "-",
                "name": "%s-%s" % (key, item.split("：")[0].split(":")[0][:8]),
                "prompt": "｜".join(x for x in [
                    style, light_short, item,
                    "正面／四分之三侧面／全身各一张，纯色浅灰背景，全身完整入画",
                    "无字幕、无水印、无文字、不是多格资产板",
                ] if x),
            })
    if not meta.get("人物") and not meta.get("角色"):
        rep.warn("定妆", "脚本卡里没有【人物】——没有定妆图，人物会在段间换脸换衣服。"
                         "生图便宜，定妆图至少 3 张（正面/侧面/全身）")

    # ── API 只能收 9 张：先分配 ────────────────────────────────────
    cap = IMG_CAP[10] if duration <= 10 else IMG_CAP[999]
    api_plan = []
    if beats >= 1:
        api_plan.append(("首帧", "拍01-A", "第一段的开场构图，必须是实景帧"))
    if beats >= 2:
        api_plan.append(("尾帧", "拍%02d-A" % beats, "最后一段的收尾画面（必须同时给首帧）"))
    left = cap - len(api_plan)
    refs = []
    for seg in range(2, beats):          # 中间段各 1 张锁构图
        if left <= 0:
            break
        refs.append(("参考图", "拍%02d-A" % seg, "锁第 %d 段构图" % seg))
        left -= 1
    for c in cast:
        if left <= 0:
            break
        refs.append(("参考图", c["name"], "锁%s长相/形制" % c["kind"]))
        left -= 1
    api_plan += refs

    n_shots = len(shots)
    n_cast = len(cast)
    n_total = n_shots + n_cast
    img_cost = n_total * a.img_price
    total = img_cost + vprice

    # ── 输出 ──────────────────────────────────────────────────────
    if a.json:
        print(json.dumps({
            "beats": beats, "duration": duration, "model": model, "video_price": vprice,
            "images": {"total": n_total, "shots": n_shots, "cast": n_cast,
                       "unit_price": a.img_price, "cost": round(img_cost, 4)},
            "api_plan": [{"slot": s, "from": f, "why": w} for s, f, w in api_plan],
            "api_cap": cap,
            "total_cost": round(total, 4),
            "list": shots + cast,
            "errors": [m for _, _, m in rep.errors()],
            "warns": [m for _, _, m in rep.warns()],
        }, ensure_ascii=False))
        return 0 if not rep.errors() else 1

    L = []
    L.append("# 拍摄计划")
    L.append("")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    L.append("| 拍数 | %d 拍 = **%g 秒**（平台拆成 %s） |"
             % (beats, duration, "+".join("%g" % x for x in lens)))
    L.append("| 视频档位 | `%s` → **¥%.2f** |" % (model, vprice))
    L.append("| 分镜图 | %d 拍 × %d 张候选 = **%d 张** |" % (beats, a.candidates, n_shots))
    L.append("| 定妆/道具图 | **%d 张** |" % n_cast)
    L.append("| 图合计 | **%d 张** × ¥%.4f = **¥%.2f** |" % (n_total, a.img_price, img_cost))
    L.append("| **总计** | **¥%.2f**（图占 %.1f%%） |" % (total, 100.0 * img_cost / total if total else 0))
    L.append("")
    L.append("> 一张图 ≈ 视频的 1/57 秒。所以：**图管够，视频管少**。"
             "图用来试错和锁死，视频只拍一次。")
    L.append("")
    L.append("## 进 API 的 %d 张（上限 %d）" % (len(api_plan), cap))
    L.append("")
    L.append("| 用途 | 取哪张 | 为什么 |")
    L.append("|---|---|---|")
    for s, f, w in api_plan:
        L.append("| %s | `%s` | %s |" % (s, f, w))
    if n_total > len(api_plan):
        L.append("")
        L.append("**剩下的 %d 张不进 API**，只给人挑图用。挑完把不要的删掉，别塞进接口白占额度。"
                 % (n_total - len(api_plan)))
    L.append("")
    L.append("## 要生成的图（%d 张）" % n_total)
    L.append("")
    for it in shots + cast:
        L.append("### %s  `%s`" % (it["name"], it["kind"]))
        L.append("")
        L.append("```")
        L.append(it["prompt"])
        L.append("```")
        L.append("")
    L.append("## 闸门")
    L.append("")
    rep.dump()
    txt = "\n".join(L)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(txt + "\n")
        print("已写 %s" % a.out)
        rep.dump()
        print()
    else:
        print(txt)
    print()
    print("结论：%s" % ("可以开拍（图先跑起来）" if not rep.errors() else "有必改项，先改脚本卡"))
    return 0 if not rep.errors() else 1


if __name__ == "__main__":
    sys.exit(main())
