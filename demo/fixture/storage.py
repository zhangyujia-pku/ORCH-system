"""内存任务存储。所有读写都走这里，方便以后换持久化。"""

TASKS = {}
_next_id = 1


def reset():
    global _next_id
    TASKS.clear()
    _next_id = 1


def list_all():
    return list(TASKS.values())


def get(task_id):
    return TASKS.get(task_id)


def create(title):
    global _next_id
    task = {"id": _next_id, "title": title, "done": False}
    TASKS[_next_id] = task
    _next_id += 1
    return task


def delete(task_id):
    if task_id in TASKS:
        del TASKS[task_id]
        return True
    return False
