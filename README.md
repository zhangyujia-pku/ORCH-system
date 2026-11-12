# orch · 多 Agent 编排引擎

> **v1.9.4 + 2026-11-12 修复快照（源码公开，非商业使用）。** Codex 的前台批处理层：Codex 只做"理解需求"和"交代结果"，
> 干活外包给便宜算力。任务拆解写成可校验的契约，执行在独立 worktree 里，验收由命令判，不由模型判。
>
> 运行时**纯标准库，无第三方依赖**。验证是否完整：
> `python -m pytest tests -q` → 本快照实测 `391 passed, 1 skipped`（约 6 分钟，多数是真 git 操作）。
> 设计与接口规格见 `report/04-设计冻结-v1.9-2026-11-09.pdf`；换机器复现见 [`docs/REPRODUCE.md`](docs/REPRODUCE.md)。
> **换机器两步**：`python scripts/install_cli.py`（把 `orch` 装成命令，之后不用 cd 到仓库）
> → `python scripts/preflight.py`（不调模型，只验环境能不能起进程）。

## 它做什么

| 形态 | 什么时候用 | 命令 |
|---|---|---|
| 单线 | 一个交付物 | `orch dispatch "需求" --repo <目录>` |
| 并线 | 多个互不依赖的产物，各任务一 worktree | 同上，计划里 `mode: parallel` |
| 串联 | 后一步要看前一步的产物 | `mode: chain` + `inputsFrom`（缺则 E022） |

跑完之后：`orch apply <run_id>` 把产物合到当前分支，`orch undo <run_id>` 撤掉（保留证据），
`orch cleanup --stale` 回收卡死的运行。办公产物（docx / xlsx / pptx）走确定性脚本生成 + 断言，
不让模型手写二进制。

## 命令面

`intake` `status` `eta` `tools` `cleanup` `apply` `undo` `bypass` `show` `plan` `run`
`cancel` `answer` `validate` `route`（`python -m orch --help` 看全部）。

## 历史说明（M0 – M4）

> 以下是逐阶段开发记录，保留原文。
>
> 第四步更新：新增有限返工、合格候选接替、共享调用/检查/时间上限、人工澄清、取消与按层恢复。
> 通过运行目录 `execution.json` 或 `--execution <路径>` 配置；默认保持每任务单次尝试，额外重规划默认关闭。
> `run --resume`、`cancel`、`answer` 及后台恢复方式见 [第四步说明](docs/第四步_恢复资源与异构闭环.md)。
> 自动测试已验证控制流程；本机两个真实模型 CLI 的联合验收仍受路径/模块错误阻断，版本尚未冻结。语言学研究等待冻结后开始。

> 第三步更新：执行必须提供用户维护的固定整体验收配置，默认读取运行目录 `verification.json`，或用 `--verification <路径>` 指定。
> 任务验收、独立整合、固定整体验收全部通过才报告整体成功；缺少配置时阻断。
> 产物保存在报告给出的独立整合目录和分支，原目标目录的分支及未提交内容保持不变。
> 配置和概念详见 [第三步说明](docs/第三步_可信验收与整合.md)，示例为 `examples/verification.example.json`；示例要求目标基线中已有 `checks/` 检查文件。

> 2026-11-07 第一步更新：规划失败会停止并明确报告“尚未执行”，不会自动启动单 Agent。
> `fallback` 是保留的旧机器标识；新记录另含 `fallback_executed=false`。
> 执行前必须确认基线及完整分派；缺失能力、未核验或规格不符均阻断。
> `status` 区分计划就绪、执行结果与通知结果；旧成功记录若无 `final_verified` 证据，不推断已做整体验收。
> 下文 M0—M2 运行经历是历史记录；当前行为以上述说明及测试为准。

> 第二步更新：每层任务从同一版本并行执行，验收后按计划顺序整合；下一层继承此前各层已整合的全部成果。
> 起点和整合版本写入 trace；已提交与未提交修改都相对本任务起点核验。
> 当前仍不允许多个任务写同一文件，即使已声明串行依赖。详见 [第二步说明](docs/第二步_依赖成果传递.md)。

> 在**花任何 token 之前**，把坏的拆解挡回去。

这是整个多 agent 编排系统的第一层。它不调用模型、不起进程、不碰 git，
只做一件事：**检查 Planner 产出的计划是不是一个能执行的计划**。

MAST 的标注显示 41.8% 的多 agent 失败源于规格问题——而规格问题是**可以在发车前查出来的**。
这一层就是那道闸门。

---

## 快速开始

```bash
cd orchestrator

# 从一句话开始（入口层，原话存档）
python -m orch intake "帮我给这个 Flask 项目加个登录功能，做好一点" --repo .
python -m orch status

# 一份合法计划
python -m orch validate examples/tasks.good.json
#   5 tasks · 3 layers · max width 2 · t01 / t02,t03 / t04,t05
#   0 error(s), 0 warning(s)  PASS

# 一份"看起来很合理"的坏计划
python -m orch validate examples/tasks.bad.demo.json
#   7 error(s)  FAIL     ← 退出码 1

# 机器可读（喂给 trace / CI）
python -m orch validate examples/tasks.good.json --json

# 有真实仓库时，glob 写集会按实际文件展开，判断更精确
python -m orch validate plan.json --repo /path/to/repo
```

零依赖，只要有 Python 3.10+。退出码：`0` 通过，`1` 有 error。

## 跑测试

```bash
python -m unittest discover -s tests -t . -v     # 测试数量以当前输出为准
```

M0 的验收标准就写在这里：**21 份坏样例，每一份必须只触发它自己那一个错误码**。
这条约束比"能报错"严格得多——它逼着每条规则不串味，也保证你后面统计
`plan.rejected` 事件时，每个 code 的含义是干净可比的。

---

## 目录

```
orch/
  contract.py            Task / Violation / Report + 全部校验逻辑（约 500 行）
  intake.py              入口层：一句话 → runs/<id>/intent.md（原话存档 + 含糊度标记）
  cli.py                 intake / status / validate 三个子命令
schema/
  tasks.schema.json      给 Planner 当 prompt 约束、给编辑器做补全（权威仍是 contract.py）
examples/
  tasks.good.json        5 任务、3 层的合法计划
  tasks.bad.demo.json    真实风格坏计划，一次报 7 个错
  bad/                   21 份单点坏样例 + _manifest.json
tests/
  test_contract.py       坏样例精确命中、环路径、写集、判据、分层
  test_intake.py         原话字节级一致、含糊标记、run_id 唯一
  test_schema_sync.py    防止 schema 与 dataclass 漂移
skill/
  SKILL.md               给 Codex / Claude Code 的技能包（说明书，不是引擎）
AGENTS.md                仓库级约定：对话里不要自己拆，交给 intake
pyproject.toml           装成 `orch` 命令（可选）
```

---

## 契约字段

| 字段 | 必填 | 说明 |
|---|:--:|---|
| `id` | ✓ | 稳定标识（`t03`）。trace、返工、冲突全按它索引 |
| `goal` | ✓ | 一句话，动词开头，单一动作 |
| `deps` | | 前置任务 id，构成 DAG |
| `reads` | | 需要读的文件 glob，决定注入多少上下文 |
| `writes` | ✓ | **承诺要写的文件**。两个任务写集相交，发车前就被拒 |
| `acceptance` | ✓ | 验收判据，每条必须可判定 |
| `forbidden` | | 禁改路径 / 禁用依赖 |
| `context_refs` | | 必须继承的已定决策（对抗 handoff 处的预设丢失） |
| `budget` | | 美元上限，用于熔断 |

两个字段是分水岭：

- **写不出 `writes` 的任务，不该被拆出来。**
- **写不出 `acceptance` 的任务，不该被派出去。**

---

## 三个校验

### 1. 结构

字段、类型、未知字段（专抓 `write` / `acceptance_criteria` 这类笔误）、
依赖存在性、自依赖、环（三色 DFS，报完整环路径）、复合 goal
（含"并且 / 然后 / 同时 / 以及 / ；"）、路径越界（绝对路径、`..`）、
`writes` 与 `forbidden` 自相矛盾。

### 2. 写集重叠

**这是最有价值的一条。** worktree 只在物理上隔离目录，管不住两个任务改同一个文件。

判断策略是**保守**：宁可误报，不可漏报。误报的代价是 Planner 重拆一次（几分钱），
漏报的代价是一次跑到一半才发现冲突的完整运行（几块钱 + 半小时）。

- 两边都是字面量 → 直接比相等
- 通配符 vs 字面量 / 通配符 vs 通配符 → 比较**通配符之前的目录前缀**是否可兼容
  （`src/auth/**` × `src/auth/login.py` → 重叠；`src/auth/**` × `src/billing/**` → 不重叠）
- 给了 `--repo` → 先按真实文件展开，命中具体文件才算，未命中的通配符退回前缀判断

### 3. 判据可判定性

一条判据必须能落到三种形式之一，否则报 E014：

| 类型 | 例子 |
|---|---|
| 测试命令 | `pytest -q tests/test_auth.py::test_login_ok 退出码 0` |
| 命令 + 期望 | `git diff --name-only 不含 migrations/` · `POST /login 正确密码返回 200` |
| 文件断言 | `` `src/auth.py` 中存在函数 `verify_token` `` |

同时抓模糊词（正常 / 合理 / 良好 / 美观 / 流畅 / 一致…）：含模糊词又没有阈值、
路径或 `` `符号` `` 作锚点的，一律判不可判定。

这条规则会很烦人，但它逼着 Planner 把「用户能正常登录」改写成
「POST /login 正确密码返回 200 且 body 含 token」。**那 41.8% 的规格失败，就是在这里消掉的。**

---

## 错误码

| 码 | 含义 | 怎么改 |
|---|---|---|
| `E000` | JSON 语法错误 | LLM 常输出尾逗号/注释，让它重写 |
| `E001` | 文档结构非法 | 顶层必须是 `{"tasks": [...]}` |
| `E002` | id 重复 | id 是唯一索引，必须唯一 |
| `E003` | 缺必填字段 | id / goal 必须有 |
| `E004` | 未知字段 | 大概率是笔误，对照字段表 |
| `E005` | 字段类型错误 | 列表字段必须是字符串数组 |
| `E006` | goal 为空 | — |
| `E007` | goal 疑似复合 | 含并列连词，拆成两个任务 |
| `E008` | deps 指向不存在 | 检查 id 拼写 |
| `E009` | 自依赖 | 删掉 |
| `E010` | 依赖成环 | 环上删一条边或反转 |
| `E011` | writes 为空 | 至少声明要新建/修改的文件 |
| `E012` | acceptance 为空 | 说明还没想清楚交付什么 |
| `E013` | 写集重叠 | 合并为同一写入任务 / 调整文件边界后重拆；当前加依赖仍不放行同文件多任务 |
| `E014` | 判据不可判定 | 改写成命令、命令+期望、或文件断言 |
| `E015` | 路径越界 | 只允许仓库内相对路径 |
| `E016` | writes 撞 forbidden | 契约自相矛盾 |
| `E017` | budget 非法 | 必须 > 0 |
| `E018` | writes 覆盖整个仓库（`**`/`*`） | 写成具体文件或子目录 |
| `E019` | 读了别人要写的文件却没依赖对方 | **自动补依赖**，不报错；只有补了会成环才报 |
| `W001` | goal 过长 | 可能塞了多个动作 |
| `W002` | reads 未命中文件 | 路径写错，worker 会拿到空上下文 |
| `W003` | reads 覆盖整个仓库 | 只读真正需要的，否则挤爆上下文 |

---

## 设计上的两个取舍

**1. 不抛异常，返回 Violation 列表。**
因为调用方需要把整批违规写进 `trace.jsonl` 的 `plan.rejected` 事件——
「模型为什么会想出这种拆法」是一手研究材料，丢了就没了。

**2. 代码是稳定的，文字是可改的。**
报告措辞随便改，`E013` 这三个字符不能改。你做实验时统计的是 code 的分布，
不是中文描述的分布。

---

## 入口：为什么不是"把项目写成一个 skill"

需求是在 **Codex 桌面端用自然语言发起**。这件事的正确形态是三样东西分工，而不是把整个项目包成 skill：

| 东西 | 角色 | 能承载什么 |
|---|---|---|
| `SKILL.md` / `AGENTS.md` | 说明书：什么时候调用、按什么顺序、失败怎么报 | 只能做入口指引 |
| `orch` CLI | 引擎：跑循环、管进程、管状态 | 全部实际工作 |
| MCP server（M3） | 让任意 agent 用统一协议调这套引擎 | 可选的第二层外壳 |

**Skill 不提供运行时。** 一次 run 要跑 5–15 分钟、起 5 个进程、写 600 条事件，这不可能塞进一段提示词。

### 控制流放哪：桌面端只负责"提交"

桌面端本身是个 agent。如果让它当总控（自己拆、调用你的工具），就等于把架构退回到"LLM 当调度器"——不可复现、不可单测、关掉窗口就断。正确做法是 fire-and-forget：

```
桌面端说一句话
   ↓  （skill 告诉它：别自己拆，调这条命令）
orch intake "<原话>" --repo <目标仓库>
   ↓  写 runs/<id>/intent.md（一字不改）
后台：Planner 编译成 tasks.json → 校验 → 派发 → 验证 → 合并
   ↓
orch status
```

### 界面上有一条线

**对话层只产出含糊的自然语言，编排器负责把它编译成严格的结构化契约。**

这条线不能合并。合并了，你就没法把"需求有多含糊"当成自变量——而那恰好是这个项目最想回答的问题。所以 `intake` 会打五个廉价标记，原样带进 Planner：

| 码 | 含义 |
|---|---|
| `I001` | 未声明验收标准 |
| `I002` | 含模糊词（"做好一点""尽量"） |
| `I003` | 未指明改动范围（没提任何路径/文件） |
| `I004` | 多目标并列（并且/然后/同时） |
| `I005` | 需求过短，信息不足 |

这些不是错误，是**实验分组**。跑够样本之后你能回答：intent 的含糊度每加一分，Planner 的重写次数涨多少、最终成功率掉多少。

```bash
$ python -m orch intake "帮我给这个 Flask 项目加个登录功能，做好一点" --repo ~/proj
  含糊度 2/5
    I001  未声明验收标准
    I003  未指明改动范围
```

### 装到哪：全局，而不是某个仓库

入口要能对**任意仓库**下指令，所以别把 `AGENTS.md` 塞进目标项目。两种装法：

1. **用户级 skill**：把 `skill/SKILL.md` 放到 Codex / Claude Code 的用户级 skills 目录
   （具体路径随版本变化，以官方文档为准），之后任何会话都能触发。
2. **装成命令**：`python scripts/install_cli.py`，之后 `orch` 全局可用。
   当前**只支持源码目录安装**，不提供独立 wheel：`workers.json`、`config/`、`skill/`、`examples/`
   都在 `orch` 包之外，装成 wheel 会找不到这些资源。

`AGENTS.md` 留在本仓库，管的是**开发这个引擎时**的行为约定（别自己拆、加规则必须加坏样例）。

## M1：Planner（已接通）

闭环：`intent.md` → Codex 生成计划 → 静态校验 → 不过就带着**结构化违规列表**打回重写 → 仍不过则停止并报告尚未执行。

```bash
export CODEX_BIN="<你的 codex 可执行文件路径>"
python -m orch plan --provider codex --timeout 240
```

`codex exec` 的调用模板（`codex_argv()`）：

```
codex exec --skip-git-repo-check --sandbox read-only -C {cwd}
           --output-last-message {out_file} {prompt}
```

三个参数的理由：

- `--sandbox read-only`：Planner 只该读，不该改。它一旦能写，整个"先计划后执行"的分离就没了。
- `--output-last-message {out_file}`：把最终回复单独落盘。stdout 里混着 `WARNING: failed to clean up...` 这类噪音，从里面刨 JSON 是自找麻烦。
- `{prompt}` 作为**单个 argv 元素**传入，不经过 shell，所以提示词里的引号、分号不会被解释。

### 真实运行的三次迭代（这段比结论有用）

| 轮次 | 结果 | 暴露的问题 | 修法 |
|---|---|---|---|
| 1 | attempt1 挂 E014×2，attempt2 过；**但 `writes: ["**"]`** | 校验器放行了"我会写整个仓库" | 新增 `E018`：根级通配符一律拒 |
| 2 | 三次全挂 E018，降级 | 模型不知道该写哪个文件——prompt 里没有文件清单 | prompt 注入仓库文件列表（前 80 个）+ 明确禁止 `**` |
| 3 | **attempt1 一次过，45 秒** | — | — |

第 1 轮里那两个 E014 长这样，正好示范了"结构化反馈"为什么有效：

```
FAIL  「文件断言：现有 GET/POST 测试的断言未被删除、弱化或跳过。」（没有可判定的谓词）
FAIL  「文件断言：自动化测试覆盖 PATCH 与 DELETE 操作不存在任务的场景…」（有谓词但没有锚点）
```

模型看到这两条后，第二轮把它们改成了 `断言状态码等于 404`——从"形容"变成了"谓词 + 锚点"。

### 一个必须知道的结论

**这条计划最终只有 1 个任务，这是对的。** PATCH 和 DELETE 都要改 `app.py`，写集必然重叠，按单写者原则只能串行，串行就不该拆。

这也说明 `writes` 是**文件级**的粗粒度约束：同一个文件里的多个功能，在这套规则下永远是 1 个任务。想让"同文件多功能"可并行，得做到函数级写集（`app.py::create_task`），那是另一个量级的工作——**可以作为你后面的一条研究线**。

### 第二次真实需求：真的拆出了 3 个任务

把 demo 仓库拆成 `app.py` / `storage.py` / `cli.py` 三个模块后，给一句"做三件互不相关的事"，
Planner 产出了 **3 任务、1 层、宽度 3** 的完全并行计划（attempt1 挂 4 个 E014，attempt2 过，102 秒）：

| 任务 | writes | 依赖 |
|---|---|---|
| t01 实现任务数据持久化 | `storage.py`, `tests/test_app.py` | — |
| t02 新增 CLI 耗时输出选项 | `cli.py`, `tests/test_cli.py` | — |
| t03 补充数据文件说明 | `README.md` | — |

写集两两不相交，E013 通过。

**但这份计划有个真问题：t03 要写"tasks.json 的格式"，而格式是 t01 定义的，两者却没有依赖。**
它会基于自己想象的格式写文档，然后和实际实现对不上。

这不是 bug，是**机械规则的边界**：写集检查看得见"谁改了哪个文件"，看不见"谁在语义上依赖谁"。
E019 只能抓"读了别人正在写的文件"，抓不到"文档在描述别人还没实现的行为"。

所以这套系统仍然需要 verifier 和 L3 升级——**规则能保证不打架，保证不了说得对**。
这也是后面做实验时最值得测的一格：*机械校验能把规格失败率压到多低，剩下的那部分到底是什么。*

### 一个把重试从 3 次降到 0 次的改动

早期跑批里连续三次 `E019`（t03 读了 t01 要写的 `storage.py` 却没写 `deps`），规划未通过；旧记录称“降级”，不代表发生过单 Agent 执行。
复盘发现两处规则太死：

1. **锚点只认反引号**：模型写 `文件断言：storage.py 含 …`，没加反引号就被判"不可判定"，
   但人一看就知道可查。已把**裸文件名**（`*.py/.md/.json/…`）和**引号内字面量**也算作锚点。
2. **读写依赖让模型重试**：`B 读 A 写的文件 → B 必须排在 A 后面`，这是推导，不是判断。
   改成代码自动补边（`repair_dependencies`），只有补了会成环才报错，并记 `plan.repaired` 事件。

用当时失败的三份原始输出回放：

```
attempt1: FAIL  修复 2 条  ['E014','E014','E014']
attempt2: FAIL  修复 0 条  ['E014']
attempt3: PASS  修复 1 条  []        ← 原本三次全挂，现在直接过
```

剩下的 `E014` 都是模型真写得含糊（"说明…""保留…"，没有谓词），该重试还是要重试。

## 沙箱诊断（Windows）

`codex-cli 0.166.0-alpha.6` + Windows 11。已确认的故障：旧式 elevated 沙箱重新初始化时打不开
`.codex/.sandbox-bin`，报 `helper_sandbox_lock_failed`（错误 5，拒绝访问）。

实测矩阵（同一台机器，逐项隔离）：

| 配置 | 目录形态 | 工作区写 | `git status` | 越界写 |
|---|---|---|---|---|
| 默认（`[windows] sandbox = "elevated"`，**已修**） | worktree | ✅ 38s | ✅ 42s | ✅ 拦住 |
| `-c windows.sandbox="mxc"` | worktree | ❌ 写不进去 | ❌ 无物证 | ❌ 无 marker |
| 修复前（`helper_sandbox_lock_failed`） | worktree | ✅ 但慢 | ❌ **死锁** | ✅ 拦住 |

结论：

1. **根因是 `.codex/.sandbox-bin` 的锁竞争**（`helper_sandbox_lock_failed`，错误 5）。
   修好之后，默认配置下 worktree 里的 git 也不再死锁，三项全过。
2. **MXC 在这套调用下反而不可用**：工作区写被拒，连 marker 都写不出来。
   所以不要加 `CODEX_CONFIG`，默认配置就是对的。
3. 早期"worktree 死锁"的观察，是在 `.sandbox-bin` 坏掉的状态下测的——
   **故障态下的结论不能当架构结论**。这条教训值得记住。

### 判断"修好"的标准是三条，缺一不可

```bash
python scripts/sandbox_check.py --timeout 90
```

- `W` 能写工作目录内的文件
- `G` 能执行 git 命令 —— 判据是**拿到真实的 HEAD 哈希**（`rev-parse --short HEAD`）。
  这个值沙箱外也算得出来，可以用来对答案，模型编不出来。
- `E` 往工作目录外写**必须失败**

**三条都要求"有对得上的证据"**：`G` 对哈希，`E` 要求先写 marker 再越界。
因为"命令根本没启动"和"越界被拦住"在文件系统上长得一模一样——
不要求证据，就会把崩溃读成安全。

> 早期的 `G` 是"跑 git status 把首行写进文件"，实测发现它在模型不配合时会误判
> （`probe_w.txt`、`probe_e_ran.txt` 都在，唯独 `probe_g.txt` 没有）。
> 那说明它在测"模型听不听话"，不是在测"沙箱卡不卡"。换成哈希对答案后稳定。

### 换后端的姿势

一般不需要。默认配置在修复后可用。若确实要换：

```bash
CODEX_CONFIG='windows.sandbox="mxc"' python -m orch run --worker-sandbox workspace-write
```

多个覆盖用分号分隔。**换完必须重跑 `sandbox_check.py`**——本机实测 MXC 会让写入失败。

## M2 实测：3 个任务真跑完了

```bash
python -m orch run --provider codex --verifier codex --max-agents 3 --timeout 420
```

```
DONE   t02  109.2s  2 文件
DONE   t01  113.4s  2 文件
DONE   t03  115.2s  1 文件

合并到 orch/run-20261106-010547-3c07
  ok   orch/t01-a1
  ok   orch/t02-a1
  ok   orch/t03-a1

3/3 任务完成 · OK
```

- **墙钟 118 秒**，三个任务串行会是 337 秒 → 并行收益 **2.9×**
- 合并后 `pytest -q` → **10 passed**（基线 4 个，worker 自己补了 6 个测试）
- 5 个文件、127 行改动，无冲突
- 三条分支按 `t01 → t02 → t03` 顺序合并（拓扑序），无冲突

### 但那次"语义依赖"是碰巧对的

t03 写的是 `tasks.json` 的格式，而格式由 t01 决定，两者没有依赖（E019 抓不到，
因为 t03 没读 `storage.py`）。结果这次**文档和实现对上了**：

```
README：顶层为任务对象数组        storage.py：json.dump(list_all(), ...)
实际文件：[{"id": 1, "title": "测试一下", "done": false}]
```

对上是因为两边都从同一句需求里读到了"存到 tasks.json"。**需求再含糊一点就会分叉。**
这正是机械规则压不下去的那部分失败——`E013`（写冲突）和 `E019`（读写依赖）都管不了它。

## 当时还剩什么（M3 历史记录）

此节保留当时问题。当前有限返工和可执行资源上限已在第四步补齐；精确费用仍未知，语义依赖研究及实验机制等待版本冻结后再确定。下文当时关于实验就绪的判断不作为当前验收结论。

M2 的验收标准已经达成：**3 任务计划无人值守跑完，每个 diff 都落在自己声明的 writes 里。**

还缺四件，按价值排序：

1. **失败重试**：现在一个任务失败就结束，没有 attempt 2/3。补上之后才能算"返工次数"这个指标。
2. **预算熔断**：`budget` 字段还没有真正生效，只在 intake 时记下来。
3. **语义依赖**：t03 那种"文档描述别人还没实现的行为"，机械规则抓不到。
   可以让 Planner 在 `context_refs` 里显式声明，或让 verifier 在合并后做一致性检查。
4. **实验开关**：`--agents N × --verify on/off × 拓扑` 的对照组还没接进 `report.py`。

前三件都是工程量，第四件才是产出——**你现在已经有能力跑出那张"多 agent 到底划不划算"的表了。**

---

---

# 异构 worker 路由（按能力，不按强弱）

**「Codex 最强，难的给它、简单的给便宜的」这个前提是错的。**

Codex 在 `workspace-write` 沙箱下默认 `network_access = false`，web search 默认只查
OpenAI 的索引缓存（`cached`），要实时抓得显式 `--search`。所以把联网调研派给它，
不是浪费额度，是拿一个自信的错答案回来。

| | web | db | docs | net-exec | 额度 |
|---|---|---|---|---|---|
| `codex` | plugin（需 `--search`） | none | none | plugin | 1.00 |
| `cbc`（WorkBuddy CLI） | **native** | plugin（MCP） | **native** | native | 0.30 |
| `dsh` | plugin | plugin | none | native | 0.20 |
| `local` | none | none | none | none | 0.00 |

路由两步，顺序不能颠倒：**先过滤能力（硬约束）→ 再比价格（次约束）**。

```bash
python -m orch route examples/tasks.caps.json --show-ladder --show-tools
```

契约新增 `needs` 字段（`web` / `db` / `docs` / `net-exec`），由 Planner 声明
「需要什么」但**不能指定给谁**；`W004` 查虚声明、`W005` 查漏声明、`E020` 拦拼错。
失败恢复也从「梯队升档」改成「换同能力的档」——沿价格爬会把联网任务升给 Codex，越升越错。

**完整设计、理由码表、db 接线的三条路 → [`docs/capability-routing.md`](docs/capability-routing.md)**
