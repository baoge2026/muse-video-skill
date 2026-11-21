# muse生视频 技能（多机共享版）

Nova AI 中转（router.yizzav.com）的 Muse 视频模型出片技能。一份文本，多台电脑共用，
**每台机器都能拉取更新、也都能提交自己的优化**。

- 仓库：`https://github.com/baoge2026/muse-video-skill`（公开，可直接 clone 安装）
- 技能落地目录：`~/.workbuddy/skills/muse生视频`（**必须叫这个名**，WorkBuddy 靠 `SKILL.md` 的 `name` 识别）

---

## 一、为什么用 git 而不是网盘同步

| 方式 | 问题 |
|---|---|
| 网盘 / Syncthing 直接同步目录 | 没有历史，改错了回不去；两边同时改时后写的覆盖先写的，丢改动还发现不了 |
| 微信 / 手动拷 zip | 不知道哪台是最新，容易把旧版覆盖回去 |
| **git** | 每次改动有记录、能回滚、冲突会明确报出来让你决定，`git pull` 一行就是最新版 |

---

## 二、新机器第一次装（三步）

```bash
# 1. clone 到技能目录（目录名固定，不要改）
git clone https://github.com/baoge2026/muse-video-skill.git ~/.workbuddy/skills/muse生视频

cd ~/.workbuddy/skills/muse生视频

# 2. 配密钥（config.json 不进仓库，每台机器各填一次）
cp config.example.json config.json
# 编辑 config.json，把 api_key 换成自己的（https://router.yizzav.com/keys）

# 3. 开两个本地设置（每台一次）
git config core.quotepath false          # 中文文件名正常显示
git config core.hooksPath scripts/git-hooks   # 提交前自动拦明文密钥
```

装完直接可用，不需要重启。

### 如果这台机器已经有旧版技能目录（不是 git 仓库）

先备份，再原地接管，**别直接 clone 到非空目录**：

```bash
cd ~/.workbuddy/skills
# 备份要挪出 skills 目录，留在里面会被当成第二个技能重复触发
mkdir -p ~/.workbuddy/skills-archive
mv muse生视频 ~/.workbuddy/skills-archive/muse生视频-旧版-$(date +%Y%m%d)
git clone https://github.com/baoge2026/muse-video-skill.git muse生视频
cd muse生视频
cp ~/.workbuddy/skills-archive/muse生视频-旧版-*/config.json .   # 把本机密钥带回来
git config core.quotepath false && git config core.hooksPath scripts/git-hooks
```

确认无误后再删备份目录。

---

## 三、每次使用前先同步，改完立刻推

**这条对每台机器都成立，包括当初建仓库的那台。** 没有「主机器」，每台都是完整副本，
谁改都行，谁都能拉。

### 用之前（每次，含本机）

```bash
cd ~/.workbuddy/skills/muse生视频 && python scripts/sync.py
```

三种结果，都能继续干活：

| 输出 | 含义 |
|---|---|
| `已是最新 a45b8e8` | 就是最新，直接开始 |
| `已更新 a45b8e8 → 9f2c1ab（2 个提交）` + 改动列表 | 自动拉好了，看一眼改了什么 |
| `连不上远端…继续用本机版本` | 网络问题，不挡路，但要知道规则可能已过期 |

技能流程里它已经是**第 ⓪ 步**；`preflight.py` 的 G 组还会再查一次版本，落后会记「提醒」，
所以你不可能在不知情的情况下按旧规则花钱。

### 改之前先拉，改完立刻推

**这是避免冲突的唯一办法。**

```bash
cd ~/.workbuddy/skills/muse生视频
git pull --rebase            # ① 先拿别人的改动
# ② 改 SKILL.md / references / scripts
git add -A
git commit -m "SKILL: 补 7.6 节 摇镜揭示的写法"
git push                     # ③ 立刻推，别攒
```

在另一台机器上更新，就一行：

```bash
cd ~/.workbuddy/skills/muse生视频 && git pull --rebase
```

拉完检查技能有没有真的生效：

```bash
head -3 SKILL.md                  # 看 name/description 是否为最新
git log --oneline -5              # 看最近是谁改了什么
```

### 提交信息怎么写

看历史时能一眼知道改了什么、为什么改，比「update」有用得多：

```
SKILL: 新增 7.5 时间流逝不可用
REF: 段边界改按色温 U 判定
SCRIPT: preflight 增加台词卡交叉核对
FIX: check_words 同段接排漏写 seg_last_end
```

---

## 四、密钥与账本怎么处理

| 文件 | 进仓库？ | 说明 |
|---|---|---|
| `config.json` | **否**（已 .gitignore） | 本机密钥，每台各填一次。`config.example.json` 是模板 |
| `ledger.json` | **是** | 提交账本要跨机共享，否则两台机器各花一次钱。项目路径是本机的，换机匹配不上不影响判定 |
| `.muse-preflight.json` | 否 | 绑本机文件指纹的预检令牌，换机必失效，由 preflight 重新签发 |
| `__pycache__/` | 否 | Python 缓存 |

**提交前的密钥拦截**：`scripts/git-hooks/pre-commit` 会扫描暂存内容里的
`sk-...` / `gho_...` / `AKIA...` 等长串，命中直接拒绝提交。所以别把密钥写进
`README.md`、示例 prompt 或注释里——钩子会拦住你。

---

## 五、冲突了怎么办

`git pull --rebase` 时报冲突，先看是哪个文件：

### `ledger.json` 冲突（最常见）

两台机器各自提交过任务，两边都新增了任务条目。**不要手改 JSON**，用脚本并：

```bash
git show :2:ledger.json > /tmp/mine.json
git show :3:ledger.json > /tmp/theirs.json
python scripts/ledger_union.py /tmp/mine.json /tmp/theirs.json > ledger.json
git add ledger.json
git rebase --continue
```

脚本按 `task id` 去重，同 id 保留状态更靠后、信息更全的那条。

### `SKILL.md` / `references/*.md` 冲突

两边改了同一段。打开文件，`<<<<<<<` 与 `>>>>>>>` 之间取正确的那份（或合并两边的意思），
删掉冲突标记，`git add` 后 `git rebase --continue`。

**预防**：改之前 `git pull`，不要在两台机器上同时改同一节。守则类文档改起来最容易撞车，
一次只在一台机器上动 `SKILL.md`，其他机器只改 `references/` 或 `scripts/`。

### 想放弃本机改动，直接跟随远端

```bash
git fetch origin && git reset --hard origin/main
```

（`config.json` 不会被覆盖，它在 .gitignore 里。）

---

## 六、目录结构

```
muse生视频/
├── SKILL.md                     技能主文件（触发词、硬事实、流程、验收）
├── assets/prompt-连续镜头模板.txt  连续镜头 prompt 骨架
├── config.example.json          密钥模板（复制成 config.json）
├── ledger.json                  提交账本（共享，防跨机重复花钱）
├── references/                  六个专题文档 + 复盘
│   ├── 接口与计费.md
│   ├── 光影与声轨控制.md
│   ├── 念稿极限测试.md
│   ├── 素材组合策略.md
│   ├── 段边界与台词收口.md
│   └── 复盘-2026-11-21.md
├── scripts/
│   ├── muse_video.py            quote / submit / wait / download / ledger / refund
│   ├── preflight.py             提交前终检（含版本新鲜度），全绿才签发令牌
│   ├── check_words.py           台词时间线容量核算
│   ├── sync.py                  拉取远端最新版（每次使用前跑）
│   ├── ledger_union.py          账本冲突合并
│   └── git-hooks/pre-commit     密钥拦截
├── .gitignore / .gitattributes
├── CHANGELOG.md                 版本与改动记录
└── README.md                    本文件
```

---

## 七、几条硬提醒

1. **目录名不要改**。改成 `muse-video` 之类，`SKILL.md` 里的 `name: muse生视频` 与路径不一致，
   部分加载器会识别失败。要换名就同步改 frontmatter。
2. **`~/.workbuddy/skills/` 下不要嵌套两个同名技能**，例如同时存在 `muse生视频` 和
   `muse生视频-旧备份`，会重复触发同一技能。备份请放到 `~/.workbuddy/skills-archive/`。
3. **要给 Codex 那边用**，别复制第二份（会漂移）。用链接指向同一个目录：
   ```bash
   # Windows（管理员或开发者模式）
   mklink /J "%USERPROFILE%\.codex\skills\muse生视频" "%USERPROFILE%\.workbuddy\skills\muse生视频"
   # macOS / Linux
   ln -s ~/.workbuddy/skills/muse生视频 ~/.codex/skills/muse生视频
   ```
4. **`git pull` 不会动你的 `config.json`**，但也别把它 `git add -f` 强行加进去，一旦进历史就只能
   换密钥来补救。
5. **账本是真金白银的防线**。发现 `ledger.json` 里有一条 `queued` 或 `in_progress` 却没人跑，
   先 `python scripts/muse_video.py ledger --refresh` 查真实状态，别直接删条目开新任务。
