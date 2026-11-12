# 时长解析

`parse_duration("1h30m")` 应该返回秒数（5400）。

现在只解析了分和秒，漏掉了小时——修好它，并保证已有行为不变。

验收：`python checks/check_duration.py` 退出码 0。
