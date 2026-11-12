"""命令行入口：python cli.py list | add <title> | rm <id>"""

import sys

import storage


def main(argv):
    if not argv:
        print("用法: cli.py list | add <title> | rm <id>")
        return 2
    cmd = argv[0]
    if cmd == "list":
        for t in storage.list_all():
            print(f"[{t['id']}] {t['title']} {'✓' if t['done'] else ''}")
        return 0
    if cmd == "add":
        if len(argv) < 2:
            print("缺少 title")
            return 2
        print(storage.create(argv[1]))
        return 0
    if cmd == "rm":
        if len(argv) < 2:
            print("缺少 id")
            return 2
        try:
            task_id = int(argv[1])
        except ValueError:
            print("不存在")
            return 0
        if storage.delete(task_id):
            print("已删除")
        else:
            print("不存在")
        return 0
    print(f"未知命令: {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
