# 换一台机器怎么复现

这份文档只回答一个问题：**拿到封存包，怎么确认你手里这一份和我冻结的那一份一致。**

---

## 1　需要什么

| 项 | 要求 | 冻结时的实测值 |
|---|---|---|
| Python | ≥ 3.10 | 3.13.14 |
| git | 任意 2.x | 2.x |
| 第三方包 | **一个都不需要** | 运行时纯标准库 |
| 模型凭据 | 只有真正跑任务时才需要 | `DEEPSEEK_KEY` 或 codex 登录态 |

只有 Python 与 git 是硬要求。**不联网也能装好、能跑测试。**

---

## 1.2　把 orch 装成命令（一次）

```bash
python scripts/install_cli.py
```

会在 `~/bin`（Windows 为 `%USERPROFILE%\bin`）写两个文件：`orch.py`（入口，UTF-8，
能装中文路径）和 `orch.cmd` / `orch`（垫片，纯 ASCII）。之后**任何目录**都能用 `orch ...`，
不需要先 `cd` 到仓库——真实使用时客户端开在哪都行。

装完会自己验证一次（按名字跑 `orch --help`）。若提示不在 PATH，把 `~/bin` 加进去再开新终端。
换机器或换了仓库路径，重跑这条命令即可。

## 1.5　先跑一次环境自检（不花钱）

```bash
python scripts/install_cli.py   # 先装命令
python scripts/preflight.py     # 再验环境
```

它检查 Python、git、codex 二进制、**沙箱能不能起子进程**、凭据在不在，一次模型都不调。
沙箱那项是真实踩过的坑：新版 codex 在某些 Windows 环境上 `workspace-write` 起不了进程，
worker 因此一个文件都改不了——而 orch 在 Windows 默认改走 bypass，这条检查确认那条路是通的。

## 2　从封存包复现（推荐）

```bash
# 1) 校验包没坏
sha256sum -c dist/SHA256SUMS.txt      # macOS: shasum -a 256 -c

# 2) 从 bundle 克隆（不依赖任何远程仓库）
git clone dist/orch-v1.9.2.bundle orch
cd orch   # 本仓库目前**没有 tag**；以 `git rev-parse HEAD` 为准，不要 checkout 不存在的标签

# 3) 跑测试：应输出 370 passed, 1 skipped（约 5 分钟）
python -m unittest discover -s tests -t .
```

测试慢是故意的：多数用例真的会 `git init`、`git worktree add`、起子进程跑验收。
（原旧文档写 `Ran 343 tests ... OK`、约 8 分钟，已过时。）

测试慢是故意的，但也会把"沙箱起不来""worktree 锁残留""中文路径被转义"
这类只有真跑才暴露的问题一起 mock 掉。

## 3　快速冒烟（30 秒，不烧模型）

不想等 8 分钟，跑这几条也能确认基本完整：

```bash
python -m orch --help                                # 应列出 17 个子命令
python -m orch validate examples/tasks.good.json     # 应通过
python -m orch tools                                 # 应看到 8 个 ✓ 工具
```

再验一次办公流水线（生成 → 断言，全程不调模型）：

```bash
printf '# 标题\n\n正文一段。\n' > /tmp/a.md
python -m orch.docxgen /tmp/a.md /tmp/a.docx
python -m orch.docxcheck /tmp/a.docx --min-chars 4 --require-heading   # 退出码 0
```

---

## 4　真正跑一个任务

```bash
python scripts/make_demo.py          # 从 demo/fixture 重建 demo/tasks_app
export DEEPSEEK_KEY=...              # 或 codex login

python -m orch.dispatch "给 storage.py 加持久化" \
  --repo demo/tasks_app --plan examples/tasks.good.json --mode foreground
```

跑完会打印 `run <id>`。产物在集成分支，落到工作区用：

```bash
python -m orch apply <run_id> --runs-dir demo/tasks_app/.orch-runs
```

---

## 4.5　量一次"省了多少"

orch 侧的数字在报告里（`token 输入 …（缓存 …）· 输出 … · 轮次 …`）。
另一半——**同一件事让 Codex 独自做花多少**——用基线脚本量：

```bash
# 用 codex 自己的默认模型（那才是"Codex 独自"，会花订阅额度）
python scripts/baseline.py --repo <仓库> --task "<原话>" --raw \
    --accept "<验收命令>" --vs <runs>/<run_id> --json-out baseline.json

# 或用 workers.json 里某一档（同模型，用来隔离"编排本身的开销"）
python scripts/baseline.py --repo <仓库> --task "<原话>" \
    --worker codex-ds-flash --accept "<验收命令>" --vs <runs>/<run_id>
```

两边都用 `--json` 事件流解析，口径一致。看三个数：**未缓存输入**、**输出**、**轮次**。
输入总量不要直接比——里面绝大部分是缓存命中的系统提示。

## 5　平台差异（影响行为，不影响结果）

| 项 | Windows | Linux / macOS | 为什么 |
|---|---|---|---|
| worker 沙箱 | 默认 `bypass` | 默认 `workspace-write` | Windows 上 `workspace-write` 会卡在启动，worker 一个文件都改不了 |
| 强制结束进程 | `taskkill /T /F` | `kill -TERM` | `cleanup --force` 先核对进程命令行里有没有 run id，对不上就拒绝 |
| 编码 | utf-8 → gbk 回退 | 直接 utf-8 | 本地代码页会把解码炸在读取线程里 |

隔离仍有两道：每个任务独立 worktree + 事后的越界检查。`bypass` 指的是 **codex 自己的**沙箱，
不是把这套隔离绕过去——`orch/dispatch.py` 里有注释。

---

## 6　它**不**保证什么

- **不保证好看**。docx / xlsx / pptx 的生成器只写内容层；版式、配色、图表不在范围内。
- **不保证跨版本可比**。`codex-cli 0.167.0-alpha.3.2` 是冻结时核验的版本；换版本可能改变行为
  （尤其 sandbox 与 argv），换之前先 `python -m orch.dispatch --help` 对照一遍。
- **不保证性能**。判据松一格不行，慢一点可以。

---

## 7　冻结点在哪

| 项 | 值 |
|---|---|
| tag | 无（以 `git rev-parse HEAD` 为准） |
| 设计规格 | `report/04-设计冻结-v1.9-2026-11-09.pdf` §8.1 |
| 测试 | 371 项（pytest 收集；含 2026-11-12 新增回归） |
| 命令面 | 17 个子命令 |
| 错误码 | E001–E024（24 条）· W001–W006（6 条） |
| worker 壳 | 核验通过的是 `codex-cli`；模型档 `deepseek-flash` 为默认 |

---

## 8　出错时先看哪里

| 现象 | 先查 |
|---|---|
| 测试里 git 报 `Author identity unknown` | 仓库缺 `user.name` / `user.email`；runner 有前置检查会直接说这句话 |
| 任务"完成"但文件没变 | 看 `runs/<id>/artifacts/*.diff` 是不是 0 字节——沙箱起不来就会这样 |
| `apply` 说工作区脏 | `git status -uall`；缓存目录（`__pycache__`）已豁免，其余是真的未提交改动 |
| 找不到 `verification.json` | 不是错误：仓库根没有就跳过整体验收，报告里会写"不声称整体可用" |
| `cleanup --force` 被拒绝 | 它在核对 pid 的命令行，防止 pid 复用误杀；确认那个进程真是这次 run 的 |
