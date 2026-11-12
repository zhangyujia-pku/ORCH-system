# 统计小工具

实现 `stats.py` 里的三个函数：

| 函数 | 行为 |
|---|---|
| `mean(xs)` | 算术平均；空列表返回 `None` |
| `median(xs)` | 中位数；偶数个取中间两个的平均；空列表返回 `None` |
| `mode(xs)` | 出现次数最多的值；并列时取**较小**的值；空列表返回 `None` |

每个函数要有 docstring。验收：`python checks/check_stats.py` 退出码 0。
