"""job_05：统计 returns 数量。"""

NAMES = ("a", "b", "c", "d")


def run(records):
    """统计 records 里非空的 returns 条目数并报告。"""
    total = 0
    for r in records:
        if r.get("returns"):
            total += 1
    print("processed {} returns".format(total))
    return total
