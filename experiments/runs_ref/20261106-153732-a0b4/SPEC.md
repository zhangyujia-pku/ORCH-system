# 导出任务 CSV
## 要做
在 app.py 增加 GET /tasks/export，通过 storage.py 的现有读取接口获取任务列表，输出任务 ID、标题、完成状态三列 CSV。
包含表头，数据行保留存储接口返回的任务顺序；空列表仅输出表头。
正确处理标题中的中文、逗号、双引号和换行。
## 不能改
不修改 storage.py、cli.py、requirements.txt、README.md 或现有路由行为。
不直接读取或写入 tasks.json。
## 已定
HTTP 路由与响应组装保留在 app.py。
使用 Python 标准库 csv 生成 CSV，响应使用 UTF-8 编码和 text/csv 类型。
沿用现有任务结构中的 ID、标题、完成状态字段名作为表头。
该改动属于单一路由功能，不拆分任务。