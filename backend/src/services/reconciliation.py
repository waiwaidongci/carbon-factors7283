"""两期归因任务服务：从已复核报告冻结快照建任务、分块落冻结行。

内存策略：快照→标准化行→落库全程以生成器/分块处理，除分块外不整体物化，
百万行下服务层额外内存有界；跨侧编号并集的连续性由 worker 流式校验兜底。
"""
from __future__ import annotations

from decimal import Decimal

from fastapi import HTTPException

from src.models.run import Run
from src.reconciliation import engine
from src.reconciliation.engine import RejectError, build_frozen_line
from src.reconciliation.store import SqlStore
from src.services.audit import log

DEFAULT_BATCH_SIZE = 50_000
STAGE_CHUNK = 5_000


def _require_approved_run(db, run_id: int, label: str) -> Run:
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, f'{label}报告不存在')
    if run.status != 'completed' or not run.approved:
        raise HTTPException(409, f'{label}必须是已复核报告才可发起归因')
    return run


def _iter_frozen_raw(snapshot: dict):
    """逐条产出带因子信息的原始行；缺冻结编号/因子即拒算（不静默补号）。"""
    factors = snapshot['policy']['factors']
    for source in snapshot['sources']:
        if 'line_no' not in source:
            raise RejectError(
                engine.REJECT_MISSING_NUMBER,
                '报告快照缺少冻结编号 line_no（旧版报告不支持归因，请重新提交核算）',
                {'has_line_no': False})
        factor = factors.get(source['activity'])
        if not factor:
            raise RejectError(
                engine.REJECT_UNIT_INCOMPARABLE,
                f"冻结口径缺少活动因子：{source['activity']}",
                {'activity': source['activity']})
        yield {
            'line_no': source['line_no'],
            'activity': source['activity'],
            'unit': source['unit'],
            'quantity': source['quantity'],
            'department': source['department'],
            'factor_unit': factor['unit'],
            'kg_per_unit': factor['kg_per_unit'],
        }


def _iter_lines(snapshot: dict):
    """逐条标准化，O(1) 做单侧非空/重号/乱序校验。"""
    prev = 0
    seen_any = False
    for raw in _iter_frozen_raw(snapshot):
        line = build_frozen_line(raw)
        if line.line_no <= prev:
            raise RejectError(
                engine.REJECT_DUPLICATE_NUMBER,
                f'冻结编号重号或乱序：{line.line_no}',
                {'line_no': line.line_no})
        prev = line.line_no
        seen_any = True
        yield line
    if not seen_any:
        raise RejectError(engine.REJECT_MISSING_NUMBER,
                          '报告快照没有任何底稿行', {})


def _exact_total(snapshot: dict) -> str:
    """用冻结快照按 Decimal 重算精确总量（流式，不物化）。"""
    total = Decimal(0)
    for line in _iter_lines(snapshot):
        total += line.emission
    return str(total)


def _stage_in_chunks(store: SqlStore, job_id: int, side: str, snapshot: dict) -> int:
    count = 0
    chunk = []
    for line in _iter_lines(snapshot):
        chunk.append(line)
        count += 1
        if len(chunk) >= STAGE_CHUNK:
            store.stage_lines(job_id, side, chunk)
            chunk = []
    if chunk:
        store.stage_lines(job_id, side, chunk)
    return count


def create_reconciliation(db, user, store: SqlStore, base_run_id: int,
                          current_run_id: int, batch_size: int = DEFAULT_BATCH_SIZE) -> dict:
    if base_run_id == current_run_id:
        raise HTTPException(422, '两份报告不能相同')
    base = _require_approved_run(db, base_run_id, '基准期')
    current = _require_approved_run(db, current_run_id, '对比期')
    if not (1 <= batch_size <= 1_000_000):
        raise HTTPException(422, '批大小需在 1..1000000 之间')

    existing = store.find_job(base_run_id, current_run_id)
    if existing:
        if existing['status'] == 'completed':
            return existing
        raise HTTPException(409, f"该配对已有{existing['status']}状态的归因任务，"
                                 '失败/拒算后可由负责人重置再跑')

    # 预检：单侧非空/重号（O(1) 流式）；单位不可比在 build_frozen_line 即时抛出。
    # 跨侧并集连续性由 worker 归并校验，拒算时任务不会发布结果。
    try:
        base_total = _exact_total(base.snapshot)
        current_total = _exact_total(current.snapshot)
    except RejectError as exc:
        log(db, user.email, 'recon.reject.preflight', exc.code)
        db.commit()
        raise HTTPException(422, f'拒算：{exc.message}')

    job_id = store.create_job(
        base_run_id=base_run_id, current_run_id=current_run_id,
        batch_size=batch_size,
        base_checksum=base.checksum, current_checksum=current.checksum,
        created_by=user.email,
        original_base_total=base_total,
        original_current_total=current_total)

    base_count = _stage_in_chunks(store, job_id, 'base', base.snapshot)
    current_count = _stage_in_chunks(store, job_id, 'current', current.snapshot)

    frozen_basis = {
        'base_run_id': base_run_id,
        'current_run_id': current_run_id,
        'base_run_name': base.name,
        'current_run_name': current.name,
        'base_checksum': base.checksum,
        'current_checksum': current.checksum,
        'base_policy_version': base.snapshot['policy']['version'],
        'current_policy_version': current.snapshot['policy']['version'],
        'pairing_rule': '按冻结连续编号 line_no 1..N 配对；单边缺项按零用量，'
                        '因子与部门借用有记录一侧',
        'allocation_rule': '用量/因子/部门归属三维度全部6种替换顺序的平均边际贡献',
        'base_line_count': base_count,
        'current_line_count': current_count,
    }
    # 冻结依据随任务保存，发布后仍由完成态结果门控展示
    store.save_basis(job_id, frozen_basis)
    job = store.get_job(job_id)
    log(db, user.email, 'recon.queue', f'{base_run_id}->{current_run_id}')
    db.commit()
    return {'job': job, 'frozen_basis': frozen_basis}


def reset_reconciliation(store: SqlStore, job_id: int) -> dict:
    store.requeue(job_id)
    return store.get_job(job_id)
