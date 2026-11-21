# -*- coding: utf-8 -*-
"""台词时间线核算 v3：按「从第几秒开口、到下一个必须开口的时刻还剩多少秒、按这个语速还能说几个字」核算，
不按「每段几条 / 每段几句」核算。

用法:
    python check_words.py 台词卡.txt --seg 10 --segs 5
    python check_words.py 台词卡.txt --json          # 给 preflight 用

台词卡格式（一行一句，段号从 1 开始，| 分隔）:
    1|旁白|洛阳江入海口，水阔五里，潮水一日两至。
    2|蔡襄|潮水一日两至，这石基如何立得稳？|1.2|紧     ← 起秒 + 语速档
    2|老匠|大人，江底烂泥，石头沉底。|快              ← 第 4 列直接写档名
    3|旁白|……                                        ← 不写档 = 稳档，且 36 字封顶

语速档（2026-11-21 实测，别自己拍）:
    慢  3.2 字/秒   10 秒 32 字   实测「什么都不提」的对照值；给低龄儿童片用，不催语速
    稳  3.6 字/秒   10 秒 36 字   实测 37 字已到 9.83 秒；留 1 秒余量，推荐
    快  4.0 字/秒   10 秒 40 字   实测句首恒 4.0 字/秒，不吞句号的上限
    紧  4.4 字/秒   10 秒 44 字   开始吞句号连读，发音有糊的风险
    极限 4.6 字/秒  10 秒 46 字   到顶，再多就是 9.5–9.9 秒硬切

铁律：**不写速语档的段落，字数按稳档 36 字封顶，超了直接判错。**
不许「这段没设语速、但塞 45 字赌模型念得快」。

判定（全片一条时间线，不是 N 个盒子）:
    ① 每句的容量 = (下一个必须开口的时刻 − 本句开口) × 本句语速。
       下一个必须开口的时刻 = 下一句的开口；没有下一句 = 片长 − 片尾留白。
       超容量 = 必改（这就是撞车，也说明「说太多了」）。
    ② 片尾最后一句必须念完并留 ≥0.8 秒。
    ③ 单句语速 > 4.6 字/秒 = 必改（吞字、糊音）。
    ④ 该段没有一句台词却占了时间：只提醒，不判错。
    ⑤ 段内首句开口晚于 0.5 秒：提醒「头部空了 N 秒，白扔 X 字额度」。
"""
import argparse
import json
import re
import sys

PUNCT = re.compile(r"[，。、“”‘’…！？：；—－,.\s]")
PAREN = re.compile(r"[（(][^）)]*[）)]")

# (档名, 字/秒)  —— 顺序即从保守到激进
TIERS = [("慢", 3.2), ("稳", 3.6), ("快", 4.0), ("紧", 4.4), ("极限", 4.6)]
TIER_RATE = dict(TIERS)
DEFAULT_TIER = "稳"
HARD_RATE = 4.6          # 实测硬线：超过就吞句号、发音糊


def split_segments(duration):
    """平台真实拆段：平均拆成每段不超过 10 秒。

    官方原文的例子：「25 秒拆成 9 + 8 + 8」。所以 25 秒不是 2×10+5，也不是 3×8.33，
    而是 [9, 8, 8]。这就是「不要一味追求 10 的倍数」的依据——
    内容够 23 秒就交 23 秒（三档同价 ¥0.60），不用硬凑 30 秒再注水 7 秒。

    规则：n = ceil(duration/10)；base = duration // n；余数 r 摊给前 r 段各 +1。
    """
    if duration <= 0:
        return []
    n = (int(duration) + 9) // 10
    base, r = divmod(int(duration), n)
    lens = [base + 1] * r + [base] * (n - r)
    # 小数时长（比如 23.5）：把零头给最后一段
    frac = float(duration) - int(duration)
    if frac > 1e-6:
        lens[-1] = round(lens[-1] + frac, 3)
    return lens


def _off(seg, lens):
    """第 seg 段（1 起）的起始秒。lens 是列表就累加，是数字就按统一段长算。"""
    if isinstance(lens, (list, tuple)):
        return float(sum(lens[:seg - 1]))
    return (seg - 1) * float(lens)


def _len(seg, lens):
    if isinstance(lens, (list, tuple)):
        return float(lens[seg - 1]) if 0 < seg <= len(lens) else 0.0
    return float(lens)


def bare(s):
    return PAREN.sub("", s)


def count(s):
    return len(PUNCT.sub("", bare(s)))


def load(path, lens, strict_tier=True):
    """读台词卡 -> (rows, problems)。lens 可以是统一段长(float)或每段秒数列表。"""
    rows, problems, seg_last = [], [], {}
    with open(path, encoding="utf-8") as f:
        for ln, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 3:
                problems.append(f"第 {ln} 行格式不对（少于 3 列）：{line}")
                continue
            try:
                seg = int(parts[0])
            except ValueError:
                problems.append(f"第 {ln} 行段号不是数字：{line}")
                continue
            role, text = parts[1], parts[2]
            start_raw = tier_raw = ""
            if len(parts) >= 5:
                start_raw, tier_raw = parts[3], parts[4]
            elif len(parts) == 4:
                if parts[3] in TIER_RATE:
                    tier_raw = parts[3]
                else:
                    start_raw = parts[3]

            declared = bool(tier_raw)
            tier = tier_raw or DEFAULT_TIER
            if tier not in TIER_RATE:
                problems.append(f"第 {ln} 行语速档「{tier}」不认识（只认 "
                                + "/".join(t for t, _ in TIERS) + "）")
                tier, declared = DEFAULT_TIER, False

            n = count(text)
            if n == 0:
                problems.append(f"第 {ln} 行台词是空的")
                continue
            rate = TIER_RATE[tier]
            base = _off(seg, lens)
            explicit = False
            if start_raw:
                try:
                    start = base + float(start_raw)
                    explicit = True
                except ValueError:
                    problems.append(f"第 {ln} 行起秒不是数字，按接排处理")
                    start = seg_last.get(seg, base)
            else:
                start = seg_last.get(seg, base)
            end = start + n / rate
            rows.append({
                "line": ln, "seg": seg, "role": role, "text": text, "chars": n,
                "tier": tier, "tier_declared": declared, "rate": rate,
                "start": start, "end": end, "dur": n / rate,
                "explicit_start": explicit,
            })
            seg_last[seg] = end
    return rows, problems


def analyze(rows, lens, segs=None, tail=0.8, baseline=3.6):
    """核算并返回结果 dict（供 CLI 打印和 preflight 复用）。

    lens 可以是统一段长(float，此时必须给 segs)或每段秒数列表。
    段长不一定是 10——平台会把 25 秒拆成 9+8+8，所以容量要按每段真实秒数算。
    """
    if not isinstance(lens, (list, tuple)):
        segs = segs or 1
        lens = [float(lens)] * segs
    lens = [float(x) for x in lens]
    segs = len(lens)
    offs = [0.0]
    for x in lens:
        offs.append(offs[-1] + x)
    total = offs[-1]
    t0 = baseline
    srt = sorted(rows, key=lambda r: (r["start"], r["line"]))
    for r in rows:
        r["need_next_by"] = total - tail      # 默认硬截止 = 片尾
    for a, b in zip(srt, srt[1:]):
        a["need_next_by"] = min(a["need_next_by"], b["start"])
    for r in rows:
        r["window"] = max(0.0, r["need_next_by"] - r["start"])
        r["capacity"] = r["window"] * r["rate"]
        r["over"] = r["chars"] - r["capacity"]

    errs, warns = [], []
    over_lines, over_segs = set(), set()
    cap_errs, rate_errs, tier_errs = [], [], []
    for r in srt:
        slen = _len(r["seg"], lens)
        if r["over"] > 0.5:
            over_lines.add(r["line"])
            over_segs.add(r["seg"])
            nxt = ("「%s」要在 %.1fs 开口" % (r["_next_role"], r["need_next_by"])
                   if r.get("_next_role") else "片尾 %.1fs" % (total - tail))
            cap_errs.append(
                "第 %d 段「%s」从 %.1fs 开口，%s，按「%s」%.1f 字/秒 只剩 %.1fs 能说 %.0f 字；"
                "你写了 %d 字（多 %d 字）——少说一句，剩下的挪到下一段开头"
                % (r["seg"], r["role"], r["start"], nxt, r["tier"], r["rate"],
                   r["window"], r["capacity"], r["chars"], round(r["over"])))
        if r["rate"] > HARD_RATE + 1e-6:
            rate_errs.append("第 %d 段「%s」声明语速 %.1f 字/秒 > 实测红线 %.1f——必吞字、发音糊"
                             % (r["seg"], r["role"], r["rate"], HARD_RATE))
        if not r["tier_declared"] and r["chars"] > slen * t0 + 0.5 and r["line"] not in over_lines:
            tier_errs.append(
                "第 %d 段「%s」没写语速档，按稳档 %.1f 字/秒、本段 %.0f 秒只够 %.0f 字，你却写了 %d 字——"
                "要么删字，要么显式声明档位（快/紧），不许默认它自己念快"
                % (r["seg"], r["role"], t0, slen, slen * t0, r["chars"]))
        head = r["start"] - offs[r["seg"] - 1]
        if head > 0.5 and not r.get("_has_prev"):
            warns.append("第 %d 段「%s」%.1fs 才开口，头部空了 %.1fs = 白扔约 %.0f 字额度"
                         % (r["seg"], r["role"], r["start"], head, head * r["rate"]))
        tail_over = r["end"] - offs[r["seg"]]
        if tail_over > 4.0:
            warns.append("第 %d 段「%s」跨入下一段 %.1fs，下一段开局被占，留意别撞车"
                         % (r["seg"], r["role"], tail_over))

    errs.extend(cap_errs)
    errs.extend(rate_errs)
    errs.extend(tier_errs)

    # 片尾（最伤）：那句已经报过超容量就不再重复
    if srt:
        last = max(srt, key=lambda r: r["end"])
        if last["end"] > total - tail and last["line"] not in over_lines:
            errs.append("全片最后一句念到 %.1fs，片长 %.0fs，只剩 %.1fs（要求 ≥%.1fs）——"
                        "结尾没有下一段接着念，必定硬切" % (last["end"], total, total - last["end"], tail))

    segs_out = []
    for k in range(1, segs + 1):
        slen = lens[k - 1]
        ls = [r for r in rows if r["seg"] == k]
        chars = sum(r["chars"] for r in ls)
        density = chars / slen if slen else 0
        first_start = min((r["start"] for r in ls), default=None)
        finish = max((r["end"] for r in ls), default=None)
        flags = []
        if density > HARD_RATE:
            flags.append("密度 %.1f 超红线" % density)
            if k not in over_segs:
                errs.append("第 %d 段 %d 字 / %.0f 秒 = %.1f 字/秒 > %.1f——必吞句连读，少说几句"
                            % (k, chars, slen, density, HARD_RATE))
        elif density > 3.6:
            flags.append("密度 %.1f 偏紧" % density)
        if not ls:
            flags.append("本段无台词")
            if k < segs:
                warns.append("第 %d 段整段没排台词——可以，但要确认这是有意的留白段，"
                             "不是忘了写（留白段 prompt 必须写「此段无人声」）" % k)
        tiers = sorted({r["tier"] for r in ls}, key=lambda t: TIER_RATE[t])
        segs_out.append({
            "seg": k, "len": slen, "chars": chars, "density": round(density, 2),
            "first_start": None if first_start is None else round(first_start - offs[k - 1], 2),
            "finish": None if finish is None else round(finish, 2),
            "slack": None if finish is None else round(offs[k] - finish, 2),
            "roles": sorted({r["role"] for r in ls}), "tiers": tiers,
            "flags": flags,
        })

    rows_out = [{
        "line": r["line"], "seg": r["seg"], "role": r["role"], "chars": r["chars"],
        "tier": r["tier"], "rate": r["rate"], "start": round(r["start"], 2),
        "end": round(r["end"], 2), "need_next_by": round(r["need_next_by"], 2),
        "window": round(r["window"], 2), "capacity": round(r["capacity"], 1),
        "over": round(r["over"], 1), "_chained": r.get("_chained", False),
    } for r in srt]

    return {
        "seg_len": (lens[0] if len(set(lens)) == 1 else None),
        "lens": lens, "segs": segs, "total": total, "tail": tail,
        "baseline": t0, "rows": rows_out, "segments": segs_out,
        "errors": errs, "warns": warns,
    }


def attach_context(rows):
    """给每句挂上「下一个必须开口的是谁 / 自己是不是段内第一句 / 下一句是不是接着自己念」。"""
    srt = sorted(rows, key=lambda r: (r["start"], r["line"]))
    seen_seg = set()
    for i, r in enumerate(srt):
        nxt = srt[i + 1] if i + 1 < len(srt) else None
        r["_next_role"] = nxt["role"] if nxt else None
        r["_has_prev"] = r["seg"] in seen_seg
        r["_chained"] = bool(nxt and not nxt["explicit_start"]
                             and abs(nxt["start"] - r["end"]) < 0.05)
        seen_seg.add(r["seg"])


def print_report(res, rows):
    lens, segs, total = res["lens"], res["segs"], res["total"]
    print("拆段 %s = %gs（平台按每段 ≤10 秒平均拆，不必是 10 的倍数）   红线 %.1f 字/秒   片尾留白 ≥%.1fs"
          % ("+".join("%g" % x for x in lens), total, HARD_RATE, res["tail"]))
    print("每段预算（从段首 0 秒开口、一直说到段尾）：")
    for i, x in enumerate(lens, 1):
        print("   第%d段 %gs：%s" % (i, x, "   ".join("%s→%d字" % (t, int(x * r)) for t, r in TIERS)))
    print()

    print("逐句核算（容量 = 从开口到下一个必须开口的时刻 × 本句语速）")
    print(" 段 | 音色 | 字数 | 档位   | 开口 → 念完 | 硬截止 | 窗口 | 容量 | 用量 | 判定")
    for r in res["rows"]:
        use = "%.0f%%" % (r["chars"] / r["capacity"] * 100) if r["capacity"] > 0 else "—"
        if r["over"] > 0.5:
            bad = "超 %d 字" % round(r["over"])
        elif -r["over"] <= 0.6 and r.get("_chained"):
            bad = "接排满额"
        elif r["capacity"] > 0:
            bad = "余 %.0f 字" % -r["over"]
        else:
            bad = ""
        print("%3d | %-4s | %4d | %-6s | %5.1f→%5.1f | %6.1fs | %4.1fs | %4.0f字 | %5s | %s"
              % (r["seg"], r["role"], r["chars"], r["tier"], r["start"], r["end"],
                 r["need_next_by"], r["window"], r["capacity"], use, bad))
    print()

    print("逐段汇总")
    print(" 段 | 首句开口 | 段内字数 | 密度 | 收尾 | 音色 | 判定")
    for s in res["segments"]:
        fs = "—" if s["first_start"] is None else "%.1fs" % s["first_start"]
        fin = "—" if s["slack"] is None else "%+.1fs" % s["slack"]
        print("%3d | %8s | %8d | %4.1f | %6s | %-9s | %s"
              % (s["seg"], fs, s["chars"], s["density"], fin,
                 "/".join(s["roles"]) or "—", "；".join(s["flags"]) or "OK"))
    print()

    for w in res["warns"]:
        print("提醒：" + w)
    for e in res["errors"]:
        print("必须改：" + e)
    if not res["errors"]:
        print("通过：每句都在可用窗口内、没有片尾截断、没有超红线密度。"
              + ("（有提醒项，看情况处理）" if res["warns"] else ""))


def main():
    ap = argparse.ArgumentParser(description="台词时间线核算（按开口时刻算可用字数）")
    ap.add_argument("file")
    ap.add_argument("--duration", type=float, default=0, help="片长总秒数；给了就按平台真实拆段算（25→9+8+8）")
    ap.add_argument("--seg", type=float, default=10.0, help="每段秒数，默认 10（给了 --duration 就忽略）")
    ap.add_argument("--segs", type=int, default=0, help="总段数，不给则按最大段号")
    ap.add_argument("--tail", type=float, default=0.8, help="片尾最少留白，默认 0.8 秒")
    ap.add_argument("--json", action="store_true", help="输出 JSON（preflight 用）")
    args = ap.parse_args()

    if args.duration:
        lens = split_segments(args.duration)
    else:
        segs0 = args.segs or 1
        lens = [args.seg] * segs0
    rows, problems = load(args.file, lens)
    if not rows and not problems:
        print("没读到台词")
        return 1
    segs = args.segs or len(lens) or max((r["seg"] for r in rows), default=0)
    if args.duration:
        segs = len(lens)
    for r in rows:
        if r["seg"] > segs:
            problems.append("第 %d 行段号 %d 超过总段数 %d" % (r["line"], r["seg"], segs))
    attach_context(rows)
    res = analyze(rows, lens, segs, args.tail)
    res["problems"] = problems
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print_report(res, rows)
        for p in problems:
            print("格式问题：" + p)
    ok = not res["errors"] and not problems
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
