"""job_09：统计 tickets 数量。"""

NAMES = ("a", "b", "c", "d")


def run(records):
    """统计 records 里非空的 tickets 条目数并报告。"""
    total = 0
    for r in records:
        if r.get("tickets"):
            total += 1
    print("processed {} tickets".format(total))
    return total
