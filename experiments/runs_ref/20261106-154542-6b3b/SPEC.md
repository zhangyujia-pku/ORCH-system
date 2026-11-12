# 任务优先级排序
## 要实现
让 storage.py 的 create 接受 priority 参数，让 list_all 按 priority 降序返回任务。
补充创建优先级、降序排序、兼容旧任务的测试。
本需求属于同一存储行为改动，合为一个任务。
## 不能改
不修改 app.py、cli.py、requirements.txt 或运行期数据文件 tasks.json。
## 已定的技术决策
沿用现有存储方式和公开接口。
priority 默认值为 0，并随任务保存。
旧任务缺少 priority 时按 0 排序。
相同 priority 的任务保持插入顺序。