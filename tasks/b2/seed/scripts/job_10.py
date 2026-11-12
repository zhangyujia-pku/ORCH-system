"""job_10：统计 reviews 数量。"""

NAMES = ("a", "b", "c", "d")


def run(records):
    """统计 records 里非空的 reviews 条目数并报告。"""
    total = 0
    for r in records:
        if r.get("reviews"):
            total += 1
    print("processed {} reviews".format(total))
    return total
