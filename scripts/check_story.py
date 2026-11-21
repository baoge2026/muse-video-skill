# -*- coding: utf-8 -*-
"""文案闸门（check_story）：脚本卡能不能过，机器说了算。

为什么要有这个脚本：2026-11-21 折叠座椅电动车 50 秒片，preflight 全绿、账本干净、¥1.00 花掉，
用户看完的原话是「你写提示词的水平不如 2 岁孩童，你这是介绍产品还是讲故事呢」。
技术检查管的是「不出错」，管不了「好不好看」。这个脚本补的是**能机械判的那部分**：

    靶心    有【一句话承诺】、≤20 字、不含禁用词
    弧线    第 1 拍=钩子、最后一拍=落点、中间至少一拍兑现、建议一拍反转
    禁用词  说明书腔 / 空转动词 / 被用烂的词
    画面    每拍有画面；画面含「变」的动作；不含空转动词；机位不超过一句
    数字    全片 ≥2 处可验证的数字或对比
    额度    每拍旁白字数 ≤ 该档位 10 秒容量
    复述    画面里看见的名词，旁白又念一遍
    温度    某拍既没人也没手
    空度    全片字数 < 额度 55%

判不了的不装闸门：好不好笑、有没有味道、观众会不会哭——那些只能人看。
所以「脚本卡先给用户看」是硬流程，不是建议。

用法:
    python check_story.py 脚本卡.txt --segs 5
    python check_story.py 脚本卡.txt --segs 5 --emit 台词卡.txt   # 顺手生成 check_words 用的台词卡
    python check_story.py 脚本卡.txt --segs 5 --json              # 给 preflight 用

脚本卡格式见 assets/脚本卡模板.txt：
    一行一拍，| 分隔 5 列 —— 段号|作用|语速档|旁白|画面
    【一句话承诺】/【核心冲突】/【受众】/【语气】/【片长】 写在 # 注释块里也能读

退出码：0 = 可以往下走；1 = 有必改项；2 = 文件读不了。
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_words as cw  # noqa: E402

SEG_LEN = 10.0

# ── 弧线：顺序即叙事。drama = 通用叙事；kids = 低龄启蒙（正反示范，不要反转）
ARCS = {
    "drama": ["钩子", "反转", "兑现", "身份", "落点"],
    "kids":  ["错误", "后果", "正确", "记住"],
}
ROLES = ARCS["drama"]

# ── 禁用词：说明书腔 ─────────────────────────────────────────────
BANNED = ["这款", "该款", "采用", "打造", "引领", "尽显", "全方位", "一站式",
          "赋能", "助力", "焕新", "尽享", "甄选", "匠心", "极致", "完美", "贴心",
          "升级", "革命性", "黑科技", "遥遥领先"]
# ── 禁用词：空转动词（尤其不许出现在画面里）─────────────────────
EMPTY_VERBS = ["展现", "体现", "彰显", "呈现", "营造", "诠释", "演绎",
               "传达", "突出", "凸显", "带来"]
# ── 禁用词：被用烂的具体词 ───────────────────────────────────────
CLICHE = ["轻松", "三秒", "一键", "秒变", "神器", "必备"]

# ── 画面必须含「变」：物体状态改变的动作 ─────────────────────────
CHANGE_VERBS = ["压", "拉", "合拢", "塌", "弹开", "拎", "塞进", "推进", "掀开", "合上",
                "拧", "跨上", "踩", "抬头", "回头", "伸手", "攥", "松开", "亮起", "暗下",
                "掠过", "溅", "晃", "缩", "撑住", "停住", "推开", "扶", "举起", "放下",
                "弯腰", "侧身", "挤过", "穿过", "摇到", "转身", "立起", "收拢", "收起",
                "握", "攥", "按", "踢", "抬头", "低头", "回头",
                "走过", "跨过", "走上", "走下", "走来", "驶过", "驶出", "抬起", "掀起",
                "挤过", "钻过", "越过", "擦过", "闪过", "超过", "带起", "切进", "拧",
                "刹住", "拐", "蹬", "踩下", "甩", "贴住", "让开", "躲", "撞", "碰",
                "搭", "靠在", "露出", "塞进", "抽出", "翻开", "扣上", "拉上", "拉开",
                "推", "伸", "搬", "抱", "端", "捏", "掂", "甩开", "顶", "按下去",
                "举起", "放下", "蹲下", "站起", "转身", "抬头", "低头", "回头看"]

# ── 废镜头：这些词出现就说明这一拍在空转（用户原话「50 秒很多废镜头」）──
WASTE_VISUAL = ["静止", "空镜", "一路", "走去", "走向", "凝视", "望向", "伫立",
                "缓慢推近", "缓慢拉远", "原地", "停在那里", "没有动作", "画面不动"]

# ── 画面里有、旁白就别念的名词（复述检查）───────────────────────
VISIBLE_NOUNS = ["轮子", "车轮", "四轮", "座椅", "靠背", "车把", "后备箱", "箱子",
                 "提手", "踏板", "电池", "仪表", "车身", "瓷砖", "地面", "车流", "红灯"]

# ── 有人在场（温度检查）──────────────────────────────────────────
HUMAN = ["人", "他", "她", "你", "我", "手", "脚", "孩子", "同事", "前台", "保安",
         "骑手", "司机", "家长", "学生", "师傅", "工人", "官", "匠"]

NUM = re.compile(r"[0-9]+|[一二三四五六七八九十百千万两零几]+(?:个|天|年|月|次|块|公里|米|秒|分|小时|位|台|只|件|倍|格|周|层|人|张|条|%)")


class Report:
    def __init__(self):
        self.items = []

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


def load(path):
    """读脚本卡 -> (meta dict, rows list, problems list)"""
    meta, rows, problems = {}, [], []
    with open(path, encoding="utf-8") as f:
        for ln, raw in enumerate(f, 1):
            line = raw.strip()
            if not line:
                continue
            # 元信息：【键】值  也接受出现在 # 注释里
            m = re.match(r"^#?\s*【([^】]+)】\s*(.*)$", line)
            if m:
                meta[m.group(1).strip()] = m.group(2).strip()
                continue
            if line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|", 5)]
            if len(parts) < 5:
                problems.append("第 %d 行只有 %d 列，脚本卡要 5 列：段号|作用|档|台词|画面"
                                "（第 6 列可选：角色，默认旁白）—— %s" % (ln, len(parts), line))
                continue
            try:
                seg = int(parts[0])
            except ValueError:
                problems.append("第 %d 行段号不是数字：%s" % (ln, line))
                continue
            rows.append({"line": ln, "seg": seg, "role": parts[1], "tier": parts[2],
                         "text": parts[3], "visual": parts[4],
                         "speaker": (parts[5].strip() if len(parts) >= 6 and parts[5].strip()
                                     else "旁白")})
    return meta, rows, problems


def check_meta(rep, meta):
    p = meta.get("一句话承诺", "")
    if not p:
        rep.err("靶心", "没有【一句话承诺】—— 一句话说不出「因为有了它，你就不用____」，"
                        "这条片就是五个卖点平铺，等于没有靶心")
    else:
        if "____" in p or "______" in p:
            rep.err("靶心", "【一句话承诺】还是模板占位符，没填")
        else:
            n = cw.count(p)
            if n > 20:
                rep.err("靶心", "【一句话承诺】%d 字，超 20 字——单独发给人看，他复述不出来" % n)
            else:
                rep.ok("靶心", "一句话承诺：%s（%d 字）" % (p, n))
            hit = [w for w in BANNED + EMPTY_VERBS + CLICHE if w in p]
            if hit:
                rep.err("靶心", "一句话承诺里有禁用词：%s" % "、".join(hit))
            # 一串卖点不是一句承诺：「折叠方便、能放后备箱、带靠背、续航远」不是承诺
            frags = [x for x in re.split(r"[、，,；;/]", p) if x.strip()]
            if len(frags) >= 3 and not re.search(r"不用|不必|省|免|再也不用|从此|就是", p):
                rep.err("靶心", "一句话承诺是 %d 段卖点的并列（%s），不是一句承诺——"
                                "它应该是一个「因为有了它，你就不用____」的答案" % (len(frags), p))
    for k, must in [("核心冲突", False), ("受众", False)]:
        v = meta.get(k, "")
        if not v:
            (rep.err if must else rep.warn)("靶心", "没写【%s】—— 受众不具体，语气就飘" % k)


def beats(rows):
    """按段号聚合 -> [(seg, role, visual, [rows])]，支持一段多句（对白+旁白）。"""
    out = []
    for r in rows:
        if out and out[-1][0] == r["seg"]:
            out[-1][3].append(r)
        else:
            out.append([r["seg"], r["role"], r["visual"], [r]])
    return [tuple(b) for b in out]


def check_arc(rep, rows, segs, arc="drama"):
    if not rows:
        rep.err("弧线", "脚本卡里没有任何一拍")
        return
    roles_allowed = ARCS.get(arc) or ARCS["drama"]
    bs = beats(rows)
    segs_got = [b[0] for b in bs]
    if segs_got != list(range(1, len(bs) + 1)):
        rep.err("弧线", "段号必须从 1 连续排到 N（同一段可以写多句），现在是 %s" % segs_got)
    if segs and len(bs) != segs:
        rep.err("弧线", "脚本卡 %d 拍，但 --segs %d —— 拍数必须等于段数（一拍 = 10 秒）"
                % (len(bs), segs))

    roles = [b[1] for b in bs]
    bad = [r for r in roles if r not in roles_allowed]
    if bad:
        rep.err("弧线", "作用「%s」不认识。%s 轨只认：%s（同一段第二句起写 -）"
                % ("、".join(bad), arc, " / ".join(roles_allowed)))
        return

    if arc == "kids":
        # 低龄启蒙：不要反转，要「错误→后果→正确→记住」，而且必须正反都有
        for need in ("错误", "正确"):
            if need not in roles:
                rep.err("弧线", "kids 轨缺「%s」拍 —— 孩子靠正反对比学，只讲一边记不住" % need)
        if "记住" in roles and roles[-1] != "记住":
            rep.err("弧线", "「记住」必须放最后一拍")
        if "反转" in roles:
            rep.warn("弧线", "kids 轨不建议用反转——孩子分不清「故意演错的」和「应该做的」")
        if "后果" not in roles:
            rep.warn("弧线", "没有「后果」拍 —— 只说「不能这样」孩子不知道为什么")
        if not rep.errors():
            rep.ok("弧线（kids）", "弧线：%s" % " → ".join(roles))
        return

    if roles[0] != "钩子":
        rep.err("弧线", "第 1 拍是「%s」，必须是「钩子」—— 第 1 拍介绍产品全貌就是没人看完的原因"
                % roles[0])
    if roles[-1] != "落点":
        rep.err("弧线", "最后一拍是「%s」，必须是「落点」—— 结尾复述卖点等于没有结尾"
                % roles[-1])
    if "兑现" not in roles:
        rep.err("弧线", "没有「兑现」拍 —— 反转之后必须落到「这对我有什么用」")
    if "反转" not in roles:
        if len(roles) >= 4:
            rep.err("弧线", "没有「反转」拍 —— 四拍以上没有反转，就是卖点平铺，"
                            "这正是「像说明书、没起伏」最直接的原因。"
                            "确实没有反直觉的点，用代价/身份/尺度反转替代（见 文案与叙事.md 3.2）")
        else:
            rep.warn("弧线", "没有「反转」拍 —— 短片可以不要，但要有代价或身份顶着")
    if "身份" not in roles:
        rep.warn("弧线", "没有「身份」拍 —— 观众不知道「谁在用」，就不知道「跟我有没有关系」")
    dup = [x for x in set(roles) if roles.count(x) > 1]
    if dup:
        rep.warn("弧线", "作用重复：%s —— 同一作用连用两拍，中间那拍多半可以删" % "、".join(dup))
    if not rep.errors():
        rep.ok("弧线", "弧线：%s" % " → ".join(roles))


def check_words_all(rep, rows):
    total = 0
    for seg, _role, _v, seg_rows in beats(rows):
        n_seg = sum(cw.count(r["text"]) for r in seg_rows)
        total += n_seg
        rates = []
        for r in seg_rows:
            tier = r["tier"] or "稳"
            rate = cw.TIER_RATE.get(tier)
            if rate is None:
                rep.err("额度", "第%d拍语速档「%s」不认识（稳/快/紧/极限）" % (seg, tier))
            else:
                rates.append((tier, rate))
        if not rates:
            continue
        # 一段里多句时，按最保守的那一档算整段容量
        tier, rate = min(rates, key=lambda x: x[1])
        cap = SEG_LEN * rate
        if n_seg > SEG_LEN * cw.HARD_RATE + 0.5:
            rep.err("额度", "第%d拍 %d 字 = %.1f 字/秒，超实测红线 %.1f，必吞字"
                    % (seg, n_seg, n_seg / SEG_LEN, cw.HARD_RATE))
        elif n_seg > cap + 0.5:
            rep.err("额度", "第%d拍 %d 字，%s档 10 秒只能放 %d 字（超 %d 字）——删字，不要加秒"
                    % (seg, n_seg, tier, int(cap), n_seg - int(cap)))
        else:
            rep.ok("额度", "第%d拍 %d 字 · %s %.1f 字/秒 · 约 %.1f 秒（余 %.1f 秒）"
                   % (seg, n_seg, tier, rate, n_seg / rate, SEG_LEN - n_seg / rate))
    budget = len(beats(rows)) * SEG_LEN * 3.6
    if budget and total < budget * 0.55:
        rep.warn("空度", "全片 %d 字，只用了额度 %.0f%% —— 检查是不是每拍都讲清了；"
                         "空不等于高级" % (total, 100.0 * total / budget))
    return total


CLAUSE = re.compile(r"[；;。→\n]+")


def check_visual(rep, rows):
    for seg, _role, v, _rows in beats(rows):
        v = (v or "").strip()
        if len(v) < 8:
            rep.err("画面", "第%d拍画面太短或为空——没有画面就是没有片子" % seg)
            continue
        if "___" in v:
            rep.err("画面", "第%d拍画面还是模板占位符，没填" % seg)
            continue
        hit = [w for w in EMPTY_VERBS if w in v]
        if hit:
            rep.err("画面", "第%d拍画面里有空转动词「%s」——镜头拍不到「体现」，只拍得到动作"
                    % (seg, "、".join(hit)))
        # 废镜头词
        waste = [w for w in WASTE_VISUAL if w in v]
        if waste:
            rep.err("废镜头", "第%d拍画面里有「%s」——这三类镜头（静止展示／空镜推拉／位移过程）"
                              "就是「50 秒很多废镜头」的来源，全删" % (seg, "、".join(waste)))
        # 动作链：一段 10 秒要有 3 个连续微动作，不是 1 个动作 + 干等
        clauses = [c.strip() for c in CLAUSE.split(v) if c.strip()]
        acting = [c for c in clauses if any(w in c for w in CHANGE_VERBS)]
        if len(acting) < 3:
            rep.err("动作链", "第%d拍只有 %d 个带动作的分句，要 ≥3 —— "
                              "一个动作撑不满 10 秒，模型会用凝视/空镜/慢移把剩下的秒数填掉。"
                              "写成「动作1；动作2；动作3」三个连续微动作" % (seg, len(acting)))
        if v.count("镜头") > 1:
            rep.warn("画面", "第%d拍写了 %d 次「镜头」——机位只写一句（景别+机位+有没有运动）"
                     % (seg, v.count("镜头")))
        if not any(w in v for w in HUMAN):
            rep.warn("温度", "第%d拍画面里没有人也没有手——产品片里没人，就是 PPT" % seg)


def check_banned(rep, rows):
    for r in rows:
        seg = r["seg"]
        for label, table in [("说明书腔", BANNED), ("空转动词", EMPTY_VERBS), ("用烂的词", CLICHE)]:
            hit = [w for w in table if w in r["text"]]
            if hit:
                rep.err("禁用词", "第%d拍旁白有%s：%s" % (seg, label, "、".join(hit)))


def check_numbers(rep, rows, arc="drama"):
    if arc == "kids":
        rep.ok("数字", "kids 轨不查数字——低龄片里冒数字反而增加理解负担")
        return
    found = []
    for r in rows:
        for m in NUM.finditer(r["text"]):
            found.append((r["seg"], m.group(0)))
    if len(found) < 2:
        rep.err("数字", "全片只有 %d 处可验证的数字/对比，至少要 2 处——"
                        "没有数字的主张，观众凭什么信" % len(found))
    else:
        rep.ok("数字", "数字/对比 %d 处：%s"
               % (len(found), "、".join("第%d拍 %s" % x for x in found[:6])))


def check_repeat(rep, rows):
    for seg, _role, v, seg_rows in beats(rows):
        said = "".join(r["text"] for r in seg_rows)
        both = [w for w in VISIBLE_NOUNS if w in (v or "") and w in said]
        if both:
            rep.warn("复述", "第%d拍画面里看得见「%s」，旁白又念了一遍——念观众看得见的东西等于白扔一秒"
                     % (seg, "、".join(both)))


def emit_lines(rows, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 由 check_story.py 从脚本卡生成，别手改；改脚本卡再重新生成\n")
        f.write("# 段号|角色|台词|语速档\n")
        for r in rows:
            f.write("%d|%s|%s|%s\n" % (r["seg"], r.get("speaker") or "旁白",
                                        r["text"], r["tier"] or "稳"))


def main():
    ap = argparse.ArgumentParser(description="脚本卡文案闸门")
    ap.add_argument("script_card")
    ap.add_argument("--segs", type=int, default=0, help="片长拆几段（一拍 10 秒）")
    ap.add_argument("--arc", choices=["drama", "kids"], default="drama",
                    help="drama=通用叙事（钩子/反转/兑现/身份/落点）；kids=低龄启蒙（错误/后果/正确/记住）")
    ap.add_argument("--emit", help="生成 check_words 用的台词卡")
    ap.add_argument("--json", action="store_true", help="给 preflight 用")
    a = ap.parse_args()

    if not os.path.exists(a.script_card):
        print("脚本卡不存在：%s" % a.script_card, file=sys.stderr)
        return 2
    meta, rows, problems = load(a.script_card)

    rep = Report()
    for p in problems:
        rep.err("格式", p)
    check_meta(rep, meta)
    check_arc(rep, rows, a.segs, a.arc)
    check_banned(rep, rows)
    check_visual(rep, rows)
    total = check_words_all(rep, rows)
    check_numbers(rep, rows, a.arc)
    check_repeat(rep, rows)

    if a.emit and rows and not problems:
        emit_lines(rows, a.emit)
        rep.ok("台词卡", "已生成 %s（%d 行）" % (a.emit, len(rows)))

    if a.json:
        print(json.dumps({"errors": [m for _, _, m in rep.errors()],
                          "warns": [m for _, _, m in rep.warns()],
                          "total_chars": total,
                          "rows": [{"seg": r["seg"], "role": r["role"], "tier": r["tier"],
                                    "speaker": r.get("speaker"), "chars": cw.count(r["text"]),
                                    "text": r["text"]}
                                   for r in rows]}, ensure_ascii=False))
        return 0 if not rep.errors() else 1

    rep.dump()
    print()
    if rep.errors():
        print("结论：%d 项必改、%d 项提醒 —— 改完重跑，exit 0 才许写 prompt"
              % (len(rep.errors()), len(rep.warns())))
        return 1
    print("结论：全绿（%d 项提醒）。下一步：脚本卡给用户看，他点头再写 prompt"
          % len(rep.warns()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
