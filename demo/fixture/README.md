# tasks_app

一个极小的任务清单服务，用 Flask 写，数据存在内存里。

## 接口

- `GET /tasks` 列出全部任务
- `POST /tasks` 新建任务，body 为 `{"title": "..."}`

## 测试

```
pytest -q
```
