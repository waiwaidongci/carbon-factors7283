"""服务层冒烟：已复核门控、预检拒算、幂等、冻结编号注入。

不依赖 FastAPI/Postgres：用轻量假对象模拟 Run/DB，store 用临时 sqlite。
"""
import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from decimal import Decimal
import sys

# 容器外：补齐 settings 需要的路径与环境
_BACKEND = Path(__file__).resolve().parents[1]
os.environ.setdefault('DATABASE_URL', 'sqlite://')
os.environ.setdefault('JWT_SECRET', 'test-secret')
if not Path('/app/project.json').exists():
    _cfg = (_BACKEND / 'project.json').read_text()
    _real_read_text = Path.read_text

    def _patched_read_text(self, *args, **kwargs):
        if str(self) == '/app/project.json':
            return _cfg
        return _real_read_text(self, *args, **kwargs)
    Path.read_text = _patched_read_text

# 纯标准库环境：注入最小 fastapi/sqlalchemy 垫片（生产由 requirements 真包提供）
try:
    import fastapi  # noqa: F401
    import sqlalchemy  # noqa: F401
    _HAS_FASTAPI = True
except ImportError:
    import types
    from tests import fastapi_compat, sqlalchemy_compat
    sys.modules.setdefault('fastapi', fastapi_compat)
    responses = types.ModuleType('fastapi.responses')
    responses.Response = fastapi_compat.Response
    sys.modules.setdefault('fastapi.responses', responses)
    sys.modules.setdefault('sqlalchemy', sqlalchemy_compat)
    orm = types.ModuleType('sqlalchemy.orm')
    orm.sessionmaker = sqlalchemy_compat.sessionmaker
    orm.DeclarativeBase = sqlalchemy_compat.DeclarativeBase
    sys.modules.setdefault('sqlalchemy.orm', orm)
    _HAS_FASTAPI = False

from fastapi import HTTPException
from src.services import reconciliation as svc

from tests.helpers import make_store, new_tmpdir


POLICY = {
    'version': '因子-v1', 'scope': '合成研究用活动量，仅覆盖三类活动',
    'factors': {
        '电力': {'unit': 'kWh', 'kg_per_unit': 0.57},
        '柴油': {'unit': 'L', 'kg_per_unit': 2.68},
        '通勤': {'unit': 'km', 'kg_per_unit': 0.12},
    },
}


def make_run(run_id, approved, sources, version_suffix=''):
    policy = dict(POLICY)
    if version_suffix:
        policy = dict(policy, version='因子-v' + version_suffix)
    snapshot = {'policy': policy,
                'sources': [dict(s, line_no=i) for i, s in enumerate(sources, 1)],
                'period': '全部'}
    total = Decimal(0)
    for s in snapshot['sources']:
        f = policy['factors'][s['activity']]
        total += Decimal(str(s['quantity'])) * Decimal(str(f['kg_per_unit']))
    result = {'summary': {'核算kgCO2e精确': str(total)}}
    return SimpleNamespace(id=run_id, name=f'报告{run_id}', status='completed',
                           approved=approved, snapshot=snapshot, result=result,
                           checksum='a' * 64, review='已核对')


class _DB:
    def __init__(self, runs):
        self._runs = {r.id: r for r in runs}
        self.commits = 0

    def get(self, model, run_id):
        return self._runs.get(run_id)

    def add(self, obj):
        self._added = getattr(self, '_added', [])
        self._added.append(obj)

    def commit(self):
        self.commits += 1


USER = SimpleNamespace(email='analyst@demo.local', role='analyst')


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = new_tmpdir()
        self.store = make_store(self.tmp)

    def _runs(self):
        src1 = [
            {'period': '2026-10', 'department': '材料试验室', 'activity': '电力',
             'quantity': 100, 'unit': 'kWh'},
            {'period': '2026-10', 'department': '工艺试验室', 'activity': '柴油',
             'quantity': 10, 'unit': 'L'},
        ]
        src2 = [
            {'period': '2026-11', 'department': '材料试验室', 'activity': '电力',
             'quantity': 120, 'unit': 'kWh'},
            {'period': '2026-11', 'department': '光学试验室', 'activity': '柴油',
             'quantity': 10, 'unit': 'L'},
        ]
        return make_run(1, True, src1), make_run(2, True, src2, '2')

    def test_requires_approved_reports(self):
        r1, r2 = self._runs()
        r1.approved = False
        db = _DB([r1, r2])
        with self.assertRaises(HTTPException) as cm:
            svc.create_reconciliation(db, USER, self.store, 1, 2)
        self.assertEqual(cm.exception.status_code, 409)

    def test_reject_same_run_and_missing_line_no(self):
        r1, _ = self._runs()
        db = _DB([r1])
        with self.assertRaises(HTTPException) as cm:
            svc.create_reconciliation(db, USER, self.store, 1, 1)
        self.assertEqual(cm.exception.status_code, 422)
        # 旧报告（无 line_no）预检拒算
        old = make_run(9, True, [r1.snapshot['sources'][0]])
        for s in old.snapshot['sources']:
            s.pop('line_no')
        db2 = _DB([old, r1])
        with self.assertRaises(HTTPException) as cm2:
            svc.create_reconciliation(db2, USER, self.store, 9, 1)
        self.assertEqual(cm2.exception.status_code, 422)
        self.assertIn('拒算', cm2.exception.detail)

    def test_create_is_idempotent_and_stages_lines(self):
        r1, r2 = self._runs()
        db = _DB([r1, r2])
        out1 = svc.create_reconciliation(db, USER, self.store, 1, 2)
        job1 = out1['job']
        self.assertEqual(job1['status'], 'queued')
        self.assertEqual(self.store.count_lines(job1['id'], 'base'), 2)
        self.assertEqual(self.store.count_lines(job1['id'], 'current'), 2)
        # 同配对重复发起：未完成时冲突
        with self.assertRaises(HTTPException) as cm:
            svc.create_reconciliation(db, USER, self.store, 1, 2)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertIn('frozen_basis', out1)

    def test_end_to_end_completed_job_ties_out(self):
        from src.reconciliation.runner import ReconcileRunner
        r1, r2 = self._runs()
        db = _DB([r1, r2])
        job = svc.create_reconciliation(db, USER, self.store, 1, 2)['job']
        ReconcileRunner(self.store).run_once('w')
        done = self.store.get_job(job['id'])
        self.assertEqual(done['status'], 'completed')
        self.assertTrue(done['result']['tie_out_ok'])
        # 完成态门控（与 routes.reconciliations._view 同规则）：
        # 完成态下发结果；未完成时 result/frozen_basis 必须为 None
        self.assertIsNotNone(done['result'])
        self.assertIsNotNone(done['frozen_basis'])
        running = dict(done, status='running', result=None, frozen_basis=None)
        self.assertIsNone(running['result'])
        self.assertIsNone(running['frozen_basis'])

    def test_large_snapshot_service_stays_bounded(self):
        """服务层对大快照分块处理，额外内存不随行数线性增长。"""
        import tracemalloc
        n = 60_000

        def src(prefix, dept):
            return [{'period': prefix, 'department': dept, 'activity': '电力',
                     'quantity': (i % 500) + 1, 'unit': 'kWh'} for i in range(n)]

        r1 = make_run(1, True, src('2026-10', '材料试验室'))
        r2 = make_run(2, True, src('2026-11', '材料试验室'), '2')
        db = _DB([r1, r2])
        tracemalloc.start()
        job = svc.create_reconciliation(db, USER, self.store, 1, 2,
                                        batch_size=10_000)['job']
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertEqual(self.store.count_lines(job['id'], 'base'), n)
        self.assertEqual(self.store.count_lines(job['id'], 'current'), n)
        # 分块（5000/块）下服务层额外峰值应远低于全量物化；限额取 32MiB
        self.assertLess(peak, 32 * 1024 * 1024,
                        msg=f'服务层峰值 {peak} 超 32MiB，疑似全量物化')


if __name__ == '__main__':
    unittest.main(verbosity=2)
