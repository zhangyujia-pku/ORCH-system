# 四个小工具

| 模块 | 函数 | 行为 |
|---|---|---|
| src/a.py | add(x, y) | 两数相加 |
| src/b.py | slugify(s) | 小写、空格转 `-`、去首尾 `-` |
| src/c.py | truncate(s, n) | 超长截断并以 `…` 结尾（`…` 占一位） |
| src/d.py | parse_range(s) | `1-3,5` → 升序去重列表 |

每个函数要有 docstring。验收：`python checks/check_all.py` 退出码 0。
