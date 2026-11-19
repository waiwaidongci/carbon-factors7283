"""百万行规模测试：分批处理、额外内存 ≤64MiB、续跑正确性。

只依赖标准库；用临时 sqlite 文件作为分片落库介质，模拟生产中的分批管线。
"""
import os
import time
import tracemalloc
import unittest
from decimal import Decimal

from tests.helpers import make_store, new_tmpdir
from src.reconciliation.engine import FrozenLine
from src.reconciliation.runner import ReconcileRunner

N = 1_000_000
BATCH = 50_000
MEM_LIMIT = 64 * 1024 * 1024  # 64 MiB

ACT = [
    ('电力', 'kWh', Decimal('0.57'), Decimal('0.63')),
    ('柴油', 'L', Decimal('2.68'), Decimal('2.68')),
    ('通勤', 'km', Decimal('0.12'), Decimal('0.15')),
]
DEPTS = ['材料试验室', '工艺试验室', '光学试验室', '能源办公室', '测试中心']


def gen_lines(side: str, n: int):
    """确定性生成 n 行，量值随编号变化，制造用量/因子/部门三类变动。"""
    chunk = []
    for i in range(1, n + 1):
        idx = i % 3
        activity, unit, f1, f2 = ACT[idx]
        factor = f1 if side == 'base' else f2
        q_base = Decimal((i * 7) % 500 + 1)
        quantity = q_base if side == 'base' else (q_base + Decimal((i % 11) - 5))
        dept = DEPTS[i % len(DEPTS)] if side == 'base' else DEPTS[(i + (i % 2)) % len(DEPTS)]
        chunk.append(FrozenLine(
            line_no=i, activity=activity, unit=unit, source_unit=unit,
            quantity=quantity, factor=factor, emission=quantity * factor,
            department=dept,
        ))
        if len(chunk) == BATCH:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


class MillionRowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = new_tmpdir()
        self.store = make_store(self.tmp)

    def _stage(self, job_id: int):
        base_total = Decimal(0)
        current_total = Decimal(0)
        for chunk in gen_lines('base', N):
            self.store.stage_lines(job_id, 'base', chunk)
            base_total += sum((x.emission for x in chunk), Decimal(0))
        for chunk in gen_lines('current', N):
            self.store.stage_lines(job_id, 'current', chunk)
            current_total += sum((x.emission for x in chunk), Decimal(0))
        return base_total, current_total

    def test_million_rows_under_64mib_and_tie_out(self):
        job_id = self.store.create_job(
            base_run_id=10, current_run_id=11, batch_size=BATCH,
            base_checksum='b' * 64, current_checksum='c' * 64,
            created_by='scale-test', original_base_total='0',
            original_current_total='0')
        base_total, current_total = self._stage(job_id)
        with self.store._connect() as conn:
            cur = conn.cursor()
            cur.execute('UPDATE recon_jobs SET original_base_total=?,original_current_total=? '
                        'WHERE id=?', (str(base_total), str(current_total), job_id))
            conn.commit()

        self.assertEqual(self.store.count_lines(job_id, 'base'), N)
        self.assertEqual(self.store.count_lines(job_id, 'current'), N)

        runner = ReconcileRunner(self.store, lease_seconds=600, heartbeat_every=100_000)
        tracemalloc.start()
        start = time.time()
        runner.run_once('scale-worker')
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        elapsed = time.time() - start

        job = self.store.get_job(job_id)
        self.assertEqual(job['status'], 'completed', msg=job.get('reject_message'))
        self.assertEqual(job['total_pairs'], N)
        self.assertTrue(job['result']['tie_out_ok'])
        self.assertEqual(
            Decimal(job['result']['report_delta']),
            (current_total - base_total).quantize(Decimal('0.000001')))
        # 部门维度贡献行数有界（与百万行无关），验证内存模型
        self.assertLessEqual(len(job['result']['components']),
                             len(DEPTS) * 3 + len(ACT) * 3)

        print(f'\n百万行：用时 {elapsed:.1f}s，Python 额外峰值内存 '
              f'{peak / 1024 / 1024:.2f} MiB（限额 64 MiB）')
        self.assertLess(peak, MEM_LIMIT,
                        msg=f'额外峰值内存 {peak} 超过 64MiB 限额')

    def test_million_row_resume_keeps_exact_result(self):
        """中断续跑：先处理 30% 后换执行者接管，结果须与一次跑完一致。"""
        job_id = self.store.create_job(
            base_run_id=20, current_run_id=21, batch_size=BATCH,
            base_checksum='b' * 64, current_checksum='c' * 64,
            created_by='scale-test', original_base_total='0',
            original_current_total='0')
        base_total, current_total = self._stage(job_id)
        with self.store._connect() as conn:
            cur = conn.cursor()
            cur.execute('UPDATE recon_jobs SET original_base_total=?,original_current_total=? '
                        'WHERE id=?', (str(base_total), str(current_total), job_id))
            conn.commit()

        runner = ReconcileRunner(self.store, lease_seconds=5, heartbeat_every=100_000)
        claim = self.store.claim('w-old', lease_seconds=5, now=0)
        from src.reconciliation.engine import Accumulator
        from src.reconciliation.runner import _dump_numerators
        acc = Accumulator()
        target = int(N * 0.3)
        for pair in runner._iter_pairs(job_id):
            if pair.line_no > target:
                break
            acc.add_pair(pair)
        self.store.save_progress(
            job_id, claim['epoch'], target + 1,
            _dump_numerators(acc.numerators), acc.total_base, acc.total_current,
            acc.missing_base, acc.missing_current, lease_expires=5, now=1)

        # 旧租约过期，新执行者接管续跑
        runner2 = ReconcileRunner(self.store, lease_seconds=600, heartbeat_every=100_000)
        runner2.run_once('w-new', now=100)
        job = self.store.get_job(job_id)
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['total_pairs'], N)
        self.assertTrue(job['result']['tie_out_ok'])
        self.assertEqual(job['epoch'], claim['epoch'] + 1)
        self.assertEqual(
            Decimal(job['result']['components_total']),
            (current_total - base_total).quantize(Decimal('0.000001')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
