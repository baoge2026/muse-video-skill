#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""合并两台机器各自产生的提交账本，按 task id 去重。

ledger.json 冲突时（两台机器各提交过任务）用这个并，不要手改：

    git show :2:ledger.json > /tmp/base.json      # 当前分支那份
    git show :3:ledger.json > /tmp/other.json     # 拉下来的那份
    python scripts/ledger_union.py /tmp/base.json /tmp/other.json > ledger.json
    git add ledger.json && git commit

规则：同 id 保留「状态更靠后」的一条（completed > in_progress > queued > failed）；
      一边有 superseded_by / refund 而另一边没有时，取信息更全的那条。
"""
import json
import sys

RANK = {"queued": 0, "in_progress": 1, "failed": 2, "completed": 3, "canceled": 4}


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def richness(t):
    keys = ("superseded_by", "refund", "completed", "prompt_sha", "ts_epoch", "error")
    return sum(1 for k in keys if t.get(k) not in (None, "", "—"))


def better(a, b):
    ra, rb = RANK.get(a.get("status"), 0), RANK.get(b.get("status"), 0)
    if ra != rb:
        return a if ra > rb else b
    return a if richness(a) >= richness(b) else b


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2

    a, b = load(sys.argv[1]), load(sys.argv[2])
    merged = {}
    for src in (a, b):
        for t in src.get("tasks", []):
            tid = t.get("id")
            if not tid:
                continue
            merged[tid] = better(merged[tid], t) if tid in merged else t

    tasks = sorted(merged.values(), key=lambda t: t.get("ts_epoch") or 0)
    out = {
        "note": a.get("note") or b.get("note"),
        "tasks": tasks,
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
