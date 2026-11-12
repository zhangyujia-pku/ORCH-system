"""job_05：统计 returns 数量。"""
import logging

NAMES = ("a", "b", "c", "d")

log = logging.getLogger(__name__)


def run(records):
    """统计 records 里非空的 returns 条目数并报告。"""
    total = 0
    for r in records:
        if r.get("returns"):
            total += 1
    log.info("processed {} returns".format(total))
    return total
