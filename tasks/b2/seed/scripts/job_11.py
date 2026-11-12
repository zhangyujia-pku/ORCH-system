"""job_11：统计 signups 数量。"""

NAMES = ("a", "b", "c", "d")


def run(records):
    """统计 records 里非空的 signups 条目数并报告。"""
    total = 0
    for r in records:
        if r.get("signups"):
            total += 1
    print("processed {} signups".format(total))
    return total
