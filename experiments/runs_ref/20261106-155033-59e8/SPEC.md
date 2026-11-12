# 校验任务标题
## 要做
在 storage.py 的 create 中校验 title；空字符串触发 ValueError，阻止写入。
非空 title 保持现有创建行为。
## 不能改
不改 app.py、cli.py 或运行期数据文件。
## 已定
校验由 storage.py 的 create 执行。
使用 ValueError 表示空标题错误。