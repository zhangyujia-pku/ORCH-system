# 公开证据索引

本仓库是**源码与文档的公开快照**。实验的逐次原始记录（`runs/exp/*.jsonl`）体积大、
含本机绝对路径，**不进版本库**（见 `.gitignore`），保留在本机私有目录。

因此：下面每一条公开结论，都要能定位到「哪个文件、哪条命令可以复算」；
数据不在快照里的，明确说明，不用缺失数据假装跑过。

## 结论与复算

| 结论 | 复算依据 | 重算命令 |
|---|---|---|
| 强制编排 vs 便宜模型单干（B）：结构性全败 | `runs/exp/ladder.jsonl`、`h1.jsonl`、`results.jsonl`、`docs_v2.jsonl`（私有） | `python scripts/make_progress_html.py` |
| 中等规模（N=2–8）D 相对 A 在金额/时间/额度上占优 | 同上；汇总表见 `docs/实验日志.md` E29、E46、E48 | 同上 |
| 单元规模与共享上下文的单变量消融 | `runs/exp/router_p.jsonl`、`router_p2.jsonl`（私有） | `python scripts/make_router.py --verify` |
| 额度与金额双口径（含高峰/空闲价） | `config/prices.json` + `runs/exp/*.jsonl` | 见 `docs/实验日志.md` E49 |
| 成本核算（额度 / 金额）报告块 | `orch/costing.py`、`config/baseline.json` | `python scripts/make_baseline.py` |
| 形态基线的来源 | `config/baseline.json`（由历史 A 臂生成，含 n 与区间） | `python scripts/make_baseline.py` |

## 快照内确实存在的证据

- `tasks/`：题面、`seed/`、`ref/`、冻结哈希 `tasks/FREEZE*.json` —— 题库可复建、可自检。
- `docs/`：实验日志 E0–E50、方案、冻结规则、路由判据、成本核算设计。
- `report/`：设计冻结与阶段报告 PDF。
- `experiments/`：早期实验夹具（历史材料，保留版本归属，不冒充新版本结果）。

## 快照内不存在的证据（不要当成已验证）

- `runs/`、`*.log`、`.orch-runs/`：运行痕迹，含绝对路径，按 `.gitignore` 排除。
- 真实 API 账单：金额是 `config/prices.json` 的公开单价折算，**不是实付账单**。
- 订阅额度的真实扣费单位：用的是 H 侧输入 token 作**代理量**。
- 模型端到端：某些环境下的执行器初始化曾被拒绝访问（见交接文档 E01），
  未在本快照内重新验证所有真实模型调用。
- `docs/实验日志.md` 里引用的 `runs/exp/...` 路径，指本机私有目录；
  这不表示数据已被删掉，也不表示它们存在于公开仓库里。

## 整理原则

保留私有原始资料与历史，不通过删除失败记录来改善结果；公开快照只做**精选**，
缺失的数据就收窄结论。新提交删除文件不会清除旧提交里的内容，若将来要彻底脱敏，
需要专门处理历史，并保留一份私有备份。


## 本快照的脱敏规则（2026-11-12）

- 本机绝对路径中的用户目录替换为 `<HOME>`，仓库根替换为 `<REPO>`，
  工作区替换为 `<WORKSPACE>`；代码里用于生成 `.cmd` 的路径改用 `%USERPROFILE%`。
- 作者署名统一为 `changkong34@qq.com`（含 5 份 PDF 的文本层，已逐页目视复核）。
- 运行目录 `runs/` 整体移出发布快照，原始记录与完整历史保留在私有备份与开发仓库中，
  **没有删除任何实验数据**。
- git 历史重建为单次快照提交，作者信息同步更新；脱敏前的 bundle 与工作区另有私有备份。
