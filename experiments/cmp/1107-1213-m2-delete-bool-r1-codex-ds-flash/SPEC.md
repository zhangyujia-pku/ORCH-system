# m2-delete-bool

让 storage.delete 返回 True/False（不存在的 id 返回 False），并在 cli.py 增加 rm <id> 子命令：删除成功打印“已删除”，失败打印“不存在”。
