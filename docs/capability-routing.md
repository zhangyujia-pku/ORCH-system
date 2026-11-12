# 异构 worker 路由：按能力分，不按强弱分

> 这份文档替代了 README 里原先那版「便宜档 → 贵档」的梯队描述。那一版是错的。

## 一个必须先纠正的前提

「Codex 最强，所以难的给它、简单的给便宜的」——这句话在**纯代码**上成立，
在**联网调研**上是反的。

Codex CLI 的 `workspace-write` 沙箱默认 `network_access = false`，
web search 默认是 `cached`——查的是 OpenAI 维护的索引缓存，不是实时抓页面。
要实时得显式 `--search` 或 `web_search = "live"`，MCP 还得人工批准。

所以把「查一下某库最新 API」派给 Codex，**不是省不省钱的问题，是它会拿着缓存索引
编一个语法完全正常、事实全错的答案回来**。这比超时更贵——你要花时间才能发现它错了。

反过来，WorkBuddy 的 `WebFetch` 是真抓正文，DSH 有正式的数据库插件
（SQLite / PostgreSQL / MySQL，只读查询 + 写审批 + JSONL 审计）。这两样 Codex 都没有。

**结论：先过滤能力（硬约束），再比价格（次约束）。**
顺序颠倒就会写出「便宜优先、能力将就」的路由，那是最贵的一种。

## 三家实测能力矩阵

| | web（实时抓正文） | db（直连数据库） | docs（企业文档平台） | net-exec（命令出网） | 额度 |
|---|---|---|---|---|---|
| `codex` | plugin（默认 cached，需 `--search`） | **none** | **none** | plugin（需 `network_access = true`） | 1.00 |
| `cbc`（WorkBuddy CLI） | **native** | plugin（`--mcp-config`） | **native**（自带连接器） | native | 0.30 |
| `dsh`（DeepSeek Harness） | plugin（`dsh-web-search` / `dsh-tavily`） | plugin（`db-connector` / `dsh-data-agent`） | none | native | 0.20 |
| `local`（ollama） | none | none | none | none | 0.00 |

三种等级，含义写死在代码里：

- `native` —— 开箱能用
- `plugin` —— 要装插件或配 key，**没配好之前按 `none` 算**
- `none` —— 不具备

`plugin` 默认不算可用，是这一层最重要的一条纪律。注册表里写「它应该支持」、
执行时才发现没装，是这类系统最常见的翻车方式。要启用就把 `available` 里对应项
置 `true`——这是一个**明确的、可审计的动作**，不是靠着默认值蒙过去。

## 契约里加了 `needs`

能力必须变成契约字段，否则路由只能靠猜。所以 `tasks.json` 多了一个可选字段：

```json
{
  "id": "t01",
  "goal": "查 DeepSeek Harness 的 db-connector 当前支持哪些数据库",
  "writes": ["docs/dsh-db-connector.md"],
  "acceptance": ["文件 docs/dsh-db-connector.md 含 `sqlite`"],
  "needs": ["web"]
}
```

值域只有四个：`web` / `db` / `docs` / `net-exec`。写错的能力名报 `E020` 而不是忽略——
**拼错会被当成「不需要」，静默失效比报错危险得多**。

### 权力分离：Planner 声明需求，代码决定给谁

`needs` 允许 Planner 说「这个任务需要联网」，**不允许它说「给 cbc」**。

声明需求是事实判断，分配 worker 是花钱决策。Planner 一旦能挑 worker，它会全挑贵的——
这是它的训练目标决定的，不是它使坏。所以路由权必须留在代码里。

### 双向校验：`W004` 与 `W005`

只声明不检查，`needs` 会变成两种垃圾数据：

- `W004` 虚声明 —— 声明了 `web`，但 goal / acceptance / context_refs 里没有一处联网线索。
  后果是白占一个能力约束，把任务挤到有能力的那档，或者被用来绕过路由。
- `W005` 漏声明 —— 任务里有 URL、`pip install`、`sqlite` 这类明确信号却没声明。
  后果是发车后才发现跑不通，而那时候钱已经花了。

两个都只 warn，不阻断。理由是：`needs` 是提示不是证明，误报的代价是看一行字。

**这条规则的生命线是误报率。** 所以 `db` 的线索被刻意收窄了：
单独出现「数据库」三个字不算——「查…支持哪些数据库」是在描述**对象**，
不是「我要连它」。这种句子误报一次，用户就会开始无视所有 needs 警告，
那整套校验就废了。

## 两条规则，按顺序

**第一步：能力过滤（硬约束）** —— 不满足就出局，跟价格无关。

| 码 | 含义 |
|---|---|
| `C01` | 需要实时联网 |
| `C02` | 需要数据库 |
| `C03` | 需要企业文档平台 |
| `C04` | 需要命令出网 |
| `C05` | 多项能力，取交集 |
| `C09` | **能力缺口**：整个注册表都没有，计划必须改 |
| `C10` | 有能力，但没有 argv 已核验的档 |

**第二步：成本排序（次约束）** —— 只在剩下的人里比谁便宜。

| 码 | 含义 |
|---|---|
| `R01` | 文档类，写集全是文本且 ≤ 2 个 |
| `R02` | 机械类，写集 ≤ 3 且判据全是命令 |
| `R03` | 存在非命令型判据 |
| `R04` | 写集超出候选档上限 |
| `R05` | 兜底 |

`R` 码只反映**真正起作用的那个约束**。一个因为写集太大才落到 codex 的任务，
理由报 `R04` 而不是 `R03`——否则统计时会把「规格不够」和「判据复杂」混成一件事。

## 未核验的 harness 不进估算

`argv_verified: false` 的档默认不参与路由，命中的任务报 `C10`。

理由很直接：估算里混进一个可能根本起不来的档，省下来的额度是纸面上的。
现在 `dsh` 和 `local` 都是未核验状态（本机没装），所以默认只有 `cbc` 和 `codex` 在跑。
想看含未核验档的估算，加 `--allow-unverified`。

## 失败恢复：换人，不升档

这一条也跟着改了。原来是「失败 → 梯队下一档」，方向是**沿价格往上爬**——
于是联网任务失败会被升给 Codex，越升越错。

现在是 `alternatives(name, needs)`：**换一个具备同样能力的 worker**，按成本升序。
Codex 不在联网任务的备选里，这不是降级，是它本来就没有这个能力。

假设也跟着变了：升档假设「贵的就是更强的」；换能力假设「可能是这家 harness 的问题，
换一家同样能做的」。后者才对应真实的失败原因。

## 工具裁剪：给文档任务开 Bash，等于给不需要的人配钥匙

`cbc` 支持 `--tools`，所以路由会顺带算出每个任务该裁到哪几把工具：

```
t01  cbc  Edit Glob Grep Read WebFetch WebSearch Write   ← 需要联网，保留 WebFetch
t02  cbc  Bash Edit Glob Grep Read Write                ← 改代码，保留 Bash
t03  cbc  Edit Glob Grep Read Write                     ← 纯文档，Bash 被裁掉
t04  codex  不支持裁剪
```

能力表和工具表是同一张（`TOOLS_BY_NEED`），所以「声明需要什么」和「给它开什么」
不会各说一套。裁剪不支持的 harness 返回 `None`，不假装能管。

顺带一个副产品：工具 schema 本身也占 token，裁掉用不上的那几把，上下文也小一圈。

## 试算

```bash
python -m orch route                                # 最近一次 run
python -m orch route examples/tasks.caps.json --show-ladder --show-tools
python -m orch route --json                         # 喂给统计脚本
```

在 `examples/tasks.caps.json`（5 个任务）上的真实输出：

```
  任务        文件  所需能力            worker        份额  理由
  --------------------------------------------------
  t01        1  web             cbc         0.30  C01
           └─ 需要实时联网：只有 native 联网档能拿回真实页面（web=native）
  t02        1  —               cbc         0.30  R02
  t03        1  —               cbc         0.30  R01
  t04        5  —               codex       1.00  R04
           └─ 写集 ×5，只有 codex 扛得住
  t05        1  db              GAP            —  C09
           └─ 没有任何 worker 具备 db——改计划或先给某个档接线

  额度估算（codex = 1.00）：基线 5.00 → 本计划 1.90　省 62.0%
  分布：cbc×3　codex×1
  ⚠ 不可路由：t05——这几条现在算不出账，必须先解决能力缺口
```

注意 62% 不是 80%。**能力路由省得比「无脑便宜优先」少，这才是它可信的地方**——
那 20% 差价买的是「联网任务不会被派给读缓存索引的档」。

`C09` 那行的意义在于：它在你花钱之前就告诉你「这个任务现在没人能做」。
把 `t05` 强行塞给 codex，它会给你一份用 `sqlite3` 假装查过、数字全是编的报表。

## trace 里记了什么

`orch run` 会在 `run.start` 之前写一批 `route.decided` 事件：

```json
{"event":"route.decided","session":1,"data":{
  "task_id":"t01","worker":"cbc","reason":"C01","needs":["web"],
  "gap":[],"alternatives":[],"routable":true}}
```

**现在只记录，不分派**——执行仍走 `--provider` 指定的那一家。先记录的理由是：
这些决策原本只在终端上闪一次，不落地就没法回答「哪些任务因为能力被分给了谁」
「缺口到底卡住了几条」，而这两个数是接线时排序的唯一依据。

`alternatives` 也一并记下，等于把「失败时该换谁」在发车前就定好了。

## 现在缺的是 db，怎么接

三家都能接，工程量差很多：

1. **DSH + `db-connector`（最省事）** —— 装插件、填连接串即可。但它同时要求
   dsh 的 argv 先核验（`npx @deepseek-ai/dsh run --headless` 在本机还没跑通过）。
2. **cbc + `--mcp-config` 挂一个 sqlite/postgres MCP（最快见效）** ——
   `cbc` 是唯一已经能跑的档，挂上 MCP 再置 `available.db = true` 就通了。
3. **自己写 100 行只读查询外壳** —— 最可控，也最符合「越界即废」的哲学：
   只允许 `SELECT`，输出落在 `writes` 指定路径，diff 白名单照旧兜底。

**别做的第四种：让 worker 直接 `sqlite3 xxx.db "..."` 然后自己誊数字。**
查询结果没有 schema 约束，模型抄错一位数字你根本看不出来——而这类报表恰恰是
要拿去下结论的。让 db 能力走结构化工具，不走自由文本。

## 三个坑

**1. 验收器不能跟着降级。** worker 用 4B 模型、verifier 也用 4B 模型，「便宜」就变成
「假通过」。现在的判据大部分是 `python -m unittest ... 退出码 0` 这类命令型，
**零 token 且可信任**——这才是省额度真正的杠杆：**判据越机械，越敢用便宜 worker。**

**2. 三家的 sandbox 语义不一样，不要试图统一。** Codex 是 `--sandbox workspace-write`，
`cbc` 是 `--permission-mode acceptEdits`，dsh 走 Cordis 插件。统一到 `out_of_scope()`
的 diff 白名单上——不依赖各家的沙箱实现，用产出兜底。

**3. `net-exec` 和 `web` 是两回事。** `web` 是「读网页正文」，`net-exec` 是「让命令本身
能出网」。Codex 在 `workspace-write` 下两个都关，但它们是两个开关，得分开声明。

## 一个顺手修掉的旧 bug

`python -m unittest xxx` 之前不被认作命令型判据（正则只匹配 `python -m pytest`），
于是所有用 unittest 的任务都被迫带一条「语义判据」，全被推到最贵的档上。
现在 `python3? -m \S+` 都认。这个 bug 不影响正确性，但会让路由结论系统性偏保守。
