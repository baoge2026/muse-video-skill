# -*- coding: utf-8 -*-
"""中级检查（review_sheet）：提交视频任务之前，先出一份「给人看的审片单」。

为什么要有这一步（2026-11-21 用户原话）：

    「低级错误不要犯。上传生成视频任务的时候要做中级检查，发现问题要问我要不要修改，
      不要自己修改个没完，最后不经我同意自己就提交了。」

原来只有两道闸：
    preflight.py   技术闸（计费/时间轴/额度/素材/账本）—— 全绿也照样能出废片
    .muse-approve  价格闸 —— 用户只知道「要花 ¥0.60」，不知道「要拍什么」

缺的正是中间这一道：**把要提交的东西翻译成人话，摆到用户面前，等他决定。**

这个脚本做的事：
    1. 跑 check_story 全套，收集必改/提醒
    2. 做三条「只有人能判」的核对，尽量机械化：
       · 产品形态矛盾（写了「箱子/行李箱/拎起」，但【道具】里没这个形态）
       · 台词全文有没有废话/念画面可见物
       · 素材清单对不对（首帧/尾帧/参考图分别是什么）
    3. 输出一份 markdown 审片单 + `.muse-review.json`（带指纹）
    4. **只报告，不改任何文件。** 改不改由用户决定。

用法:
    python review_sheet.py --script 脚本卡.txt --prompt p.txt --duration 25 \\
        --first 图/首帧.png --last 图/尾帧.png --ref 图/a.png --ref 图/b.png

退出码：0 = 可以拿去问用户；1 = 有硬伤（照样出单子，但明确标「别提交」）；2 = 读不了。
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_words as cw    # noqa: E402
import check_story as cs    # noqa: E402

# 产品形态词：说了这些，就必须在【道具】里出现过，否则就是自创形态
FORM_WORDS = ["箱子", "行李箱", "手提箱", "公文包", "拎起", "拎着", "提手", "背包", "拉杆箱"]


def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sheet_sha(script, prompt, images, duration):
    h = hashlib.sha256()
    for p in [script, prompt]:
        h.update(sha_file(p).encode() if p and os.path.exists(p) else b"-")
    for p in images:
        h.update(sha_file(p).encode() if p and os.path.exists(p) else b"-")
    h.update(("|%s" % duration).encode())
    return h.hexdigest()


def check_form(rep, rows, meta):
    """产品形态矛盾：画面/台词里出现的形态词，【道具】里必须写过。"""
    prop = (meta.get("道具") or "") + (meta.get("产品") or "")
    for seg, _role, v, seg_rows in cs.beats(rows):
        said = " ".join([v or ""] + [r["text"] for r in seg_rows])
        for w in FORM_WORDS:
            if w in said and w not in prop:
                rep.err("产品形态", "第%d拍出现「%s」，但【道具】里没写这个形态——"
                                    "这是自创的产品行为，模型会照着画错（2026-11-21 折叠成行李箱事故）"
                        % (seg, w))
    if not prop:
        rep.err("产品形态", "脚本卡没有【道具】——产品没有任何文字锚定，形态必跑偏")


def main():
    ap = argparse.ArgumentParser(description="提交前中级检查：出一份给人看的审片单")
    ap.add_argument("--script", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--first")
    ap.add_argument("--last")
    ap.add_argument("--ref", action="append", default=[])
    ap.add_argument("--arc", choices=["drama", "kids"], default="drama")
    ap.add_argument("--out", help="审片单写到哪（默认 .muse-review.md，同 prompt 目录）")
    a = ap.parse_args()

    for p in [a.script, a.prompt]:
        if not os.path.exists(p):
            print("文件不存在：%s" % p, file=sys.stderr)
            return 2

    meta, rows, problems = cs.load(a.script)
    lens = cw.split_segments(a.duration)
    rep = cs.Report()
    for p in problems:
        rep.err("格式", p)
    cs.check_meta(rep, meta)
    cs.check_arc(rep, rows, len(lens), a.arc)
    cs.check_banned(rep, rows)
    cs.check_visual(rep, rows)
    cs.check_words_all(rep, rows, lens)
    cs.check_numbers(rep, rows, a.arc)
    cs.check_product_focus(rep, rows, bool(meta.get("道具")))
    cs.check_shot_size(rep, rows, bool(meta.get("道具")))
    cs.check_repeat(rep, rows)
    check_form(rep, rows, meta)

    images = ([a.first] if a.first else []) + ([a.last] if a.last else []) + list(a.ref)
    caps = {10: 10}
    cap = 10 if a.duration <= 10 else 9

    L = []
    L.append("# 提交前中级检查（请先看这一页再决定）")
    L.append("")
    L.append("生成时间：%s　片长：%g 秒　平台拆段：%s"
             % (time.strftime("%Y-%m-%d %H:%M:%S"), a.duration,
                "+".join("%g" % x for x in lens)))
    L.append("")
    L.append("## 1　这条片到底讲什么")
    L.append("")
    L.append("| 项 | 内容 |")
    L.append("|---|---|")
    for k in ("一句话承诺", "核心冲突", "受众", "语气"):
        L.append("| %s | %s |" % (k, meta.get(k) or "**（没写）**"))
    L.append("")
    L.append("## 2　台词全文（一共 %d 字）" % sum(cw.count(r["text"]) for r in rows))
    L.append("")
    for seg, role, _v, seg_rows in cs.beats(rows):
        for r in seg_rows:
            L.append("- **第%d拍**（%gs / %s）%s" % (seg, lens[seg - 1] if seg <= len(lens) else 0,
                                                  r["tier"], r["text"]))
    L.append("")
    L.append("## 3　每一拍拍什么")
    L.append("")
    for seg, role, v, _r in cs.beats(rows):
        L.append("- **第%d拍〔%s〕** %s" % (seg, role, v or "**（没写画面）**"))
    L.append("")
    L.append("## 4　进 API 的素材（%d / 上限 %d 张）" % (len(images), cap))
    L.append("")
    if a.first:
        L.append("- **首帧**　`%s`　→ 第一段的开场构图" % os.path.basename(a.first))
    if a.last:
        L.append("- **尾帧**　`%s`　→ 最后一段的收尾画面" % os.path.basename(a.last))
    for i, p in enumerate(a.ref, 1):
        L.append("- **参考图%d**　`%s`" % (i, os.path.basename(p)))
    if not images:
        L.append("- **一张都没有**")
    L.append("")
    L.append("## 5　自检发现")
    L.append("")
    errs, warns = rep.errors(), rep.warns()
    if not errs and not warns:
        L.append("没有发现问题。")
    for _lv, g, m in errs:
        L.append("- 🔴 **必改｜%s** %s" % (g, m))
    for _lv, g, m in warns:
        L.append("- 🟡 提醒｜%s %s" % (g, m))
    L.append("")
    L.append("## 6　请你决定")
    L.append("")
    if errs:
        L.append("**有 %d 项必改，建议先别提交。** 请告诉我改哪些，我不自己动。" % len(errs))
    elif warns:
        L.append("没有必改项，%d 项提醒。**要不要改？** 不改我就按这版提交。" % len(warns))
    else:
        L.append("没有发现问题。**按这版提交吗？**")
    L.append("")
    L.append("> 我不会自动修改，也不会自己提交。等你说「提交」才走下一步。")

    md = "\n".join(L)
    out_md = a.out or os.path.join(os.path.dirname(os.path.abspath(a.prompt)), ".muse-review.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(md + "\n")

    tok = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sheet_sha": sheet_sha(a.script, a.prompt, images, a.duration),
        "duration": a.duration,
        "script": os.path.abspath(a.script),
        "prompt": os.path.abspath(a.prompt),
        "images": [os.path.abspath(p) for p in images if os.path.exists(p)],
        "blockers": [m for _l, _g, m in errs],
        "warns": [m for _l, _g, m in warns],
        "sheet_md": os.path.abspath(out_md),
    }
    out_json = os.path.join(os.path.dirname(os.path.abspath(a.prompt)), ".muse-review.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(tok, f, ensure_ascii=False, indent=1)

    print(md)
    print()
    print("已写：%s" % out_md)
    print("已写：%s（指纹 %s）" % (out_json, tok["sheet_sha"][:12]))
    print("下一步：把这一页给用户看，等他说「提交」。改了任何东西都要重跑本脚本。")
    return 0 if not errs else 1


if __name__ == "__main__":
    sys.exit(main())
