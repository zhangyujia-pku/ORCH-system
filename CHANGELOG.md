# 变更记录

## v1.9.5 · 2026-11-12（修复：长提示词在 Windows 上传不进去）

**症状**：在另一台机器上第一次派发就失败，报

```text
codex.exe 调用失败（退出码 1）：Reading additional input from stdin...
```

看起来像模型挂了或没登录，实际上模型一次都没被调用。

**根因**：planning 的提示词是「规则 + 需求 + 仓库文件清单」，动辄两三万字符，而
Windows 的命令行有硬上限（CreateProcess 32767 字符；被 `.cmd` 包装时 cmd.exe 只有 8191）。
塞进 argv 的后果是 prompt 被截断或干脆没送达，codex 于是转去读 stdin——
而 orch 把 stdin 设成 DEVNULL，读到的立刻是 EOF，退出码 1。
**报错内容和真实原因完全对不上**，这是最费时间的地方。

**改了什么**：

- prompt 超过 7000 字符时不再走 argv，改成 `codex exec ... -` + stdin 写入；
  argv 里只留一个 `-`。7000 是给 cmd.exe 的 8191 留的余量。
- 超长导致的 `WinError 206` 以前会被 `except FileNotFoundError` 捞走，
  报成「找不到可执行文件：codex.exe」——方向完全指错。现在单独识别并说「命令行太长」。
- 调用失败的报错补上 stdout 尾部；如果 stderr 里出现「Reading additional input from stdin」
  而 prompt 本来不算长，会直接提示「prompt 没送达，是传参问题，不是模型问题」。
- `scripts/preflight.py` 新增一条不花钱的检查：跑 `codex exec --help`，验证
  `--json` / `--skip-git-repo-check` / `--sandbox` / `--cd` / `-c` / `-m` 这个版本都认。
  以前只验 `--version`，版本不兼容要等到真正派发才炸。
- 规划阶段失败时，报告里明确写「未执行、worker 用量未采集、无法给出节省」，
  而不是整段不出现——**没有数字也要说清楚为什么没有**。

**测试**：+4（`tests/test_planner_stdin.py`），覆盖长/短 prompt 两条路径、
无 `{prompt}` 占位符的模板不被误塞 `-`、以及 WinError 206 的诊断文本。

**同一版里另外修了四处"看起来能用、其实不成立"的地方**：

- **安装脚本不装 skill**（从 v1.9.2 起就没装过）。产品前提是"在 Codex 里说需求、在 Codex 里收结果"，
  但 `install_cli.py` 只装 `orch` 命令，`skill/SKILL.md` 靠文档里一句"自己放到 skills 目录"。
  结果就是新机器上 Codex 根本不知道有 orch，用户被迫回到终端——主入口等于没装。
  现在一条命令装两样，`--check` 分别报告命令与 skill 的状态，装完提示重启 Codex。
- **跑完不自动合并**，用户看到"执行成功"但文件没变。原本要求再说一句"合并吧"（或自己敲 apply），
  这跟"用户不碰终端"冲突，也给了模型手抄文件的口子。现在成功后自动合并到用户当前分支：
  `apply_run` 的防护全部保留（运行必须成功、整合分支必须存在、不能在游离 HEAD 上合、
  工作区有未提交改动就不合、冲突自动 abort 回滚），报告里写一行
  「工作区：已自动合并到当前分支（N 个文件）」，并给出撤回命令。
  关闭方式：`--no-apply` 或 `ORCH_AUTO_APPLY=off`。
- **前台也走投递**，同一份报告被送两遍。`dispatch` 默认是**同步**的：报告就是命令的返回值，
  Codex 在那一轮里直接读得到。可它跑完仍然 `codex queue` 推一条回会话，于是会话被多唤起一轮、
  内容重复两遍；为了推这条，还要先花最多 45 秒去猜"当前是哪个窗口"。
  现在：前台不定位窗口、不投递（实测整轮从 2 分 33 秒降到 1 分 26 秒）；
  只有 serve / detach 这种"没有返回值"的模式才投递。要强制投递用 `--notify`。
- **验收在提交之后跑**，导致 `git diff` 类判据永远空。planner 天然会写
  `git diff --name-only` / `git diff --unified=0`（"只改了 X""新增 1 行"），
  而引擎提交完工作区就干净了，这些判据一律输出空——worker 改对了也判 0 通过，
  报告于是永远显示"未完成 / 不计算节省"。真跑 `README.md` 那次就是这么假失败的。
  现在：提交 → `reset --mixed` 退回工作区 → 验收 → 通过后按 worker 改动清单重新提交；
  并在验收前后比对产物文件指纹，验收命令敢改产物就直接判失败（不让它自己判自己）。

## v1.9.4 · 2026-11-10（补丁）

**加了什么**：基线脚本 `scripts/baseline.py`。

orch 侧的 token 有了（v1.9.3），但"省了多少"还缺另一半——**同一件事让 Codex 独自做，花多少**。
两边必须用同一个解析口径，否则就是拿两种秤称同一批货。

脚本做的事：
- 从目标仓库同一条提交 `git clone` 到临时目录（不拷脏工作区）；
- 任务原话一次性喂给 codex，不给计划、不给验收；
- 记录 token（输入/缓存/输出）、轮次、墙钟、改了哪些文件、验收命令的退出码；
- `--vs <orch run 目录>` 直接并排打印两侧。

两种跑法，别混：
| 参数 | 含义 |
|---|---|
| `--raw` | 用 codex 自己的默认 provider/模型——这才是"Codex 独自" |
| `--worker X`（默认） | 复用 workers.json 里 X 的模板（同模型、同一套 `-c`） |

**为什么 `--worker` 要复用模板**：光给 `-m deepseek-flash` 不够，provider 的 base_url、
wire_api、env_key 都在那几个 `-c` 里；少一个，codex 会拿默认 provider 去请求，
报的却是"模型不可用"——看着像模型的问题。

**顺带**：解析器开始记 `turns`。每个 turn 都要重发完整上下文，只报输入合计
看不出"是 prompt 大还是来回次数多"。

**第一次实测**（同一个 1 行任务、同一个模型、都 1 轮）：

| | 输入 | 未缓存输入 | 输出 | 墙钟 |
|---|---|---|---|---|
| Codex 独自 | 121,071 | 751 | 217 | 68.5s |
| orch worker | 167,941 | 2,437 | 583 | ~94s |
| 比 | 1.39× | **3.24×** | **2.69×** | 1.37× |

结论：**这个规模（1 个文件 1 行）编排是亏的**。worker 的 prompt 要带 spec、验收标准、
仓库规则，固定开销压不下去；任务再小也省不回来。这和"单点修改自己干"的既有规则一致。

**还没测的**：4 文件以上的任务。按结构推算应该翻转（执行量足够大时固定开销被摊薄），
但那需要实测，不能拿推算当结论。

## v1.9.3 · 2026-11-09（补丁）

**修的是什么**：token 用量只解析、不使用——`last_tokens` 从 v1.9 起就在算，没人消费它。
于是报告一直写"实际金额未知"，关于"编排到底省不省额度"的讨论只能靠推理。

**改了什么**：

- `codex exec` 加 `--json`：stdout 变成事件流，`turn.completed.usage` 里有
  输入 / 缓存命中 / 输出 / 推理四类 token，比原来那行 `tokens used N` 细得多。
- `planner.parse_json_events()`：取**最后一条** agent_message 当结论；
  解析失败退回原始文本（宁可多留一坨日志，也不能丢 worker 的产出）。
- 用量沿 `Provider → WorkerResult → trace(task.usage) → RunSummary.resources` 一路带到报告；
  **每次尝试都累加**，重试烧掉的也记。
- `ControlledProvider.__getattr__` 透传：包了一层之后 `model` / `last_usage` 还在内层，
  不透传就永远是空——而它看起来"成功"。
- 报告新增：`token 输入 394,052（缓存 390,016）· 输出 3,315`；拿不到就写"token 未采集"，
  不写 0（"没花钱"和"没测到"必须能分开）。

**修的第二个 bug**：`run_plan` 外层用运行控制器的计数**覆盖** `resources`，
把 `_execute_plan` 里刚累加的 token 冲掉了。现象是 trace 里有、报告里"未采集"。

**验证**：351 个测试全绿；真实任务（1 任务、2 次调用）报告输出
`token 输入 394,052（缓存 390,016）· 输出 3,315`。

**顺带的一个观察**：这个微不足道的任务，输入 token 也有 39 万——其中 99% 是缓存命中。
所以"贵不贵"不能看输入总数，要看未缓存的那部分。

**已知缺口**：只统计 **worker** 的用量。验收判定如果调了模型（判据是断言型时会请模型判），
那一次调用的 token 没计入。判据全是命令型时缺口为零——这也是"判据要能写成命令"的又一个理由。

## v1.9.2 · 2026-11-09（补丁）

**修的是什么**：`orch` 从来不是一个命令，只能 `cd` 到仓库用 `python -m orch`。

真实使用是"打开客户端，说一句话"——没人会先 cd。之前那条要求是我给的临时支架，
不是设计需要；客户端开在别的目录时，skill 里的命令会直接 `No module named orch`。

**改了什么**：

- 新增 `scripts/install_cli.py`：在 `~/bin` 写 `orch.py` + `orch.cmd`/`orch` 垫片，
  路径从仓库推、垫片纯 ASCII（中文路径不经过 cmd 代码页），装完按名字自检。
- skill 命令统一成 `orch ...`，并在开头写明"不要假设当前目录是仓库、不要 cd"。
- `docs/REPRODUCE.md` 增加"把 orch 装成命令（一次）"。

**验证**：在 `/tmp` 下 `orch --help` 可列 15 个子命令（bash 与 cmd 各验一次）。

## v1.9.1 · 2026-11-09（补丁）

**修的是什么**：`--worker-sandbox` 在默认档位上**静默失效**。

调用链是 `dispatch` 算沙箱 → `_provider()` → `build_named()` → `workers.json` 的 argv 模板。
前两步都对，第三步把参数丢了：模板里写死 `--sandbox workspace-write`，
`build()` 也没有覆盖入口。结果是 **trace 记 `sandbox: bypass`，实际发出去仍是 `workspace-write`**。

**为什么上一版不够用**：

1. 更新后的 `codex-cli 0.167.0-alpha.3.2` 在这台机器上，`workspace-write` 沙箱起不了子进程，
   报 `windows sandbox failed: registered Core launch requires an installed app`；
2. worker 因此一个文件都读不了、改不了，diff 为 0 字节，任务被判失败；
3. 失败现场看起来像"模型不行"，而日志里明明写着 bypass —— **日志替系统撒了谎**。

实测对照（同一台机器、同一个二进制）：

| 沙箱 | 结果 |
|---|---|
| `read-only` | 能起进程（planner 用它，正常） |
| `workspace-write` | 起不了子进程 |
| `--dangerously-bypass-approvals-and-sandbox` | 起进程成功（633 ms 执行完 `echo hi`） |

**改了什么**：

- `orch/providers.py`：新增 `apply_sandbox()`，按调用方要求改写 argv 里的沙箱决定；
  `bypass` 走 `--dangerously-bypass-approvals-and-sandbox`（它不是一个 `--sandbox` 取值），
  其余两种走 `--sandbox <mode>`。插入位置固定在子命令 `exec` 之后，不放在 prompt 后面。
- `build()` / `build_named()` 增加 `sandbox=` 参数；`dispatch._provider("deepseek")` 透传。
- 新增 `scripts/preflight.py`：换机器先跑它，检查 Python / git / codex 二进制 /
  **沙箱能不能起进程** / 凭据，全程不调模型。
- 测试 +6（`tests/test_providers.py::TestSandboxOverride`）。

**验证**：

- 343 个测试全绿；
- 真实端到端（`C:\orch-e2e-v19`，4 个待实现模块）：1 个任务 · 42.6 秒 · 4 个文件 ·
  模型调用 2 次 · 检查命令 2 次 · 整体验收通过 · `apply` 后工作区干净。

**没改的**：契约字段、命令面、错误码、验收标准。所以这是补丁，不是新版本设计。

## v1.9 · 2026-11-09（冻结）

首个可冻结版本：契约 / 校验 / 执行 / 验收四层闭环，15 个子命令，337 个测试，
封存为可离线复现的 `dist/orch-v1.9.bundle`。详见 `report/04-设计冻结-v1.9-2026-11-09.pdf`。
