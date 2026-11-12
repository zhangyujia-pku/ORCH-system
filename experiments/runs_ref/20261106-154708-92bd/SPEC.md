# CLI 耗时输出
## 要实现什么
为 cli.py 的 main(argv) 增加 --verbose 开关，在 list 或 add 命令执行完成后打印一次耗时。
未启用 --verbose 时保持现有输出；参数解析失败时不打印耗时。
## 不能改什么
不修改 app.py、storage.py、requirements.txt 或运行期数据文件。
## 已定的技术决策
使用 time.perf_counter 测量耗时。
沿用现有 argparse 命令分发结构。
实现与测试属于同一顺序依赖改动，合并为一个任务。