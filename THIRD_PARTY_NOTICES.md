# 第三方声明与许可范围

## 根许可

本仓库**除下面列出的第三方部分之外**，采用
[PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0)，
全文见仓库根 `LICENSE`。

- 这是**源码公开、非商业许可**，不是 OSI 定义的「开源」许可证（限制商业用途，见
  [OSD 第 6 条](https://opensource.org/osd)）。请勿把它宣传成开源项目。
- 许可原文保持官方措辞，未添加、未改写条款。
- **版权人署名**：本仓库目前没有带来源的权利人署名记录，因此不在此编造姓名、
  邮箱或 `Required Notice:` 行。若权利人确定后需要补充，请在 `LICENSE` 之外
  另附署名文件，不要改动许可原文。
- PolyForm NC 1.0.0 对教育机构、公益组织、公共研究机构、政府机构等设有专门的
  允许用途条款，以官方原文为准。

## 第三方代码：more-itertools（MIT）

`tasks/t04/seed/`、`tasks/t05/seed/`、`tasks/t06/seed/` 下的 `more_itertools/`
及其 `LICENSE`、`docs/license.rst` 是上游 more-itertools 的副本，用于构造实验题库。

| 项 | 值 |
|---|---|
| 版权行（仓库内原文） | `Copyright (c) 2012 Erik Rose` |
| 许可证 | MIT，全文见 `tasks/t0{4,5,6}/seed/LICENSE` |
| 上游版本 / 提交 | **仓库内未记录**，不臆测 |
| 范围 | 仅上述三份 seed 中的 more-itertools 相关文件 |
| 授权 | 继续按 MIT 原文执行，**不受本仓库根 NC 许可覆盖，也不得改贴 NC 标签** |

MIT 允许商业使用；根许可的非商业限制**不适用于这些第三方文件**。

## 文档与数据

- `docs/`、`report/` 下的文字、图表与 PDF 是本项目的说明材料，随仓库一起按根许可使用；
  其中引用的实验数据按 `docs/实验日志.md` 的口径解释，不构成对模型效果或账单的承诺。
- `experiments/` 与 `tasks/` 里除第三方代码外的夹具、种子数据，按根许可使用。
- 运行目录 `runs/`、临时目录与本机绝对路径**不进版本库**（见 `.gitignore`）。
