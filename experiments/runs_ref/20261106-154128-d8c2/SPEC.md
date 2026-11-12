# 任务软删除
## 要实现
将 storage.py 的 delete 改为软删除：保留记录并设置 deleted 标记，默认列表排除已删除记录。
兼容缺少 deleted 字段的现有记录，将其视为未删除。
## 不能改
不修改 app.py、cli.py、现有测试及运行期数据文件 tasks.json。
## 已定的技术决策
使用 deleted 布尔字段标记删除状态。
保留现有公开函数签名。
本次改动集中在 storage.py，不拆分任务。