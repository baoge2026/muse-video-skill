#!/usr/bin/env bash
# 把技能装到这台机器（或把它更新到最新），并检查能不能往仓库推改动。
#
#   bash scripts/bootstrap.sh              # 装/更新到 ~/.workbuddy/skills/muse生视频
#   bash scripts/bootstrap.sh /path/to/dir # 指定目录
#
# 幂等：已经有 git 副本就只做同步，不会覆盖你的磁盘内容。
set -u

REPO="https://github.com/baoge2026/muse-video-skill.git"
TARGET="${1:-$HOME/.workbuddy/skills/muse生视频}"
ARCHIVE="$HOME/.workbuddy/skills-archive"

say() { printf '%s\n' "$*"; }
die() { printf '[x] %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || die "没装 git，先装 git"

# 有些环境的 HTTP(S)_PROXY 指向只服务特定上游的本地代理，连 github 会 502，
# 而直连是通的。这里只对单条命令清代理，**不改系统与全局 git 配置**。
git_direct() {
  HTTP_PROXY= HTTPS_PROXY= http_proxy= https_proxy= ALL_PROXY= all_proxy= \
    git -c http.proxy= -c https.proxy= "$@"
}
git_net() {  # 先直连，失败再按环境代理走
  git_direct "$@" 2>/dev/null || git "$@"
}

mkdir -p "$(dirname "$TARGET")"

# ---------- 情况一：已经是 git 副本 → 只同步 ----------
if [ -d "$TARGET/.git" ]; then
  say "[1/4] 已有仓库，就地同步：$TARGET"
  cd "$TARGET" || die "进不去 $TARGET"
  python scripts/sync.py || die "同步失败（冲突按提示处理，或看 README 第五节）"
else
  # ---------- 情况二：目录存在但不是仓库 → 先备份再接管 ----------
  if [ -e "$TARGET" ]; then
    mkdir -p "$ARCHIVE"
    BAK="$ARCHIVE/muse生视频-旧版-$(date +%Y%m%d-%H%M%S)"
    say "[1/4] 目标已存在但不是 git 仓库，先备份到：$BAK"
    mv "$TARGET" "$BAK" || die "备份失败，中止"
    # 把本机密钥与账本带过来
    [ -f "$BAK/config.json" ] && KEEP_CONFIG=1 || KEEP_CONFIG=0
    [ -f "$BAK/ledger.json" ] && KEEP_LEDGER=1 || KEEP_LEDGER=0
  else
    KEEP_CONFIG=0
    KEEP_LEDGER=0
    say "[1/4] 全新安装到：$TARGET"
  fi

  say "[2/4] 克隆仓库 …"
  git_net clone --quiet "$REPO" "$TARGET" || die "克隆失败（直连与代理都不通）"
  cd "$TARGET" || die "进不去 $TARGET"

  if [ "${KEEP_CONFIG:-0}" = "1" ]; then
    cp "$BAK/config.json" . && say "      已恢复本机 config.json（密钥不用重填）"
  fi
  if [ "${KEEP_LEDGER:-0}" = "1" ] && [ -f "$BAK/ledger.json" ]; then
    python scripts/ledger_union.py ledger.json "$BAK/ledger.json" > .ledger.tmp 2>/dev/null \
      && mv .ledger.tmp ledger.json && say "      已合并本机旧账本（防止重复花钱）"
  fi
fi

# ---------- 本地设置：每台一次，幂等 ----------
git config core.quotepath false
git config core.hooksPath scripts/git-hooks
chmod +x scripts/git-hooks/pre-commit 2>/dev/null || true

# ---------- 密钥 ----------
if [ ! -f config.json ]; then
  cp config.example.json config.json
  say ""
  say "!!! 还差一步：编辑 $TARGET/config.json 填入 api_key"
  say "    取 key：https://router.yizzav.com/keys"
  say ""
fi

# ---------- 能不能推改动的自检 ----------
say "[3/4] 检查推送权限 …"
if git_net push --dry-run --quiet origin HEAD:main >/dev/null 2>&1; then
  say "      可以直接 push：这台机器改了规则能推回 GitHub"
else
  say "      只能拉、不能推。要在本机改规则并同步给别人，按下面任一种授权："
  say "        A) gh auth login          # 用 baoge2026 登录（推荐）"
  say "        B) 生成 PAT(classic, repo 权限) 后："
  say "           git config credential.helper store"
  say "           git push origin main    # 提示时用户名 baoge2026，密码填 PAT"
  say "      只拉不推的话，什么也不用配。"
fi

# ---------- 收尾 ----------
say "[4/4] 版本"
git log --oneline -3
say ""
say "完成。以后每次动手前跑：python \"$TARGET/scripts/sync.py\""
say "备份在：$ARCHIVE（确认无误后可删，别留在 skills 目录下）"
