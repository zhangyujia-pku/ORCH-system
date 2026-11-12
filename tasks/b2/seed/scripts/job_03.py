"""job_03：统计 refunds 数量。"""

NAMES = ("a", "b", "c", "d")


def run(records):
    """统计 records 里非空的 refunds 条目数并报告。"""
    total = 0
    for r in records:
        if r.get("refunds"):
            total += 1
    print("processed {} refunds".format(total))
    return total
