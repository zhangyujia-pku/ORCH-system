# CLI 耗时开关
## 要实现
为 cli.py 的 main(argv) 增加全局 --verbose 开关，支持 --verbose list 和 --verbose add。
启用时，每次子命令执行完成后向 stderr 打印一行耗时；未启用时不打印耗时。
保留 list、add 的现有参数、正常输出与返回行为。
## 不能改
不修改 app.py、storage.py、requirements.txt 或运行期数据文件。
## 已定技术决策
使用标准库 time.perf_counter() 测量子命令执行耗时。
耗时格式为 Elapsed: <非负秒数>s，保留六位小数。
参数解析失败不打印耗时。
该需求涉及同一入口的开关与执行流程，作为一个任务交付。