"""归因任务执行器：归并游标分批、断点续跑、epoch fence。

内存约束：任意时刻只持有"一批配对 + 一份累加器"，累加器仅含
(部门,维度) 个 Decimal，与底稿行数无关，百万行额外内存远低于 64MiB。
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Optional

from src.reconciliation import engine
from src.reconciliation.engine import (
    Accumulator, PairData, RejectError,
)
from src.reconciliation.store import FencedError, SqlStore


def _row_to_line(row: tuple) -> engine.FrozenLine:
    line_no, activity, unit, source_unit, quantity, factor, emission, department = row
    return engine.FrozenLine(
        line_no=int(line_no), activity=activity, unit=unit,
        source_unit=source_unit, quantity=Decimal(quantity),
        factor=Decimal(factor), emission=Decimal(emission),
        department=department,
    )


def _load_accumulator(job: dict) -> Accumulator:
    acc = Accumulator()
    acc.numerators = {tuple(k.split('\\u0001')): Decimal(v)
                      for k, v in job['numerators'].items()}
    acc.total_base = Decimal(job['total_base'])
    acc.total_current = Decimal(job['total_current'])
    acc.missing_base = int(job['missing_base'])
    acc.missing_current = int(job['missing_current'])
    acc.pair_count = int(job['cursor_no']) - 1
    return acc


def _dump_numerators(numerators: dict) -> dict:
    return {'\\u0001'.join(key): str(value) for key, value in sorted(numerators.items())}


class ReconcileRunner:
    def __init__(self, store: SqlStore, lease_seconds: float = 30.0,
                 heartbeat_every: float = 10.0, clock=time.time):
        self.store = store
        self.lease_seconds = lease_seconds
        self.heartbeat_every = heartbeat_every
        self.clock = clock

    # ---------- 编号校验：双游标流式归并，O(1) 内存 ----------
    def _validate_numbering(self, job_id: int) -> None:
        """两侧各自严格递增（无重号/乱序、非空），并集须为从 1 起的连续编号。"""
        conn_a, cur_a = self.store.iter_lines(job_id, 'base')
        conn_b, cur_b = self.store.iter_lines(job_id, 'current')
        count_a = count_b = 0
        try:
            ra = cur_a.fetchone()
            rb = cur_b.fetchone()
            if ra is None:
                raise RejectError(engine.REJECT_MISSING_NUMBER,
                                  'base侧没有任何底稿行', {'side': 'base'})
            if rb is None:
                raise RejectError(engine.REJECT_MISSING_NUMBER,
                                  'current侧没有任何底稿行', {'side': 'current'})
            prev_a = prev_b = 0
            expected = 1
            while ra is not None or rb is not None:
                na = ra[0] if ra else None
                nb = rb[0] if rb else None
                if rb is None or (ra is not None and na < nb):
                    if na <= prev_a:  # 仅在该侧行被消费时校验严格递增
                        raise RejectError(
                            engine.REJECT_DUPLICATE_NUMBER,
                            f'base侧存在重号或乱序：{na}',
                            {'side': 'base', 'line_no': na})
                    prev_a, merged = na, na
                    ra = cur_a.fetchone()
                    count_a += 1
                elif ra is None or nb < na:
                    if nb <= prev_b:
                        raise RejectError(
                            engine.REJECT_DUPLICATE_NUMBER,
                            f'current侧存在重号或乱序：{nb}',
                            {'side': 'current', 'line_no': nb})
                    prev_b, merged = nb, nb
                    rb = cur_b.fetchone()
                    count_b += 1
                else:  # 两侧同号，并集只取一次
                    if na <= prev_a:
                        raise RejectError(
                            engine.REJECT_DUPLICATE_NUMBER,
                            f'base侧存在重号或乱序：{na}',
                            {'side': 'base', 'line_no': na})
                    if nb <= prev_b:
                        raise RejectError(
                            engine.REJECT_DUPLICATE_NUMBER,
                            f'current侧存在重号或乱序：{nb}',
                            {'side': 'current', 'line_no': nb})
                    prev_a, prev_b, merged = na, nb, na
                    ra = cur_a.fetchone()
                    rb = cur_b.fetchone()
                    count_a += 1
                    count_b += 1
                if merged != expected:
                    raise RejectError(
                        engine.REJECT_MISSING_NUMBER,
                        f'两期编号并集不连续，缺号：{expected}',
                        {'missing': [expected], 'actual': merged})
                expected += 1
        finally:
            conn_a.close()
            conn_b.close()

    # ---------- 双游标归并产出配对（流式，支持续跑下界，不整体载入） ----------
    def _iter_pairs(self, job_id: int, min_line_no: int = 1):
        conn_a, cur_a = self.store.iter_lines(job_id, 'base', min_line_no)
        conn_b, cur_b = self.store.iter_lines(job_id, 'current', min_line_no)
        try:
            ra = cur_a.fetchone()
            rb = cur_b.fetchone()
            while ra is not None or rb is not None:
                na = ra[0] if ra else None
                nb = rb[0] if rb else None
                if ra is not None and (rb is None or na < nb):
                    line = _row_to_line(ra)
                    yield PairData(
                        line_no=line.line_no, activity=line.activity, unit=line.unit,
                        q1=line.quantity, f1=line.factor, q2=Decimal(0), f2=line.factor,
                        dept1=line.department, dept2=line.department,
                        missing_side='current')
                    ra = cur_a.fetchone()
                elif rb is not None and (ra is None or nb < na):
                    line = _row_to_line(rb)
                    yield PairData(
                        line_no=line.line_no, activity=line.activity, unit=line.unit,
                        q1=Decimal(0), f1=line.factor, q2=line.quantity, f2=line.factor,
                        dept1=line.department, dept2=line.department,
                        missing_side='base')
                    rb = cur_b.fetchone()
                else:
                    a = _row_to_line(ra)
                    b = _row_to_line(rb)
                    if a.unit != b.unit or a.activity != b.activity:
                        raise RejectError(
                            engine.REJECT_UNIT_INCOMPARABLE,
                            f'编号 {na} 两期单位/活动不可比：'
                            f'{a.activity}({a.unit}) vs {b.activity}({b.unit})',
                            {'line_no': na,
                             'base': f'{a.activity}:{a.unit}',
                             'current': f'{b.activity}:{b.unit}'})
                    yield PairData(
                        line_no=a.line_no, activity=a.activity, unit=a.unit,
                        q1=a.quantity, f1=a.factor, q2=b.quantity, f2=b.factor,
                        dept1=a.department, dept2=b.department, missing_side=None)
                    ra = cur_a.fetchone()
                    rb = cur_b.fetchone()
        finally:
            conn_a.close()
            conn_b.close()

    def run_once(self, worker_id: str, now: Optional[float] = None) -> Optional[int]:
        """认领并完整推进一个任务（可能多批）。返回处理的 job_id，无任务返回 None。"""
        now = self.clock() if now is None else now
        claim = self.store.claim(worker_id, self.lease_seconds, now=now)
        if not claim:
            return None
        job_id, epoch = claim['job_id'], claim['epoch']
        try:
            job = self.store.get_job(job_id)
            # 冻结行不可变：仅首次认领（游标在起点）做全量编号校验，续跑直接跳过
            if int(job['cursor_no']) == 1 and int(job['attempt_no']) == claim['attempt_no']:
                self._validate_numbering(job_id)

            acc = _load_accumulator(job)
            cursor = int(job['cursor_no'])
            batch_size = int(job['batch_size'])
            last_beat = now
            in_batch = 0
            # 续跑下界直接下推到 SQL，不重扫已计入编号（重试不重复计入）
            for pair in self._iter_pairs(job_id, min_line_no=cursor):
                acc.add_pair(pair)
                in_batch += 1
                cur_time = self.clock()
                if cur_time - last_beat >= self.heartbeat_every:
                    self.store.heartbeat(job_id, epoch, self.lease_seconds, now=cur_time)
                    last_beat = cur_time
                if in_batch >= batch_size:
                    self._flush(job_id, epoch, acc, pair.line_no + 1, now=cur_time)
                    in_batch = 0
            self._flush(job_id, epoch, acc, acc.pair_count + 1, now=self.clock())

            result = engine.finalize(
                acc,
                original_base=Decimal(str(job['original_base_total'])),
                original_current=Decimal(str(job['original_current_total'])))
            if not result['tie_out_ok']:
                raise RejectError(
                    'tie_out_failed', '贡献加总与原报告差额不一致，拒绝发布',
                    {'components_total': result['components_total'],
                     'report_delta': result['report_delta']})
            self.store.publish(job_id, epoch, result,
                               job['frozen_basis'] or self._basis(job),
                               acc.pair_count, now=now)
            return job_id
        except FencedError:
            # 已被更新的执行者接管：安静退出，绝不发布
            return job_id
        except RejectError as exc:
            self._safe_reject(job_id, epoch, exc)
            return job_id
        except Exception as exc:  # noqa: BLE001 - 记录失败，等待租约超时重试
            self.store.fail(job_id, epoch, repr(exc))
            raise

    def _flush(self, job_id: int, epoch: int, acc: Accumulator,
               next_cursor: int, now: Optional[float]) -> None:
        lease = (self.clock() if now is None else now) + self.lease_seconds
        self.store.save_progress(
            job_id, epoch, next_cursor, _dump_numerators(acc.numerators),
            acc.total_base, acc.total_current, acc.missing_base,
            acc.missing_current, lease, now=now)

    def _safe_reject(self, job_id: int, epoch: int, exc: RejectError) -> None:
        try:
            self.store.reject(job_id, epoch, exc.code, exc.message, exc.details)
        except FencedError:
            pass

    def _basis(self, job: dict) -> dict:
        return {
            'base_run_id': job['base_run_id'],
            'current_run_id': job['current_run_id'],
            'base_checksum': job['base_checksum'],
            'current_checksum': job['current_checksum'],
            'pairing_rule': '按冻结连续编号 line_no 1..N 配对；单边缺项按零用量，'
                            '因子与部门借用有记录一侧',
            'allocation_rule': '用量/因子/部门归属三维度全部6种替换顺序的平均边际贡献',
        }
