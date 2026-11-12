# 任务搜索
## 要实现
在 storage.py 中新增 search(keyword)，返回匹配关键词的任务对象列表。
app.py 的搜索入口调用 storage.search(keyword)，仅将返回值序列化为 JSON，不再自行筛选或重组任务。
该改动存在顺序依赖，作为一个任务交付。
## 不能改
不修改 cli.py、requirements.txt、tasks.json 或缓存文件。
不改变现有任务对象结构和其他接口行为。
## 已定的技术决策
沿用现有存储方式、应用框架和关键词匹配约定。
搜索逻辑归 storage.py，JSON 序列化归 app.py。
使用现有 tests/test_app.py 补充验证。