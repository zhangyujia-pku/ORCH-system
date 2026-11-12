#!/usr/bin/env python3
"""
生成第三批语料：锚点数 0 / 1 / 2 的梯度实验。

为什么要用模板生成而不是手写：
    这批要操纵的是**锚点数量**，不是文采。手写 24 条无法保证"除锚点外逐字相同"，
    而逐字相同是这批实验成立的前提。模板把差异锁死在开头那一句上。

模板结构：
    [开头：0/1/2 个锚点]  +  [任务动词短语（三档完全相同）]  +  [结尾（三档完全相同）]

锚点定义与 orch/contract.py 的 _ANCHOR 一致：文件名、反引号、路径、数字、函数调用。
所以动词短语里刻意避开这些——不写 `search()`，只写"按关键词筛选"。

用法：python experiments/make_anchor_set.py
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 三档开头：锚点 0 / 1 / 2。长度差约 13 字，占全文 10% 以内。
HEAD = {
    "A0": "任务系统现在还有几个地方不完善，需要做一件事：",
    "A1": "storage.py 是任务模块，现在还需要做一件事：",
    "A2": "storage.py 和 app.py 都涉及任务数据，现在还需要做一件事：",
}

# 动词短语：三档逐字相同，且不含任何锚点（无文件名、无括号、无数字）
ACTIONS = {
    "persist":  "把任务数据存到文件里，重启之后还能重新读回来",
    "search":   "支持按关键词把符合条件的任务筛选出来",
    "stats":    "统计出任务总数和已经完成的数量",
    "export":   "把任务列表导出成一张表格文件",
    "softdel":  "删除任务的时候只做标记，原始记录仍然保留",
    "priority": "给每个任务加上优先级并按优先级排序",
    "paging":   "让任务列表支持分页，一次只返回一部分",
    "validate": "在创建任务的时候检查标题不能为空",
}

TAIL = (
    "。这块之前已经讨论过了，方向就按上面说的定下来，不要再改动别的部分，"
    "其它接口的行为保持原样不变。做完之后跟我说一下改了哪些地方，"
    "我这边再跑一遍测试确认没有问题。"
)


def build() -> dict:
    cases = []
    for task, action in ACTIONS.items():
        for level in ("A0", "A1", "A2"):
            cases.append({
                "id": f"{task}-{level}",
                "pair": task,
                "task": task,
                "level": level,
                "text": HEAD[level] + action + TAIL,
            })
    return {
        "note": "锚点梯度：0/1/2。开头三档不同（只差锚点），动词短语与结尾逐字相同。",
        "repo": "demo/tasks_app",
        "levels": {
            "A0": "零锚点：全文没有任何文件名/命令/字段名",
            "A1": "一个锚点：只点名 storage.py",
            "A2": "两个锚点：点名 storage.py 和 app.py",
        },
        "cases": cases,
    }


if __name__ == "__main__":
    out = HERE / "intents_anchor.json"
    out.write_text(json.dumps(build(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {out}（{len(build()['cases'])} 条）")
