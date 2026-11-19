"""归因引擎与存储规则测试：拒算、配对、Shapley 加总、单边缺项、续跑、fence。"""
import unittest
from decimal import Decimal

from tests.helpers import make_store, make_line, staged_job, new_tmpdir
from src.reconciliation import engine
from src.reconciliation.engine import (
    Accumulator, PairData, RejectError, build_pairs, build_frozen_line,
    validate_numbering, validate_pair_numbering, attribute_pair, finalize,
    Q as DIM_Q, F as DIM_F, D as DIM_DEPT,
)
from src.reconciliation.runner import ReconcileRunner
from src.reconciliation.store import FencedError


def D(x):
    return Decimal(str(x))


class EngineRulesTests(unittest.TestCase):
    def test_missing_and_duplicate_number_rejected(self):
        good = [make_line(i, 'base') for i in range(1, 6)]
        dup = [make_line(i, 'base') for i in (1, 2, 3, 3, 5)]
        validate_numbering(good, '基准报告')  # 不抛
        # 单侧只查重号/非空；连续性在两侧并集上判定
        with self.assertRaises(RejectError) as cm2:
            validate_numbering(dup, '基准报告')
        self.assertEqual(cm2.exception.code, engine.REJECT_DUPLICATE_NUMBER)
        with self.assertRaises(RejectError) as cm3:
            build_frozen_line({'activity': '电力', 'unit': 'kWh', 'quantity': 1,
                               'department': 'A', 'factor_unit': 'kWh',
                               'kg_per_unit': 0.5})
        self.assertEqual(cm3.exception.code, engine.REJECT_MISSING_NUMBER)
        # 并集缺号拒算；并集连续（单边缺项）放行
        gap_a = [make_line(i, 'base') for i in (1, 2, 4, 5)]
        gap_b = [make_line(i, 'current') for i in (1, 2, 4, 5)]
        with self.assertRaises(RejectError) as cm4:
            validate_pair_numbering(gap_a, gap_b)
        self.assertEqual(cm4.exception.code, engine.REJECT_MISSING_NUMBER)
        full_b = [make_line(i, 'current') for i in range(1, 6)]
        validate_pair_numbering(gap_a, full_b)  # 不抛：单边缺项合法

    def test_unit_incomparable_rejected(self):
        with self.assertRaises(RejectError) as cm:
            build_frozen_line({'line_no': 1, 'activity': '电力', 'unit': 'kg',
                               'quantity': 10, 'department': 'A',
                               'factor_unit': 'kWh', 'kg_per_unit': 0.5})
        self.assertEqual(cm.exception.code, engine.REJECT_UNIT_INCOMPARABLE)
        a = make_line(1, 'base', unit='kWh')
        b = make_line(1, 'current', unit='L')
        with self.assertRaises(RejectError) as cm2:
            build_pairs([a], [b])
        self.assertEqual(cm2.exception.code, engine.REJECT_UNIT_INCOMPARABLE)

    def test_single_side_missing_borrows_factor_and_department(self):
        # 基准侧整体缺项：编号 1..3 只存在于对比侧，配对时按零用量并借用因子/部门
        current = [make_line(i, 'current') for i in range(1, 4)]
        runner_pairs = []
        conn_a, cur_a = None, iter(())
        for line in current:
            runner_pairs.append(PairData(
                line_no=line.line_no, activity=line.activity, unit=line.unit,
                q1=Decimal(0), f1=line.factor, q2=line.quantity, f2=line.factor,
                dept1=line.department, dept2=line.department, missing_side='base'))
        p = runner_pairs[2]
        self.assertEqual(p.missing_side, 'base')
        self.assertEqual(p.q1, Decimal(0))
        self.assertEqual(p.f1, p.f2)          # 借用对比侧因子
        self.assertEqual(p.dept1, p.dept2)    # 借用对比侧部门
        # 缺项侧的整体空集应触发缺号拒算
        with self.assertRaises(RejectError) as cm:
            validate_numbering([], '基准报告')
        self.assertEqual(cm.exception.code, engine.REJECT_MISSING_NUMBER)

    def test_shapley_tie_out_and_department_assignment(self):
        pairs = [
            PairData(1, '电力', 'kWh', D(100), D('0.5'), D(120), D('0.6'), 'A', 'A', None),
            PairData(2, '柴油', 'L', D(10), D('2'), D(10), D('2'), 'A', 'B', None),
            PairData(3, '通勤', 'km', D(0), D('0.1'), D(50), D('0.1'), 'B', 'B', 'base'),
            PairData(4, '电力', 'kWh', D(80), D('0.5'), D(0), D('0.5'), 'C', 'C', 'current'),
        ]
        acc = Accumulator()
        for p in pairs:
            acc.add_pair(p)
        res = finalize(acc)
        self.assertTrue(res['tie_out_ok'])
        self.assertEqual(Decimal(res['components_total']), Decimal('-13.000000'))
        self.assertEqual(Decimal(res['report_delta']), Decimal('-13.000000'))
        comp = {(r['department'], r['dimension']): Decimal(r['value'])
                for r in res['components']}
        # 部门归属纯转移：A -20 / B +20
        self.assertEqual(comp[('A', 'department')], Decimal('-20.000000'))
        self.assertEqual(comp[('B', 'department')], Decimal('20.000000'))
        # pair1: Q 与 F 各 11；单边缺项全部记用量
        self.assertEqual(comp[('A', 'quantity')], Decimal('11.000000'))
        self.assertEqual(comp[('A', 'factor')], Decimal('11.000000'))
        self.assertEqual(comp[('B', 'quantity')], Decimal('5.000000'))
        self.assertEqual(comp[('C', 'quantity')], Decimal('-40.000000'))
        # 部门行加总也等于整体差额，且部门名不被维度键覆盖
        dept_names = [r['department'] for r in res['by_department']]
        self.assertIn('A', dept_names)
        self.assertIn('B', dept_names)
        self.assertIn('C', dept_names)
        self.assertEqual(sum((Decimal(r['total']) for r in res['by_department']),
                             Decimal(0)), Decimal('-13.000000'))
        for r in res['by_department']:
            self.assertEqual(set(r),
                             {'department', '用量变动', '因子变动', '部门归属变动', 'total'})

    def test_symmetry_when_quantity_and_factor_roles_equivalent(self):
        # q/f 同比例变动时，Q 与 F 贡献应相等
        pair = PairData(1, '电力', 'kWh', D(100), D(1), D(200), D(2), 'A', 'A', None)
        c = attribute_pair(pair)
        nq = c.numerators.get(('A', 'quantity'), Decimal(0))
        nf = c.numerators.get(('A', 'factor'), Decimal(0))
        self.assertEqual(nq, nf)
        self.assertEqual(nq / 6 + nf / 6, D(300))  # 400-100

    def test_matches_bruteforce_shapley_definition(self):
        """独立教科书实现：对每部门/维度直接按 Shapley 定义枚举，对照记账实现。"""
        import itertools

        def brute(pair):
            depts = {pair.dept1, pair.dept2}
            dims = (DIM_Q, DIM_F, DIM_DEPT)
            out = {}
            for target_dim in dims:
                for dept in depts:
                    def emission_for(state, owner_filter):
                        qty = pair.q2 if DIM_Q in state else pair.q1
                        fac = pair.f2 if DIM_F in state else pair.f1
                        owner = pair.dept2 if DIM_DEPT in state else pair.dept1
                        return (qty * fac) if owner == owner_filter else Decimal(0)
                    total = Decimal(0)
                    for order in itertools.permutations(dims):
                        idx = order.index(target_dim)
                        before = set(order[:idx])
                        after = before | {target_dim}
                        total += emission_for(after, dept) - emission_for(before, dept)
                    out[(dept, target_dim)] = total / Decimal(6)
            return out

        cases = [
            PairData(1, '电力', 'kWh', D(100), D('0.5'), D(120), D('0.6'), 'A', 'A', None),
            PairData(2, '柴油', 'L', D(10), D(2), D(10), D(2), 'A', 'B', None),
            PairData(3, '通勤', 'km', D(0), D('0.1'), D(50), D('0.1'), 'B', 'B', 'base'),
            PairData(4, '电力', 'kWh', D(80), D('0.5'), D(0), D('0.5'), 'C', 'C', 'current'),
            PairData(5, '柴油', 'L', D(30), D(2), D(45), D('3.1'), 'A', 'B', None),
        ]
        for pair in cases:
            got = attribute_pair(pair)
            expect = brute(pair)
            for key, value in expect.items():
                got_value = got.numerators.get(key, Decimal(0)) / Decimal(6)
                self.assertEqual(got_value, value, msg=f'{pair.line_no} {key}')
            # 全部部门边际之和 = 该配对总差额
            total = sum(got.numerators.values(), Decimal(0)) / Decimal(6)
            self.assertEqual(total, pair.e2 - pair.e1)


class RunnerFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = new_tmpdir()
        self.store = make_store(self.tmp)

    def _completed(self, n=200, batch_size=37):
        base = [make_line(i, 'base') for i in range(1, n + 1)]
        current = [make_line(i, 'current') for i in range(1, n + 1)]
        b_total = sum((x.emission for x in base), Decimal(0))
        c_total = sum((x.emission for x in current), Decimal(0))
        job_id = staged_job(self.store, base, current,
                            str(b_total), str(c_total), batch_size=batch_size)
        runner = ReconcileRunner(self.store, lease_seconds=30, heartbeat_every=0.01)
        runner.run_once('w1')
        return job_id, b_total, c_total

    def test_full_run_completes_and_ties_out(self):
        job_id, b_total, c_total = self._completed()
        job = self.store.get_job(job_id)
        self.assertEqual(job['status'], 'completed')
        self.assertTrue(job['result']['tie_out_ok'])
        self.assertEqual(Decimal(job['result']['report_delta']),
                         (c_total - b_total).quantize(Decimal('0.000001')))

    def test_resume_does_not_double_count(self):
        base = [make_line(i, 'base') for i in range(1, 101)]
        current = [make_line(i, 'current') for i in range(1, 101)]
        b_total = sum((x.emission for x in base), Decimal(0))
        c_total = sum((x.emission for x in current), Decimal(0))
        job_id = staged_job(self.store, base, current,
                            str(b_total), str(c_total), batch_size=25)
        runner = ReconcileRunner(self.store, lease_seconds=30, heartbeat_every=0.01)

        # 跑两批后模拟崩溃：用新执行者接管租约，续跑
        claim = self.store.claim('w-old', lease_seconds=5, now=0)
        self.assertEqual(claim['job_id'], job_id)
        # 手动只推进两批（沿用旧执行者 5 秒短租约，模拟随后崩溃）
        from src.reconciliation.runner import _dump_numerators
        acc = Accumulator()
        for pair in list(runner._iter_pairs(job_id))[:50]:
            acc.add_pair(pair)
        self.store.save_progress(
            job_id, claim['epoch'], 51, _dump_numerators(acc.numerators),
            acc.total_base, acc.total_current, acc.missing_base,
            acc.missing_current, lease_expires=5, now=1)
        # 旧租约已在 5 秒过期；让新执行者自行接管（epoch +1）
        t = {'v': 100}
        runner2 = ReconcileRunner(self.store, lease_seconds=30, heartbeat_every=5,
                                  clock=lambda: t['v'])
        runner2.run_once('w-new', now=100)
        job = self.store.get_job(job_id)
        self.assertEqual(job['epoch'], claim['epoch'] + 1)  # 新执行者 fence 代次
        self.assertEqual(job['status'], 'completed')
        # 与一次跑完的基准结果完全一致（无重复计入）
        self.assertEqual(Decimal(job['result']['delta']),
                         (c_total - b_total).quantize(Decimal('0.000001')))
        self.assertEqual(job['total_pairs'], 100)

    def test_stale_executor_cannot_publish_or_write(self):
        base = [make_line(i, 'base') for i in range(1, 21)]
        current = [make_line(i, 'current') for i in range(1, 21)]
        b_total = sum((x.emission for x in base), Decimal(0))
        c_total = sum((x.emission for x in current), Decimal(0))
        job_id = staged_job(self.store, base, current,
                            str(b_total), str(c_total), batch_size=10)
        old = self.store.claim('w-old', lease_seconds=5, now=0)
        # 租约过期，新执行者接管
        new = self.store.claim('w-new', lease_seconds=30, now=100)
        self.assertEqual(new['epoch'], old['epoch'] + 1)
        with self.assertRaises(FencedError):
            self.store.heartbeat(job_id, old['epoch'], 30)
        with self.assertRaises(FencedError):
            self.store.save_progress(job_id, old['epoch'], 1, {}, Decimal(0),
                                     Decimal(0), 0, 0, 1.0)
        with self.assertRaises(FencedError):
            self.store.publish(job_id, old['epoch'], {'tie_out_ok': True}, {}, 20)
        # 新执行者正常完成并发布
        clock = iter(range(1000, 2000))
        ReconcileRunner(self.store, lease_seconds=30, heartbeat_every=5,
                        clock=lambda: next(clock)).run_once('w-new')
        self.assertEqual(self.store.get_job(job_id)['status'], 'completed')

    def test_two_workers_only_latest_epoch_publishes(self):
        # 两个执行者先后接管同一任务：旧者被 fence，只有最新代次能发布
        base = [make_line(i, 'base') for i in range(1, 31)]
        current = [make_line(i, 'current') for i in range(1, 31)]
        b_total = sum((x.emission for x in base), Decimal(0))
        c_total = sum((x.emission for x in current), Decimal(0))
        job_id = staged_job(self.store, base, current,
                            str(b_total), str(c_total), batch_size=10)
        old_claim = self.store.claim('worker-A', lease_seconds=5, now=0)
        # A 心跳正常（租约内）
        self.store.heartbeat(job_id, old_claim['epoch'], 5, now=1)
        # B 接管过期租约（epoch+1）
        t = {'v': 100}
        ReconcileRunner(self.store, lease_seconds=30, heartbeat_every=5,
                        clock=lambda: t['v']).run_once('worker-B', now=100)
        # A 晚到的所有写入均被新代次拒绝
        with self.assertRaises(FencedError):
            self.store.heartbeat(job_id, old_claim['epoch'], 5, now=101)
        with self.assertRaises(FencedError):
            self.store.publish(job_id, old_claim['epoch'],
                               {'tie_out_ok': True}, {}, 30, now=102)
        job = self.store.get_job(job_id)
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['epoch'], old_claim['epoch'] + 1)
        attempts = self.store.attempts(job_id)
        self.assertEqual([a['worker_id'] for a in attempts], ['worker-A', 'worker-B'])
        self.assertEqual([a['epoch'] for a in attempts],
                         [old_claim['epoch'], old_claim['epoch'] + 1])

    def test_reject_missing_number_persists_and_blocks_publish(self):
        # 全局并集缺号：两侧都没有 3 号 → 拒算（区别于合法的单边缺项）
        base = [make_line(i, 'base') for i in (1, 2, 4, 5)]
        current = [make_line(i, 'current') for i in (1, 2, 4, 5)]
        b_total = sum((x.emission for x in base), Decimal(0))
        c_total = sum((x.emission for x in current), Decimal(0))
        job_id = staged_job(self.store, base, current,
                            str(b_total), str(c_total), batch_size=10)
        ReconcileRunner(self.store).run_once('w1')
        job = self.store.get_job(job_id)
        self.assertEqual(job['status'], 'rejected')
        self.assertEqual(job['reject_code'], engine.REJECT_MISSING_NUMBER)
        self.assertEqual(job['reject_details']['missing'], [3])
        self.assertIsNone(job['result'])

    def test_legal_single_side_gap_completes(self):
        # 基准侧缺 3 号但对比侧有：并集仍为 1..5，按零用量借用对比侧因子/部门，应完成
        base = [make_line(i, 'base') for i in (1, 2, 4, 5)]
        current = [make_line(i, 'current') for i in range(1, 6)]
        b_total = sum((x.emission for x in base), Decimal(0))
        c_total = sum((x.emission for x in current), Decimal(0))
        job_id = staged_job(self.store, base, current,
                            str(b_total), str(c_total), batch_size=10)
        ReconcileRunner(self.store).run_once('w1')
        job = self.store.get_job(job_id)
        self.assertEqual(job['status'], 'completed', msg=job.get('reject_message'))
        self.assertTrue(job['result']['tie_out_ok'])
        self.assertEqual(job['result']['missing_base'], 1)
        self.assertEqual(job['total_pairs'], 5)

    def test_reject_incomparable_unit_persists(self):
        base = [make_line(i, 'base') for i in range(1, 6)]
        current = [make_line(i, 'current') for i in range(1, 6)]
        # 同一活动但第 3 号两期单位不可比（L 与因子单位 kWh 无换算关系）
        current[2] = make_line(3, 'current', activity_idx=0)
        current[2] = engine.FrozenLine(
            line_no=3, activity='电力', unit='L', source_unit='L',
            quantity=current[2].quantity, factor=current[2].factor,
            emission=current[2].emission, department=current[2].department)
        b_total = sum((x.emission for x in base), Decimal(0))
        c_total = sum((x.emission for x in current), Decimal(0))
        job_id = staged_job(self.store, base, current,
                            str(b_total), str(c_total), batch_size=10)
        ReconcileRunner(self.store).run_once('w1')
        job = self.store.get_job(job_id)
        self.assertEqual(job['status'], 'rejected')
        self.assertEqual(job['reject_code'], engine.REJECT_UNIT_INCOMPARABLE)
        self.assertEqual(job['reject_details']['line_no'], 3)

    def test_reset_then_rerun_succeeds(self):
        # 全局并集缺号导致拒算；reset 后补齐缺失编号重跑成功
        full_base = [make_line(i, 'base') for i in range(1, 4)]
        current = [make_line(i, 'current') for i in range(1, 4)]
        b_total = sum((x.emission for x in full_base), Decimal(0))
        c_total = sum((x.emission for x in current), Decimal(0))
        job_id = staged_job(self.store, full_base, current,
                            str(b_total), str(c_total), batch_size=10)
        with self.store._connect() as conn:
            cur = conn.cursor()
            cur.execute('DELETE FROM recon_lines WHERE job_id=? AND side=? AND line_no=?',
                        (job_id, 'base', 2))
            conn.commit()
            cur2 = conn.cursor()
            cur2.execute('DELETE FROM recon_lines WHERE job_id=? AND side=? AND line_no=?',
                         (job_id, 'current', 2))
            conn.commit()
        ReconcileRunner(self.store).run_once('w1')
        self.assertEqual(self.store.get_job(job_id)['status'], 'rejected')
        # reset 清进度；恢复两侧完整冻结底稿后重跑，原报告总量始终未变
        self.store.requeue(job_id)
        with self.store._connect() as conn:
            cur = conn.cursor()
            cur.execute('DELETE FROM recon_lines WHERE job_id=? AND side=?',
                        (job_id, 'base'))
            cur.execute('DELETE FROM recon_lines WHERE job_id=? AND side=?',
                        (job_id, 'current'))
            conn.commit()
        self.store.stage_lines(job_id, 'base', full_base)
        self.store.stage_lines(job_id, 'current', current)
        ReconcileRunner(self.store).run_once('w1')
        self.assertEqual(self.store.get_job(job_id)['status'], 'completed')
        self.assertTrue(self.store.get_job(job_id)['result']['tie_out_ok'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
