# 任务搜索
## 要实现
在 storage.py 中新增 search(keyword)，负责搜索任务并返回匹配结果。
app.py 调用 storage.search(keyword)，仅将其返回值转换为 JSON，保持结果内容和顺序。
补充搜索与接口委托测试。
## 不能改
不修改 cli.py、requirements.txt 或任务持久化格式。
不提交 tasks.json、缓存等运行期数据。
## 已定的技术决策
沿用现有任务结构、存储方式和 JSON 响应机制。
搜索逻辑集中在 storage.py，app.py 不重复筛选或重组结果。
本需求存在实现与验证的顺序依赖，作为一个任务交付。