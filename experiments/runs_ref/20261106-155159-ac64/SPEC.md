# 校验创建任务的标题
## 要实现
在 storage.py 的 create 中校验 title；title 为空字符串时抛出 ValueError，阻止写入。
本需求是单一函数行为修改，保留为一个任务。
## 不能改
不修改 app.py、cli.py、requirements.txt 或运行期数据文件 tasks.json。
不改变非空标题的创建行为。
## 已定的技术决策
校验由 storage.py 的 create 负责，在任何持久化写入之前执行。
使用内置 ValueError；本次仅要求拒绝空字符串，不扩展空白字符处理规则。