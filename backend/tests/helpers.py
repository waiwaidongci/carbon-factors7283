"""测试辅助：临时 sqlite 文件存储 + 冻结行构造（仅标准库）。"""
import os
import sqlite3
import tempfile
from decimal import Decimal

from src.reconciliation.engine import FrozenLine
from src.reconciliation.store import SqlStore

ACTIVITIES = [
    ('电力', 'kWh', 'kWh', Decimal('0.57'), Decimal('0.63')),
    ('柴油', 'L', 'L', Decimal('2.68'), Decimal('2.68')),
    ('通勤', 'km', 'km', Decimal('0.12'), Decimal('0.15')),
]
DEPTS = ['材料试验室', '工艺试验室', '光学试验室']


def make_store(tmpdir: str) -> SqlStore:
    path = os.path.join(tmpdir, 'recon.db')

    def connect():
        conn = sqlite3.connect(path, timeout=30)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('PRAGMA temp_store=MEMORY')
        conn.execute('PRAGMA cache_size=-20000')
        return conn

    return SqlStore(connect, dialect='sqlite')


def make_line(no: int, side: str, *, activity_idx=None, dept=None,
              quantity=None, factor=None, unit=None) -> FrozenLine:
    if activity_idx is None:
        activity_idx = no % 3
    activity, source_unit, factor_unit, f1, f2 = ACTIVITIES[activity_idx]
    if factor is None:
        factor = f1 if side == 'base' else f2
    if unit is None:
        unit = factor_unit
    if quantity is None:
        quantity = Decimal(no % 97 + 1)
    if dept is None:
        dept = DEPTS[no % 3]
    return FrozenLine(
        line_no=no, activity=activity, unit=unit, source_unit=source_unit,
        quantity=quantity, factor=factor, emission=quantity * factor,
        department=dept,
    )


def staged_job(store: SqlStore, base_lines, current_lines,
               base_total=None, current_total=None, batch_size=1000) -> int:
    if base_total is None:
        base_total = str(sum((x.emission for x in base_lines), Decimal(0)))
    if current_total is None:
        current_total = str(sum((x.emission for x in current_lines), Decimal(0)))
    job_id = store.create_job(
        base_run_id=1, current_run_id=2, batch_size=batch_size,
        base_checksum='b' * 64, current_checksum='c' * 64,
        created_by='tester', original_base_total=str(base_total),
        original_current_total=str(current_total))
    store.stage_lines(job_id, 'base', base_lines)
    store.stage_lines(job_id, 'current', current_lines)
    return job_id


def new_tmpdir() -> str:
    return tempfile.mkdtemp(prefix='recon-test-')
