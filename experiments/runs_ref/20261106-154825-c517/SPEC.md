# GET /tasks 切片分页
## 要实现
为 app.py 的 GET /tasks 增加 limit、offset 查询参数，对任务列表执行切片。
补充分页测试。
## 不能改
不修改 cli.py、storage.py、持久化格式或其他接口。
不新增分页元数据，不改变现有响应结构或任务顺序。
## 已定的技术决策
沿用现有框架与测试工具，不新增依赖。
offset 默认 0；未传 limit 时返回 offset 之后的全部任务。
limit、offset 必须是非负整数，非法值返回 HTTP 400；limit=0 返回空列表。
分页在 app.py 中完成，不下推至存储层。
实现与测试存在依赖，作为一个任务交付。