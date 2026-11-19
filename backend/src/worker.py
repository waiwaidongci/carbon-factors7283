import time,traceback,uuid
from pathlib import Path
import redis
from src.config.database import Base,engine,Session
from src.config.settings import REDIS_URL
from src.models import identity,source,run  # noqa: F401  导入即注册模型建表
from src.models.run import Run
from src.services.calculation import calculate
from src.services.delivery import tick
from src.reconciliation.factory import get_store
from src.reconciliation.runner import ReconcileRunner
from src.reconciliation.store import FencedError
Base.metadata.create_all(engine)
r=redis.Redis.from_url(REDIS_URL)
WORKER_ID='worker-'+uuid.uuid4().hex[:12]
recon_runner=ReconcileRunner(get_store(),lease_seconds=30.0,heartbeat_every=5.0)
while True:
 Path('/tmp/worker-heartbeat').write_text(str(time.time()))
 try:
  item=r.lpop('report-jobs')
  if item:
   with Session() as db:
    job=db.get(Run,int(item))
    if job and job.status=='queued':
     job.status='running';db.commit()
     try: job.result=calculate(job.snapshot['sources'],job.snapshot['policy']);job.status='completed'
     except Exception as exc: job.status='failed';job.error=str(exc);traceback.print_exc()
     db.commit()
  # 归因队列：认领即 fence，断点续跑；旧执行者的晚到写入会被 epoch 拒绝
  try: recon_runner.run_once(WORKER_ID)
  except FencedError: pass
  with Session() as db: tick(db)
 except Exception: traceback.print_exc()
 time.sleep(1)
