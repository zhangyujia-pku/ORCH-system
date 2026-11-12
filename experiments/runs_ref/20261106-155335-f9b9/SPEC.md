# 请求访问记录
## 要实现
在 app.py 中添加请求钩子，记录每个请求的路径和响应状态码。
## 不能改
除 app.py 外的文件，不改变现有接口行为和存储逻辑。
## 已定的技术决策
使用 Flask 的 after_request 钩子，通过应用日志记录 request.path 和 response.status_code，返回原响应。