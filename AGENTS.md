# AGENTS.md

这个仓库是一个**多 agent 编排引擎**（`orch`），不是普通的应用代码。

## 你在对话里应该做什么

当用户提出「帮我实现/修改某个仓库的某个功能」这类需要多步完成的需求时：

**不要自己拆任务、不要直接改代码。** 把需求原样交给编排器，让它去拆：

```bash
# 在 orch 仓库根目录下执行（本机路径因人而异，不要写死）
python -m orch intake "<用户原话，一字不改>" --repo <目标仓库绝对路径>
```

三个约束：

1. **原话传入。** 不要总结、不要润色、不要补全。`intake.md` 里的文本要能代表用户当时真正说的话——
   改写过的需求没法做归因，而「需求有多含糊」正是这个项目要测的自变量。
2. **不要替用户决定拆法。** 拆解是 Planner 的职责，你的职责只是把需求送进去。
3. **不要在对话里等待长任务。** 一次 run 是 5–15 分钟的后台过程，对话只负责发起和查询。

## 编译计划

`intake` 只记原话，不产出计划。要变成可执行的 `tasks.json`，再跑一步：

```bash
python -m orch plan --provider codex --timeout 240
```

它会自动带重写闭环：校验不过就把违规列表塞回去让 Codex 改，最多 3 次；仍不过则
落 `plan.failed.json` 并返回退出码 3，表示“规划未通过、尚未执行”。
旧机器标识 `fallback` / `plan.fallback` 保留用于兼容，不表示实际启动过单 agent。
第四步已增加显式配置的有限重规划与恢复，不通过旧 `fallback` 标识触发执行。没有新增单 Agent 降级。

需要 `CODEX_BIN` 指向 `codex.exe`（桌面端自带的那个在
`%LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\codex.exe`）。

## 查询进度

执行阶段必须有用户维护的固定整体验收配置（运行目录 `verification.json` 或 `--verification <路径>`）。
配置不由 Planner/worker 自动放宽；`protected` 指定的检查文件和目录不能交给 worker 修改。
产物在独立整合目录和运行分支中，原仓库当前目录不会自动切换到结果。
具体格式见 `docs/第三步_可信验收与整合.md`。

第四步的执行策略使用运行目录 `execution.json` 或 `--execution <路径>`，与任务契约分开。
支持有限返工、合格后端接替、`cancel`、`answer`、`run --resume` 和后台恢复；所有恢复延续资源计数与绝对截止时间。
不确定整合、未确认停止的子进程或配置指纹变化均拒绝自动继续，不手工改恢复点伪造成功。
完整入口的有限重规划由 `dispatch` 执行，默认关闭；不能从计划失败推断已经执行降级。
详见 `docs/第四步_恢复资源与异构闭环.md`。**工程版本已于 2026-11-09 冻结为 v1.9**（tag `v1.9`）：接口、命令面、错误码、验收标准不再变。规格见 `report/04-设计冻结-v1.9-2026-11-09.pdf`，换机器复现见 `docs/REPRODUCE.md`。要改其中任何一条，按该文档 §8 的流程走。

```bash
python -m orch status            # 最近一次 run
python -m orch status <run_id>   # 指定 run
```

## 校验计划（当用户手动给了 tasks.json）

```bash
python -m orch validate <tasks.json> --repo <目标仓库>
```

失败时把**完整输出**贴回去，包括每个 `E0xx` 码和它的 `→` 提示。不要只说「校验没通过」。

## 代码约定

- 纯标准库，不要引入第三方依赖。`orch/` 下每个模块都要能在裸 Python 3.10+ 上跑。
- 所有失败返回 `Violation` 列表，不抛异常——它们要写进 `trace.jsonl`。
- 错误码（`E013` 这类）是稳定契约，报告文字可以随便改。
- 加校验规则必须同时加一个坏样例到 `examples/bad/`，并在 `_manifest.json` 登记。
  测试要求每份样例**只**触发它自己那一个错误码。
- 报告的文字可以改，错误码（`E013`）不行——它是统计口径。

## 测试

```bash
python -m unittest discover -s tests -t . -v
```

改完任何校验逻辑都要跑一遍。失败样例少于 20 份说明覆盖不够。
