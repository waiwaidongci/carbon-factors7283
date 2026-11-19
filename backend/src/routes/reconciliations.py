from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from src.config.database import database
from src.controllers.auth import current, editor, admin
from src.schemas.requests import ReconInput
from src.services.reconciliation import create_reconciliation, reset_reconciliation
from src.services.export import reconciliation_csv
from src.reconciliation.factory import get_store
router = APIRouter()


def _view(job: dict) -> dict:
    """完成态门控：未完整对账时不下发贡献结果与冻结依据。"""
    view = {k: v for k, v in job.items() if k not in ('numerators',)}
    if job['status'] != 'completed':
        view['result'] = None
        view['frozen_basis'] = None
    view['attempts'] = get_store().attempts(job['id'])
    return view


@router.get('/reconciliations')
def list_jobs(db=Depends(database), u=Depends(current)):
    return [_view(j) for j in get_store().list_jobs()]


@router.get('/reconciliations/{job_id}')
def detail(job_id: int, db=Depends(database), u=Depends(current)):
    job = get_store().get_job(job_id)
    if not job:
        raise HTTPException(404, '归因任务不存在')
    return _view(job)


@router.post('/reconciliations')
def create(b: ReconInput, db=Depends(database), u=Depends(editor)):
    out = create_reconciliation(db, u, get_store(), b.base_run_id,
                                b.current_run_id, b.batch_size)
    return _view(out['job'])


@router.post('/reconciliations/{job_id}/reset')
def reset(job_id: int, db=Depends(database), u=Depends(admin)):
    try:
        return _view(reset_reconciliation(get_store(), job_id))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.get('/reconciliations/{job_id}/csv')
def download(job_id: int, db=Depends(database), u=Depends(current)):
    job = get_store().get_job(job_id)
    if not job:
        raise HTTPException(404, '归因任务不存在')
    if job['status'] != 'completed':
        raise HTTPException(409, '仅完整对账通过的归因任务可导出')
    return Response(
        reconciliation_csv(job['result']), media_type='text/csv',
        headers={'Content-Disposition': f'attachment; filename="reconciliation-{job_id}.csv"'})
