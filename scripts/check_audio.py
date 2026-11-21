# -*- coding: utf-8 -*-
"""成片声轨验收：按段测「人声频段」实际能量，判断台词卡上写的话到底有没有被念出来。

由来：2026-11-21 折叠电动车 50s 单，台词卡 5 段全有台词，prompt 也写了，
但成片 0–30 秒是纯环境音（人声频段 −55 dB），30 秒后人物上镜才开始念。
当时只跑了 preflight（查字数/额度/素材）、只看了画面抽帧，没人量声音 -> 交了无声片。
这个脚本就是补那个缺环：**台词卡说这段有人说话，实测这段没人声 = 不合格，退出码 1。**

用法:
    python check_audio.py 成片.mp4 --lines 台词卡.txt
    python check_audio.py 成片.mp4 --lines 台词卡.txt --segs 5 --json
    python check_audio.py 成片.mp4 --no-lines          # 只报每段电平，不判期望

判定:
    人声频段 300–3000 Hz 的平均电平。
    实测参考：正常念白 −23 ~ −33 dB；纯环境音 −50 ~ −60 dB。
    默认阈值 −45 dB：低于它就算「这段没人说话」。
"""
import argparse
import json
import os
import re
import subprocess
import sys

BAND = "highpass=f=300,lowpass=f=3000"
DEFAULT_THRESH = -45.0


def die(msg, code=1):
    print("[不合格] " + msg)
    sys.exit(code)


def probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        die("读不到时长，文件可能坏了：%s" % path)


def has_audio(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
        capture_output=True, text=True).stdout.strip()
    return bool(out), out


def band_mean(path, start, dur):
    """返回该窗口 300–3000Hz 的平均电平（dB）。无音频返回 None。"""
    cmd = ["ffmpeg", "-hide_banner", "-nostats",
           "-ss", "%.3f" % start, "-t", "%.3f" % max(dur, 0.2), "-i", path,
           "-af", BAND + ",volumedetect", "-f", "null", "-"]
    err = subprocess.run(cmd, capture_output=True, text=True).stderr
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", err)
    if not m:
        return None
    return float(m.group(1))


def peak(path):
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path,
                          "-af", "volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    m = re.search(r"max_volume:\s*(-?[\d.]+) dB", err)
    return float(m.group(1)) if m else None


def load_expected(lines_path):
    """{段号: 字数}，只统计有台词的段。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import check_words as cw
    rows, problems = cw.load(lines_path, 10.0)
    if problems:
        for p in problems:
            print("[提醒] 台词卡：" + p)
    exp = {}
    for r in rows:
        exp[r["seg"]] = exp.get(r["seg"], 0) + int(r["chars"])
    return exp


def main():
    ap = argparse.ArgumentParser(description="成片声轨验收")
    ap.add_argument("video")
    ap.add_argument("--lines", help="台词卡（用来判断哪段应该有台词）")
    ap.add_argument("--no-lines", action="store_true", help="不做期望判定，只报电平")
    ap.add_argument("--segs", type=int, help="段数；不给就按平台真实拆段（25→9+8+8）")
    ap.add_argument("--duration", type=float, default=0,
                    help="片长；给了就按 split_segments 真实拆段核算，比按时长/段数平均更准")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESH,
                    help="人声频段判定阈值 dB，默认 %.0f" % DEFAULT_THRESH)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.video):
        die("文件不存在：%s" % a.video)
    dur = probe_duration(a.video)
    ok_audio, codec = has_audio(a.video)
    if not ok_audio:
        die("成片根本没有音轨（%s）——100%% 不合格" % a.video)

    import check_words as _cw
    if a.duration:
        lens = _cw.split_segments(a.duration)
    elif a.segs:
        lens = [dur / a.segs] * a.segs
    else:
        lens = _cw.split_segments(dur)
    segs = len(lens)
    seg_len = lens[0]
    exp = {}
    if not a.no_lines and a.lines:
        if not os.path.exists(a.lines):
            die("台词卡不存在：%s" % a.lines)
        exp = load_expected(a.lines)

    rows, fails, silent_but_expected = [], [], []
    offs = [0.0]
    for x in lens:
        offs.append(offs[-1] + x)
    for k in range(1, segs + 1):
        s = offs[k - 1]
        d = min(lens[k - 1], dur - s)
        db = band_mean(a.video, s, d)
        want = exp.get(k, 0) if exp else None
        voiced = (db is not None) and (db > a.threshold)
        rows.append({"seg": k, "start": round(s, 2), "end": round(s + d, 2),
                     "voice_db": db, "voiced": voiced, "expect_chars": want})
        if want and want > 0 and not voiced:
            fails.append(rows[-1])
            silent_but_expected.append(k)

    # 偏弱判定：同一条片子里，某段比最强的人声段低 8 dB 以上 = 听感上就是「没人说话」
    # （2026-11-21 实测：正常念白 −23~−33，半死不活的人声 −40~−43，差值 >8 就能听出来）
    voiced_db = [r["voice_db"] for r in rows if r["voiced"]]
    weak = []
    if len(voiced_db) >= 2:
        best = max(voiced_db)
        for r in rows:
            if r["expect_chars"] and r["voiced"] and r["voice_db"] < best - 8.0:
                weak.append(r)

    top = peak(a.video)
    print("=== 声轨验收 ===")
    print("片长 %.3f 秒 / %d 段 / 每段约 %.1f 秒 / 阈值 %.0f dB / 音轨 %s"
          % (dur, segs, seg_len, a.threshold, codec))
    for r in rows:
        tag = "有台词" if r["expect_chars"] else ("本段无台词" if exp else "—")
        state = "有声" if r["voiced"] else "无声"
        if r in weak:
            state = "偏弱"
        print("  第%d段 %5.1f–%5.1f s  人声段 %7s dB  %s  (%s)"
              % (r["seg"], r["start"], r["end"],
                 "%.1f" % r["voice_db"] if r["voice_db"] is not None else "n/a",
                 state, tag))
    print("峰值 %.1f dB%s" % (top, "（超过 -1.0，有削波风险）" if top and top > -1.0 else ""))

    total_voiced = sum(1 for r in rows if r["voiced"])
    print("有声段 %d / %d" % (total_voiced, segs))

    if fails:
        print("")
        print("[不合格] 台词卡上这些段有台词，但成片里没有人声（< %.0f dB）：第 %s 段"
              % (a.threshold, "、".join(str(x) for x in silent_but_expected)))
        print("         原因优先查 prompt：写了台词又写「无人开口/无口型」，或画面里没有发声主体，")
        print("         模型就只出环境音。详情见 SKILL.md 第九节「声轨」")
    if weak:
        print("")
        print("[不合格] 这些段虽有人声，但比全片最响的人声段低 8 dB 以上，听感等于没念清：第 %s 段"
              % "、".join(str(r["seg"]) for r in weak))
        print("         逐段人声强弱不齐 = 模型只在「画面里有人」的段落才肯念，属同类事故。")
    if not fails and not weak and exp:
        print("[通过] 所有写了台词的段都测到正常人声")
    elif not exp:
        print("[提醒] 没给台词卡，只报电平不作判定")

    if a.json:
        print(json.dumps({"duration": dur, "segs": segs, "threshold": a.threshold,
                          "peak_db": top, "rows": rows, "fails": silent_but_expected,
                          "weak": [r["seg"] for r in weak]},
                         ensure_ascii=False, indent=1))
    sys.exit(1 if (fails or weak) else 0)


if __name__ == "__main__":
    main()
