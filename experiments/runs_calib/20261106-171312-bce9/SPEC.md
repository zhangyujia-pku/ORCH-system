# 任务搜索
## 要实现
在 storage.py 添加 search(keyword)，返回标题含关键词的任务。
在 app.py 添加 GET /tasks/search，通过查询参数 keyword 搜索任务。
## 不能改
不修改 cli.py、requirements.txt、.gitignore 或运行期数据文件 tasks.json。
## 已定技术决策
沿用现有存储方式与任务列表响应结构。
存储搜索与接口存在顺序依赖，合并为一个任务。