#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从远端拉取技能最新版本——每次使用技能前都跑一次，本机也一样。

    python scripts/sync.py            # 详细输出
    python scripts/sync.py --quiet    # 一行结果（技能流程内部调用）

退出码：
    0  已是最新 / 已更新成功 / 非 git 安装 / 网络不通（用本机版本继续）
    2  拉取时冲突，需要人工处理（多半是 ledger.json，用 scripts/ledger_union.py 合）

设计原则：**同步失败不阻塞出片**。网络断了宁可让你用旧规则干活，
也不要因为拉不到代码就卡住；但会把「你在用旧版本」喊得很响。
"""

import argparse
import os
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRANCH = "main"


def git(*args, **kw):
    timeout = kw.pop("timeout", 30)
    try:
        p = subprocess.run(("git",) + args, cwd=SKILL_DIR, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        return 127, "", "git 未安装"
    except subprocess.TimeoutExpired:
        return 124, "", "超时 %d 秒" % timeout
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def say(msg):
    print(msg, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    q = args.quiet

    def log(msg):
        if not q:
            say(msg)

    code, _, _ = git("rev-parse", "--is-inside-work-tree")
    if code != 0:
        log("[同步] 本目录不是 git 仓库，跳过同步（技能内容以此处为准）")
        say("[同步] 非 git 安装，跳过") if q else None
        return 0

    code, url, _ = git("remote", "get-url", "origin")
    if code != 0 or not url:
        log("[同步] 没有 origin 远端，跳过同步")
        say("[同步] 无远端，跳过") if q else None
        return 0

    _, before, _ = git("rev-parse", "--short", "HEAD")

    log("[同步] 连 %s …" % url)
    code, _, err = git("fetch", "--quiet", "--prune", "origin", timeout=25)
    if code != 0:
        # 网络问题不让它挡住干活
        say("[同步] 连不上远端（%s），继续用本机版本 %s —— 规则可能已过期" %
            ((err or "网络不可达").splitlines()[0][:60], before))
        return 0

    code, cnt, _ = git("rev-list", "--left-right", "--count", "HEAD...origin/%s" % BRANCH, timeout=15)
    if code != 0:
        say("[同步] 读不到 origin/%s，继续用本机版本 %s" % (BRANCH, before))
        return 0

    try:
        ahead, behind = (int(x) for x in cnt.split())
    except ValueError:
        ahead, behind = 0, 0

    if behind == 0:
        tail = "（本地领先 %d 个提交，记得 push）" % ahead if ahead else ""
        say("[同步] 已是最新 %s%s" % (before, tail))
        return 0

    log("[同步] 落后 %d 个提交，开始更新 …" % behind)
    code, _, err = git("pull", "--rebase", "--autostash", "--quiet", "origin", BRANCH, timeout=90)

    if code != 0:
        _, conflicts, _ = git("diff", "--name-only", "--diff-filter=U")
        say("[同步] 更新失败：%s" % (err.splitlines()[0] if err else "未知原因"))
        if conflicts:
            say("[同步] 冲突文件：%s" % ", ".join(conflicts.splitlines()))
            if any(f.endswith("ledger.json") for f in conflicts.splitlines()):
                say("[同步] ledger.json 用脚本合并，别手改：")
                say("       git show :2:ledger.json > /tmp/mine.json")
                say("       git show :3:ledger.json > /tmp/theirs.json")
                say("       python scripts/ledger_union.py /tmp/mine.json /tmp/theirs.json > ledger.json")
                say("       git add ledger.json && git rebase --continue")
        return 2

    _, after, _ = git("rev-parse", "--short", "HEAD")
    say("[同步] 已更新 %s → %s（%d 个提交）" % (before, after, behind))

    code, log_txt, _ = git("log", "--oneline", "-%d" % min(behind, 5), "HEAD", timeout=15)
    if code == 0 and log_txt:
        for line in log_txt.splitlines():
            say("       " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
