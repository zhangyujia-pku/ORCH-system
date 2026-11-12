# 出入口闭环 · 手动验收方案（学生场景）

> 本文保留此前 M2 场景步骤。第三步之后，dispatch 需显式提供固定检查配置；本场景目标基线已有 `checks/check_all.py` 时，可加 `--verification examples/verification.example.json`。
> 完成后在报告给出的独立整合目录运行检查，不能继续把原目标目录当作合并产物。以 [第三步说明](第三步_可信验收与整合.md) 的配置和成功条件为准。

**目标**：在 Codex 对话窗口里说一句话，产物回到**同一个窗口**。

这份方案只用两个地方：

- **窗口** = Codex 桌面端的对话窗口（说需求、看结果）
- **终端** = 任意普通终端（看状态、核证据）

预计 15–20 分钟，其中 12 分钟在等模型跑。

---

## 选哪个场景

| 场景 | 验证什么 | 外部依赖 | 建议 |
|---|---|---|---|
| **文本润色**（主） | 全链路 + 多文件并行拆分 | 无，纯标准库 | 先跑这个 |
| PDF 讲义生成（进阶） | 产物是二进制文件 | 浏览器渲染链路 | 文本跑通后再试 |

**为什么先跑文本**：orch 本身能不能跑通，和"PDF 能不能渲染出来"是两件事。
先跑零依赖的场景，卡住时你能确定问题出在 orch；一上来就跑 PDF，卡住了分不清是谁的锅。

---

## 0 前置（3 分钟，只做一次）

### 0.1 重启宿主

代码在 00:49 改过（thread 定位不再白等 45 秒），旧宿主进程还是老逻辑，必须换掉。

在**跑着 `--serve` 的那个终端**按 `Ctrl+C`，然后重新启动：

```bash
cd <orch 仓库目录>
python -m orch.dispatch --serve
```

（找不到那个终端就先用 `--status` 拿 pid，再 `taskkill /PID <pid> /F`。）

### 0.2 确认宿主在跑

新开一个终端：

```bash
cd <orch 仓库目录>
python -m orch.dispatch --status
```

**预期**

```
宿主      pid <数字>
队列      0 个待办  <本机路径>\.orch\queue
```

`pid` 是 0 或"未启动"就停下，别往下走。

### 0.3 确认测试仓库是"待办"状态

```bash
cd <本机路径>\orch-e2e-text
git log --oneline
git status --short
python checks\check_all.py
```

**预期**

- 只有 1 个 commit：`init 读书报告润色任务`
- `git status` 无输出
- 校验脚本输出 **`0/3 份合格`**，退出码 1 —— 这是对的，草稿本来就该不合格

三份草稿在 `drafts/`，规范在 `standards/写作规范.md`，判据在 `checks/check_all.py`。
跑完整个目录可以删，不影响任何真实文件。

### 0.4 记下三个位置

| 东西 | 路径 |
|---|---|
| 运行目录 | `<本机路径>\orch-e2e-text-runs\<run_id>` |
| 宿主日志 | `<本机路径>\.orch\serve.log` |
| 测试仓库 | `<本机路径>\orch-e2e-text` |

---

## 1 主链路（一次跑通）

### 1.1 在 Codex 窗口里发起

**切到 Codex 窗口**，发这句话（整段复制）：

> 用 orch 把这件事交给编排器：按 standards/写作规范.md 润色 drafts/ 下三份草稿（01-引言.md、02-核心论点.md、03-结论.md），改成书面表达、每份补到 300 字以上、保留原意和一级标题。不要改 standards/ 和 checks/ 下的文件。验收：每个任务对自己负责的那份草稿运行 `python checks/check_all.py drafts/<文件名>` 退出码 0；三份合并后再跑一次 `python checks/check_all.py` 确认 3/3。仓库在 <本机路径>\orch-e2e-text

**预期（第一个判定点）**

- 工具调用里出现 `python -m orch.dispatch ...`
- **5 秒内**返回，输出里有 `run <8位>-<4位>`、`已入队，宿主 pid <数字> 会取走`
- Codex 回你一句带 run id 的话，**不会卡在那里等**

卡住超过 1 分钟 → 宿主没被识别，回去做 0.2。

### 1.2 看窗口定位对不对（最要紧）

```bash
tail -5 <本机路径>\.orch\serve.log
```

**预期**

```
[..] 取到 2026xxxx-xxxxxx-xxxx  thread=<uuid>
[..] thread  <uuid>  依据=pid(high)
```

**`依据=pid(high)` 是整套东西里最关键的一行。** 它证明 orch 顺着父进程找到了"我是哪个窗口"。

- `依据=cwd(medium)` → pid 没匹配上，走了兜底，可能投到同目录的另一个窗口
- `没认出来` → 回传整个跳过，这次只能看日志

### 1.3 等计划（1–3 分钟）

```bash
tail -5 <本机路径>\.orch\serve.log
```

**预期**

```
[..] plan    provider=codex-read-only
[..] plan    OK  3 任务 / 1 层  尝试=PASS
```

- `尝试=PASS` = 一次过
- `尝试=FAIL,PASS` = 第一次被判据不可判定打回、重写后通过，**这是正常路径**
- 规划未通过 → 尚未执行；区分校验拒绝和后端调用失败，不一律归因为需求含糊

**这里该是 3 个任务**：三份草稿写集不相交，正好并行。

### 1.4 等执行（1–4 分钟）

```bash
cd <本机路径>\orch-e2e-text-runs
tail -8 (ls -td */ | select -First 1)dispatch.log
```

（PowerShell 语法；Git Bash 里用 `tail -8 $(ls -td */ | head -1)dispatch.log`）

**预期**

```
[..] DONE  t01  xx.xs  x 文件
[..] DONE  t02  xx.xs  x 文件
[..] DONE  t03  xx.xs  x 文件
[..] queue   ok  Queued message ...
[..] job end  exit=0  delivered=True
```

**`delivered=True` 是第二个判定点**：报告已投进 Codex 的消息队列。

### 1.5 收报告

**切回 Codex 窗口**（必须切回去，见第 4 节第一条）。

**预期**：窗口里多出一条以你名义发出的消息：

```
【orch 完成】<run_id>

需求：按 standards/写作规范.md 润色 drafts/ 下三份草稿…
计划：3 任务 · 1 层 · t01 / t02,t03 / …
执行：3/3 完成 · OK

  DONE  t01  …
合并：orch/run-<run_id>
产物目录：<本机路径>\orch-e2e-text-runs\<run_id>
```

紧接着模型会复述一遍——在它看来这就是用户说的话。

---

## 2 核验（不看日志也要能验）

### 2.1 稿子真的改了

```bash
cd <本机路径>\orch-e2e-text
git log --oneline
git diff --stat HEAD~1
```

**预期**：多出 commit，`diff` 里有三份 `drafts/*.md`。

### 2.2 达到规范了吗

```bash
python checks\check_all.py
```

**预期**

```
PASS  drafts/01-引言.md  汉字 xxx
PASS  drafts/02-核心论点.md  汉字 xxx
PASS  drafts/03-结论.md  汉字 xxx

3/3 份合格
```

退出码 0。这是**真正验收的那一步**——前面所有的 `DONE` 都不算数，只有这个才算。

**注意粒度**：每个任务在**自己的隔离工作目录**里验收，那里只有它负责的那份改过，
所以判据必须是「只查自己那份」：

```bash
python checks\check_all.py drafts/01-引言.md    # → 1/1 份合格，exit 0
```

如果用全局的 `python checks/check_all.py`（要求 3/3）去验单个任务，必然失败——
另外两份在那个目录里还是原稿。这个坑实际踩过：三个任务都 `committed` 了，却全部
`verify=false`，报告写「0/3 完成」。

### 2.3 禁区真的没被碰吗（这条很有价值）

```bash
git diff --stat HEAD~3 -- standards checks
```

**预期：没有输出。**

如果这里出现了改动，说明 worker 为了"通过验收"去改了校验脚本或规范。
这是 `forbidden` 字段该拦住的东西，也是这套系统最该防的作弊路径。

### 2.4 事件链完整

```bash
cd <本机路径>\orch-e2e-text-runs
grep -o '"event": "[^"]*"' $(ls -td */ | head -1)trace.jsonl
```

**预期**（顺序大致如此）

```
run.start  plan.start  plan.accepted  run.start
layer.start  task.start  task.committed  task.verify
merge.result  run.end
```

出现 `plan.rejected` 也正常，那代表重写过。

---

## 3 判定标准

| # | 判定点 | 标准 |
|---|---|---|
| 1 | 入口不阻塞 | 窗口里 5 秒内返回 run id |
| 2 | 身份正确 | `依据=pid(high)` |
| 3 | 能并行拆分 | 计划是 3 任务、写集不相交 |
| 4 | 执行完成 | `exit=0`，三个任务全 `DONE` |
| 5 | 回传送达 | `delivered=True` 且窗口出现报告 |
| 6 | 产物合格 | `check_all.py` 输出 `3/3 份合格` |
| 7 | 没作弊 | `git diff HEAD~3 -- standards checks` 为空 |

第 7 条尤其重要：它验证的是"验收判据本身没被改写"。

---

## 4 故障对照表

| 现象 | 原因 | 处理 |
|---|---|---|
| 返回"已入队"但十几分钟没消息 | 该会话不活跃，消息在 Codex 队列排队 | 切回那个窗口，立刻出现。不算失败 |
| `thread 没认出来` | 不是从 Codex 窗口发起的 | 正常，回传跳过；可加 `--thread <uuid>` 显式指定 |
| `依据=cwd(medium)` | 命令被中转了一层，pid 没匹配上 | 能投，但可能投错窗口，核对收件人 |
| 只拆出 1 个任务 | 三个改动被判定写集重叠 | 看 `tasks.json` 的 `writes`；若都写了同一个文件，串行是对的 |
| 计划三次全 FAIL | 需求没给可锚定判据 | 补"验收：<可执行命令>" |
| `exit=4` | 有任务验收没过 | 看 trace 的 `task.verify`，`failures` 里是哪条 |
| 三个任务都 `committed` 却全 `verify=false` | 验收判据是全局的（要求 3/3），但验证在单个 worktree 里做 | 判据改成「只查自己那份」，见 2.2 |
| `delivered=False` | 没有 thread_id，或 `codex.exe` 没找到 | 看 job log 的 `queue` 行 |
| worker 改了 `checks/` | `forbidden` 没生效 | 这是 bug，把 run 目录留给我看 |
| 宿主进程没了 | 终端被关 / Ctrl+C | 重跑 `--serve`。队列不丢，下次启动继续消费 |

---

## 5 可选：故意跑一次失败路径（3 分钟）

验证"失败也会回传"。在窗口里发：

> 帮我润色一下这些稿子，弄好一点。

**预期**

- 入口返回带含糊度标记（`I001` 未声明验收标准、`I002` 含模糊词、`I003` 未指明改动范围）
- 计划可能重写或最终未通过；调用错误也可能提前停止
- 规划失败报告为 `【orch 计划未通过】`，明确尚未执行；通知是否送达单独检查，不保证一定送达

真实使用里失败比成功多，能看见失败才有得修。

---

## 6 PDF 讲义场景（进阶，先别急）

想做的话，形态是这样：

| 任务 | 产物 | 验收判据 |
|---|---|---|
| 按提纲生成讲义源文件 | `out/讲义.md` | 有 ≥3 个二级标题、≥2 个练习块、字数达标 |
| 转成 PDF | `out/讲义.pdf` | 文件存在、`%PDF-` 头正确、页数 ≥3 |

**为什么放第二步**：PDF 渲染依赖浏览器（Playwright + Chromium）或 LaTeX 之类的外部链路。
如果 worker 在沙箱里起不了浏览器，任务会失败——但失败的到底是 orch 还是渲染链路，很难分。
先把文本链路跑顺，再加这一步，问题才好定位。

真要跑的话，建议把转换做成确定性脚本放进仓库（`tools/md2pdf.py`），
让 worker 只负责生成 Markdown 和调用脚本，不让它自己去找工具链。

---

## 7 清理

```bash
# 看一眼 run 目录，DISPATCH.md 可以留作记录
ls <本机路径>\orch-e2e-text-runs

# 确认不需要了再删：
rmdir /S /Q <本机路径>\orch-e2e-text
rmdir /S /Q <本机路径>\orch-e2e-text-runs
```

宿主不想常驻就回那个终端按 `Ctrl+C`。

---

## 附：这段需求为什么这么写

它是照着 **L4 档**写的：说清改哪个文件、做什么、禁区在哪，并给一条可执行的验收命令。

`experiments/FINDINGS.md` 那批 30 条数据里，L3（说清改哪做什么、不给验收）重写率最低
（0.00），L4 是 0.17。这里仍然选 L4，原因是**文本任务的"好"太主观**——
不给 `check_all.py` 这个锚点，Planner 只能写出"表达流畅""逻辑清晰"这类不可判定的判据，
必然撞 `E014`。**先有可判定判据，才有得谈质量。**

三份草稿写集两两不相交，所以应该拆成 3 个并行任务。如果只拆出 1 个，
先看 `tasks.json` 里三条 `writes` 是不是撞在一起了。
