#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""向 GitHub 要最新版本——技能的唯一真源是远端仓库，本机只是工作副本。

    python scripts/sync.py              # 详细输出，落后自动拉
    python scripts/sync.py --quiet      # 一行结果（技能流程内部调用）
    python scripts/sync.py --check      # 只查不拉（preflight 用）
    python scripts/sync.py --json       # 结构化结果（机器读）
    python scripts/sync.py --hard       # 以远端为准，丢弃本机未提交改动（慎用）
    python scripts/sync.py --push       # 先同步到最新，再把本机提交推上去

退出码：
    0  已是最新 / 已更新成功 / 已推送 / 离线 / 非 git 安装
    2  拉取冲突，需要人工处理（多半是 ledger.json，用 scripts/ledger_union.py 合）
    3  --check 模式下确认落后（只是没跟上，不算错误，供调用方判断）
    4  --push 推送失败（多为没有推送权限，见 README「让这台机器也能推」）

网络：自动在「直连 / 本机 7897 / 继承环境代理」之间挑一条通的，记住上次成功的那条。
有些机器上 HTTP(S)_PROXY 指向只服务特定上游的本地代理，连 github 会 502，直连或
本机 Clash 反而是通的——这一步不改任何系统或全局 git 代理设置。

设计原则：**同步失败不阻塞出片**。网络断了宁可让你用旧规则干活，
也不要因为拉不到代码就卡住；但会把「这是哪个版本、是否确认最新」说清楚，
并在规则真的变了的时候明确要求你重读 SKILL.md。
"""

import argparse
import json
import os
import subprocess
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(SKILL_DIR, ".sync-state.json")
FALLBACK_BRANCH = "main"

# 网络路径候选。有些环境会把 HTTP(S)_PROXY 指向一个只服务特定上游的本地代理，
# 对 github.com 会回 502；此时直连反而通。反过来也有必须走代理的网络。
# 所以按顺序自动试，试通哪条记住哪条，**不改任何系统/全局代理设置**。
CLASH_PROXY = "http://127.0.0.1:7897"
PROXY_MODES = ("direct", "clash", "env")
MODE_LABEL = {"direct": "直连", "clash": "本机 7897", "env": "继承环境代理"}


def git(*args, **kw):
    timeout = kw.pop("timeout", 30)
    mode = kw.pop("proxy", None)  # None = 完全继承当前环境
    cmd = ["git"]
    env = dict(os.environ)

    if mode == "direct":
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                  "ALL_PROXY", "all_proxy", "GIT_PROXY_COMMAND"):
            env.pop(k, None)
        cmd += ["-c", "http.proxy=", "-c", "https.proxy="]
    elif mode == "clash":
        for k in ("ALL_PROXY", "all_proxy"):
            env.pop(k, None)
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[k] = CLASH_PROXY
        cmd += ["-c", "http.proxy=" + CLASH_PROXY, "-c", "https.proxy=" + CLASH_PROXY]

    try:
        p = subprocess.run(cmd + list(args), cwd=SKILL_DIR, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=env)
    except FileNotFoundError:
        return 127, "", "git 未安装"
    except subprocess.TimeoutExpired:
        return 124, "", "超时 %d 秒" % timeout
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def fetch_any():
    """依次尝试直连 / 本机 Clash / 继承环境，返回 (成功, 错误摘要, 用到的模式)。"""
    st = read_state()
    preferred = st.get("net_mode")
    order = ([preferred] if preferred in PROXY_MODES else [])
    order += [m for m in PROXY_MODES if m not in order]

    errs = []
    for mode in order:
        code, _, err = git("fetch", "--quiet", "--prune", "origin", timeout=25, proxy=mode)
        if code == 0:
            if preferred != mode:
                write_state(net_mode=mode)
            return True, "", mode
        first = (err or "未知错误").splitlines()[0][:70]
        errs.append("%s→%s" % (MODE_LABEL[mode], first))
    return False, "; ".join(errs), None


def push_any(branch):
    """把本机提交推上去，同样自动挑一条通的路。"""
    st = read_state()
    preferred = st.get("net_mode")
    order = ([preferred] if preferred in PROXY_MODES else [])
    order += [m for m in PROXY_MODES if m not in order]

    errs = []
    for mode in order:
        code, o, err = git("push", "--quiet", "origin", "HEAD:%s" % branch,
                           timeout=90, proxy=mode)
        if code == 0:
            if preferred != mode:
                write_state(net_mode=mode)
            return True, "", mode, o
        first = (err or "未知错误").splitlines()[0][:70]
        errs.append("%s→%s" % (MODE_LABEL[mode], first))
    return False, "; ".join(errs), None, ""



def write_state(**kw):
    try:
        old = {}
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, encoding="utf-8") as f:
                old = json.load(f)
        old.update(kw)
        old["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(old, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def read_state():
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    return {}


def current_branch():
    code, br, _ = git("rev-parse", "--abbrev-ref", "HEAD", timeout=5)
    if code == 0 and br and br != "HEAD":
        return br
    # 分离头指针或空仓库：退回远端默认分支
    code, ref, _ = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", timeout=5)
    if code == 0 and ref:
        return ref.split("/", 1)[1]
    return FALLBACK_BRANCH


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--check", action="store_true", help="只检查，不拉取")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--hard", action="store_true", help="以远端为准，丢弃本机未提交改动")
    ap.add_argument("--push", action="store_true", help="同步到最新后，把本机提交推到 GitHub")
    args = ap.parse_args()

    def log(msg):
        if not args.quiet and not args.json:
            print(msg, flush=True)

    out = {"ok": True, "status": "", "head": "", "remote": "", "branch": "",
           "behind": 0, "ahead": 0, "from": "", "to": "", "changes": [],
           "url": "", "offline": False, "stale_hours": None}

    def finish(code, status, msg=""):
        out["status"] = status
        if msg:
            out["message"] = msg
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=1))
        elif args.quiet:
            print(msg or status)
        return code

    # ① 是否 git 安装
    code, _, _ = git("rev-parse", "--is-inside-work-tree", timeout=5)
    if code != 0:
        if args.quiet:
            print("[同步] 非 git 安装，跳过")
        else:
            log("[同步] 本目录不是 git 仓库，跳过同步（技能内容以此处为准）")
        return finish(0, "not-git", "[同步] 非 git 安装，跳过")

    # ② 有没有远端
    code, url, _ = git("remote", "get-url", "origin", timeout=5)
    if code != 0 or not url:
        log("[同步] 没有 origin 远端，跳过同步")
        return finish(0, "no-remote", "[同步] 无远端，跳过")

    branch = current_branch()
    _, before, _ = git("rev-parse", "--short", "HEAD", timeout=5)
    out.update(head=before, branch=branch, url=url, remote="origin")

    # ③ 拉远端（自动挑一条通的路）
    log("[同步] 源 %s（%s）" % (url, branch))
    log("[同步] 本机 %s，查询远端 …" % (before or "空"))
    ok, err, mode = fetch_any()
    if not ok:
        st = read_state()
        stale = None
        try:
            last = time.mktime(time.strptime(st.get("updated_at", ""), "%Y-%m-%d %H:%M:%S"))
            stale = (time.time() - last) / 3600.0
        except (ValueError, OverflowError):
            pass
        out.update(ok=False, offline=True, remote_head=before,
                   stale_hours=round(stale, 1) if stale is not None else None)
        msg = "[同步] ! 三条路都连不上（%s），用本机 %s —— 无法确认是否最新" % (err, before)
        if stale is not None:
            msg += "（上次确认为最新是 %.1f 小时前）" % stale
        print(msg, flush=True)
        return finish(0, "offline", msg)

    log("[同步] 网络路径：%s" % MODE_LABEL.get(mode, mode))

    _, remote_head, _ = git("rev-parse", "--short", "origin/%s" % branch, timeout=5)
    out["remote_head"] = remote_head

    code, cnt, _ = git("rev-list", "--left-right", "--count",
                       "HEAD...origin/%s" % branch, timeout=15)
    if code != 0:
        msg = "[同步] 读不到 origin/%s，继续用本机 %s" % (branch, before)
        print(msg, flush=True)
        return finish(0, "no-upstream", msg)

    try:
        ahead, behind = (int(x) for x in cnt.split())
    except ValueError:
        ahead, behind = 0, 0
    out.update(ahead=ahead, behind=behind, from_head=before, to_head=remote_head)

    def do_push():
        log("[推送] 推送到 origin/%s …" % branch)
        okp, errp, modep, _ = push_any(branch)
        if not okp:
            print("[推送] 失败：%s" % errp, flush=True)
            print("[推送] 若提示 403/权限不足，这台机器还没配推送授权：\n"
                  "        gh auth login        # 用 baoge2026 登录（推荐）\n"
                  "        或配 PAT，见 README「让这台机器也能推」", flush=True)
            return finish(4, "push-failed", "[推送] 失败，见上方提示")
        _, now, _ = git("rev-parse", "--short", "HEAD", timeout=5)
        out["head"] = now
        print("[推送] 已推送到 GitHub：%s（路径：%s）"
              % (now, MODE_LABEL.get(modep, modep)), flush=True)
        return finish(0, "pushed", "[推送] 已推送 %s" % now)

    # ④ 已最新
    if behind == 0:
        write_state(head=before, branch=branch)
        tail = ""
        if ahead and not args.push:
            tail = "（本地领先 %d 个提交，记得 push）" % ahead
        elif ahead:
            tail = "（本地领先 %d 个提交）" % ahead
        log("[同步] 已是最新 %s%s" % (before, tail))
        if ahead:
            out["pending_push"] = True
        if not args.push:
            return finish(0, "up-to-date", "[同步] 已是最新 %s%s" % (before, tail))
        return do_push()

    if args.check:
        msg = "[同步] 落后 %d 个提交（本机 %s → 远端 %s），需要更新" % (behind, before, remote_head)
        print(msg, flush=True)
        return finish(3, "behind", msg)

    # ⑤ 更新（已 fetch 过，rebase 走本地对象，不再依赖网络）
    log("[同步] 落后 %d 个提交，开始更新 …" % behind)
    if args.hard:
        code, _, err = git("reset", "--hard", "origin/%s" % branch, timeout=30)
    else:
        code, _, err = git("rebase", "--autostash", "--quiet",
                           "origin/%s" % branch, timeout=120)

    if code != 0:
        _, conflicts, _ = git("diff", "--name-only", "--diff-filter=U", timeout=10)
        first = err.splitlines()[0] if err else "未知原因"
        print("[同步] 更新失败：%s" % first, flush=True)
        if conflicts:
            files = conflicts.splitlines()
            print("[同步] 冲突文件：%s" % ", ".join(files), flush=True)
            if any(f.endswith("ledger.json") for f in files):
                print("[同步] ledger.json 用脚本合并，别手改：", flush=True)
                print("       git show :2:ledger.json > /tmp/mine.json", flush=True)
                print("       git show :3:ledger.json > /tmp/theirs.json", flush=True)
                print("       python scripts/ledger_union.py /tmp/mine.json /tmp/theirs.json > ledger.json", flush=True)
                print("       git add ledger.json && git rebase --continue", flush=True)
        return finish(2, "conflict", "[同步] 冲突，需人工处理")

    _, after, _ = git("rev-parse", "--short", "HEAD", timeout=5)
    write_state(head=after, branch=branch)
    out.update(head=after, from_head=before, to_head=after)

    changes = []
    code, log_txt, _ = git("log", "--oneline", "-%d" % min(behind, 8),
                           "%s..HEAD" % (before or "HEAD~1"), timeout=15)
    if code == 0 and log_txt:
        changes = log_txt.splitlines()

    print("[同步] 已更新 %s → %s（%d 个提交）" % (before, after, behind), flush=True)
    for line in changes:
        print("       " + line, flush=True)
    # 规则变了就必须重读，否则上下文里还是旧规矩
    print("[同步] ! 规则已变更 —— 动手前重新读一遍 SKILL.md，别用记忆里的旧流程", flush=True)

    out["changes"] = changes
    if args.push:
        return do_push()
    return finish(0, "updated", "[同步] 已更新 %s → %s（%d 个提交）" % (before, after, behind))


if __name__ == "__main__":
    raise SystemExit(main())
