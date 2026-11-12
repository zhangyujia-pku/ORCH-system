"""job_10：统计 reviews 数量。"""
import logging

NAMES = ("a", "b", "c", "d")

log = logging.getLogger(__name__)


def run(records):
    """统计 records 里非空的 reviews 条目数并报告。"""
    total = 0
    for r in records:
        if r.get("reviews"):
            total += 1
    log.info("processed {} reviews".format(total))
    return total
