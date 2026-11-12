"""job_12：统计 cancels 数量。"""

NAMES = ("a", "b", "c", "d")


def run(records):
    """统计 records 里非空的 cancels 条目数并报告。"""
    total = 0
    for r in records:
        if r.get("cancels"):
            total += 1
    print("processed {} cancels".format(total))
    return total
