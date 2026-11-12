# 任务统计
## 要实现
在 storage.py 新增 stats()，统计当前任务总数和 done 为真的任务数。
让 app.py 直接返回 stats() 的结果。
该改动存在顺序依赖，作为一个任务交付。
## 不能改
不修改 cli.py、任务存储格式或运行期数据文件 tasks.json。
不改变已有任务增删改查行为。
## 已定的技术决策
沿用现有任务读取方式。
stats() 返回字典，total 表示任务总数，completed 表示已完成数。
空集合返回 {"total": 0, "completed": 0}。
统计逻辑集中在 storage.py，app.py 不重复计算。