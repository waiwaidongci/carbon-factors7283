"""归因任务的持久化存储（原始 SQL，同时支持 SQLite 与 PostgreSQL）。

设计要点
--------
* 进度以"整份累加器状态 + 游标"落库，每批一个事务：崩溃后从游标续跑，
  已处理编号不会被二次计入（重试不重复计入）。
* 每次认领生成递增 epoch 作为 fencing token；旧执行者的所有条件写
  （心跳/进度/发布）都带 ``WHERE epoch=?``，rowcount=0 即被 fence，
  晚到的旧执行者无法发布结果。
* 冻结底稿按行落 ``recon_lines``，配对时用两个有序游标归并，
  百万行不整体载入内存。
"""
from __future__ import annotations

import json
import time
from typing import Callable, Optional

SCHEMA_SQLITE = [
    """CREATE TABLE IF NOT EXISTS recon_jobs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        base_run_id INTEGER NOT NULL,
        current_run_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        epoch INTEGER NOT NULL DEFAULT 0,
        attempt_no INTEGER NOT NULL DEFAULT 0,
        worker_id TEXT,
        lease_expires REAL,
        batch_size INTEGER NOT NULL,
        cursor_no INTEGER NOT NULL DEFAULT 1,
        total_pairs INTEGER,
        numerators TEXT NOT NULL DEFAULT '{}',
        total_base TEXT NOT NULL DEFAULT '0',
        total_current TEXT NOT NULL DEFAULT '0',
        original_base_total TEXT NOT NULL DEFAULT '0',
        original_current_total TEXT NOT NULL DEFAULT '0',
        missing_base INTEGER NOT NULL DEFAULT 0,
        missing_current INTEGER NOT NULL DEFAULT 0,
        result TEXT,
        reject_code TEXT,
        reject_message TEXT,
        reject_details TEXT,
        base_checksum TEXT,
        current_checksum TEXT,
        frozen_basis TEXT,
        created_by TEXT,
        created_at REAL,
        updated_at REAL,
        UNIQUE(base_run_id, current_run_id)
    )""",
    """CREATE TABLE IF NOT EXISTS recon_lines(
        job_id INTEGER NOT NULL,
        side TEXT NOT NULL,
        line_no INTEGER NOT NULL,
        activity TEXT NOT NULL,
        unit TEXT NOT NULL,
        source_unit TEXT NOT NULL,
        quantity TEXT NOT NULL,
        factor TEXT NOT NULL,
        emission TEXT NOT NULL,
        department TEXT NOT NULL,
        PRIMARY KEY(job_id, side, line_no)
    )""",
    """CREATE TABLE IF NOT EXISTS recon_attempts(
        job_id INTEGER NOT NULL,
        attempt_no INTEGER NOT NULL,
        worker_id TEXT NOT NULL,
        epoch INTEGER NOT NULL,
        started_at REAL NOT NULL,
        ended_at REAL,
        outcome TEXT,
        detail TEXT,
        PRIMARY KEY(job_id, attempt_no)
    )""",
    """CREATE TABLE IF NOT EXISTS recon_audit(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at REAL NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        detail TEXT NOT NULL
    )""",
]

SCHEMA_POSTGRES = [s.replace('INTEGER PRIMARY KEY AUTOINCREMENT',
                             'INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY')
                   for s in SCHEMA_SQLITE]

JOB_COLUMNS = ('id', 'base_run_id', 'current_run_id', 'status', 'epoch', 'attempt_no',
               'worker_id', 'lease_expires', 'batch_size', 'cursor_no', 'total_pairs',
               'numerators', 'total_base', 'total_current',
               'original_base_total', 'original_current_total',
               'missing_base', 'missing_current',
               'result', 'reject_code', 'reject_message',
               'reject_details', 'base_checksum', 'current_checksum', 'frozen_basis',
               'created_by', 'created_at', 'updated_at')


class FencedError(Exception):
    """当前执行者的 epoch 已过期，所有写入被拒绝。"""


class SqlStore:
    """传入返回 DBAPI 连接的工厂；dialect 为 'sqlite' 或 'postgres'。"""

    def __init__(self, connect: Callable, dialect: str = 'sqlite'):
        self._connect = connect
        self.dialect = dialect
        self.placeholder = '?' if dialect == 'sqlite' else '%s'
        self.create_schema()

    def ph(self, n: int) -> str:
        return ','.join([self.placeholder] * n)

    def create_schema(self) -> None:
        ddl = SCHEMA_SQLITE if self.dialect == 'sqlite' else SCHEMA_POSTGRES
        with self._connect() as conn:
            cur = conn.cursor()
            for stmt in ddl:
                cur.execute(stmt)
            conn.commit()

    # ---------- 任务创建 / 查询 ----------
    def create_job(self, base_run_id: int, current_run_id: int, batch_size: int,
                   base_checksum: str, current_checksum: str, created_by: str,
                   original_base_total: str, original_current_total: str,
                   now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        with self._connect() as conn:
            cur = conn.cursor()
            cols = ('base_run_id,current_run_id,status,batch_size,base_checksum,'
                    'current_checksum,created_by,original_base_total,'
                    'original_current_total,created_at,updated_at')
            values = (base_run_id, current_run_id, 'queued', batch_size,
                      base_checksum, current_checksum, created_by,
                      original_base_total, original_current_total, now, now)
            if self.dialect == 'postgres':
                cur.execute(f'INSERT INTO recon_jobs({cols}) VALUES({self.ph(11)}) '
                            'RETURNING id', values)
                job_id = cur.fetchone()[0]
            else:
                cur.execute(f'INSERT INTO recon_jobs({cols}) VALUES({self.ph(11)})', values)
                job_id = cur.lastrowid
            cur.execute('INSERT INTO recon_audit(at,actor,action,detail) '
                        f'VALUES({self.ph(4)})',
                        (now, created_by, 'recon.create', f'{base_run_id}->{current_run_id}'))
            conn.commit()
            return job_id

    def get_job(self, job_id: int) -> Optional[dict]:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(f'SELECT {",".join(JOB_COLUMNS)} FROM recon_jobs WHERE id={self.placeholder}',
                        (job_id,))
            row = cur.fetchone()
            return self._job_dict(row) if row else None

    def list_jobs(self) -> list:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(f'SELECT {",".join(JOB_COLUMNS)} FROM recon_jobs ORDER BY id DESC')
            return [self._job_dict(r) for r in cur.fetchall()]

    def find_job(self, base_run_id: int, current_run_id: int) -> Optional[dict]:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute('SELECT ' + ','.join(JOB_COLUMNS) +
                        f' FROM recon_jobs WHERE base_run_id={self.placeholder}'
                        f' AND current_run_id={self.placeholder}',
                        (base_run_id, current_run_id))
            row = cur.fetchone()
            return self._job_dict(row) if row else None

    def requeue(self, job_id: int, now: Optional[float] = None) -> None:
        """失败/拒算后的人工重试：清空进度回到 queued（epoch 在下次认领时递增）。"""
        now = time.time() if now is None else now
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"UPDATE recon_jobs SET status='queued',worker_id=NULL,lease_expires=NULL,"
                f"cursor_no=1,total_pairs=NULL,numerators='{{}}',total_base='0',"
                f"total_current='0',"
                f"missing_base=0,missing_current=0,result=NULL,"
                f"reject_code=NULL,reject_message=NULL,reject_details=NULL,"
                f"updated_at={self.placeholder} WHERE id={self.placeholder} AND status<>'completed'",
                (now, job_id))
            if cur.rowcount == 0:
                conn.rollback()
                raise ValueError('已完成任务不可重跑，请新建配对')
            # 冻结底稿保持不变，仅清空进度；attempt 记录保留用于审计
            conn.commit()

    def _job_dict(self, row) -> dict:
        data = dict(zip(JOB_COLUMNS, row))
        data['numerators'] = json.loads(data['numerators'] or '{}')
        data['result'] = json.loads(data['result']) if data['result'] else None
        data['reject_details'] = json.loads(data['reject_details']) if data['reject_details'] else None
        data['frozen_basis'] = json.loads(data['frozen_basis']) if data['frozen_basis'] else None
        return data

    # ---------- 冻结底稿分批落库 ----------
    def stage_lines(self, job_id: int, side: str, lines: list) -> None:
        """lines: 标准化后的 FrozenLine 列表，按批写入。"""
        if not lines:
            return
        payload = [(job_id, side, x.line_no, x.activity, x.unit, x.source_unit,
                    str(x.quantity), str(x.factor), str(x.emission), x.department) for x in lines]
        with self._connect() as conn:
            cur = conn.cursor()
            cur.executemany(
                'INSERT INTO recon_lines(job_id,side,line_no,activity,unit,source_unit,'
                f'quantity,factor,emission,department) VALUES({self.ph(10)})', payload)
            conn.commit()

    def count_lines(self, job_id: int, side: str) -> int:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute('SELECT COUNT(*) FROM recon_lines '
                        f'WHERE job_id={self.placeholder} AND side={self.placeholder}',
                        (job_id, side))
            return int(cur.fetchone()[0])

    def iter_lines(self, job_id: int, side: str, min_line_no: int = 1):
        """有序流式游标；min_line_no 提供续跑下界，调用方用完关闭连接。"""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute('SELECT line_no,activity,unit,source_unit,quantity,factor,emission,department '
                    f'FROM recon_lines WHERE job_id={self.placeholder} AND side={self.placeholder} '
                    f'AND line_no>={self.placeholder} ORDER BY line_no',
                    (job_id, side, min_line_no))
        return conn, cur

    # ---------- 认领 / 心跳 / fence ----------
    def claim(self, worker_id: str, lease_seconds: float, now: Optional[float] = None):
        """认领 queued 任务，或接管租约过期的 running 任务（epoch+1）。"""
        now = time.time() if now is None else now
        with self._connect() as conn:
            cur = conn.cursor()
            lock_clause = ' FOR UPDATE SKIP LOCKED' if self.dialect == 'postgres' else ''
            cur.execute(
                "SELECT id,status,epoch,attempt_no FROM recon_jobs "
                "WHERE status='queued' OR (status='running' AND lease_expires<"
                f"{self.placeholder}) ORDER BY id LIMIT 1{lock_clause}", (now,))
            found = cur.fetchone()
            if not found:
                conn.commit()
                return None
            job_id, _status, epoch, attempt_no = found
            new_epoch = epoch + 1
            new_attempt = attempt_no + 1
            lease = now + lease_seconds
            cur.execute(
                "UPDATE recon_jobs SET status=" + self.placeholder + ',epoch=' +
                self.placeholder + ',attempt_no=' + self.placeholder + ',worker_id=' +
                self.placeholder + ',lease_expires=' + self.placeholder +
                ',updated_at=' + self.placeholder + ' WHERE id=' + self.placeholder,
                ('running', new_epoch, new_attempt, worker_id, lease, now, job_id))
            cur.execute(
                'INSERT INTO recon_attempts(job_id,attempt_no,worker_id,epoch,started_at) '
                f'VALUES({self.ph(5)})',
                (job_id, new_attempt, worker_id, new_epoch, now))
            conn.commit()
            return {'job_id': job_id, 'epoch': new_epoch, 'attempt_no': new_attempt}

    def heartbeat(self, job_id: int, epoch: int, lease_seconds: float,
                  now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute('UPDATE recon_jobs SET lease_expires=' + self.placeholder +
                        ',updated_at=' + self.placeholder +
                        ' WHERE id=' + self.placeholder + ' AND epoch=' + self.placeholder +
                        " AND status='running'", (now + lease_seconds, now, job_id, epoch))
            if cur.rowcount != 1:
                conn.rollback()
                raise FencedError(f'心跳被拒：任务 {job_id} epoch {epoch} 已失效')
            conn.commit()

    def save_progress(self, job_id: int, epoch: int, next_cursor: int,
                      numerators: dict, total_base, total_current,
                      missing_base: int, missing_current: int,
                      lease_expires: float, now: Optional[float] = None) -> None:
        """整份累加器状态覆盖落库（非增量），配合游标实现 exactly-once。"""
        now = time.time() if now is None else now
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                'UPDATE recon_jobs SET cursor_no=' + self.placeholder + ',numerators=' +
                self.placeholder + ',total_base=' + self.placeholder + ',total_current=' +
                self.placeholder + ',missing_base=' + self.placeholder +
                ',missing_current=' + self.placeholder + ',lease_expires=' + self.placeholder +
                ',updated_at=' + self.placeholder +
                ' WHERE id=' + self.placeholder + ' AND epoch=' + self.placeholder +
                " AND status='running'",
                (next_cursor, json.dumps(numerators, ensure_ascii=False, sort_keys=True),
                 str(total_base), str(total_current), missing_base, missing_current,
                 lease_expires, now, job_id, epoch))
            if cur.rowcount != 1:
                conn.rollback()
                raise FencedError(f'进度写入被拒：任务 {job_id} epoch {epoch} 已失效')
            conn.commit()

    def publish(self, job_id: int, epoch: int, result: dict, frozen_basis: dict,
                total_pairs: int, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE recon_jobs SET status='completed',result=" + self.placeholder +
                ',frozen_basis=' + self.placeholder + ',total_pairs=' + self.placeholder +
                ',updated_at=' + self.placeholder +
                ' WHERE id=' + self.placeholder + ' AND epoch=' + self.placeholder +
                " AND status='running'",
                (json.dumps(result, ensure_ascii=False),
                 json.dumps(frozen_basis, ensure_ascii=False), total_pairs, now,
                 job_id, epoch))
            if cur.rowcount != 1:
                conn.rollback()
                raise FencedError(f'发布被拒：任务 {job_id} epoch {epoch} 已失效')
            cur.execute('INSERT INTO recon_audit(at,actor,action,detail) '
                        f'VALUES({self.ph(4)})',
                        (now, 'worker', 'recon.completed', str(job_id)))
            cur.execute('UPDATE recon_attempts SET ended_at=' + self.placeholder +
                        ",outcome='succeeded' WHERE job_id=" + self.placeholder +
                        ' AND epoch=' + self.placeholder, (now, job_id, epoch))
            conn.commit()

    def reject(self, job_id: int, epoch: int, code: str, message: str,
               details: dict, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE recon_jobs SET status='rejected',reject_code=" + self.placeholder +
                ',reject_message=' + self.placeholder + ',reject_details=' + self.placeholder +
                ',updated_at=' + self.placeholder +
                ' WHERE id=' + self.placeholder + ' AND epoch=' + self.placeholder,
                (code, message, json.dumps(details, ensure_ascii=False), now, job_id, epoch))
            if cur.rowcount != 1:
                conn.rollback()
                raise FencedError(f'拒算状态写入被拒：任务 {job_id} epoch {epoch} 已失效')
            cur.execute('INSERT INTO recon_audit(at,actor,action,detail) '
                        f'VALUES({self.ph(4)})',
                        (now, 'worker', 'recon.rejected', f'{job_id}:{code}'))
            cur.execute('UPDATE recon_attempts SET ended_at=' + self.placeholder +
                        ",outcome='rejected' WHERE job_id=" + self.placeholder +
                        ' AND epoch=' + self.placeholder, (now, job_id, epoch))
            conn.commit()

    def fail(self, job_id: int, epoch: int, message: str, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE recon_jobs SET status='failed',reject_message=" + self.placeholder +
                ',updated_at=' + self.placeholder +
                ' WHERE id=' + self.placeholder + ' AND epoch=' + self.placeholder,
                (message, now, job_id, epoch))
            if cur.rowcount == 1:
                cur.execute('UPDATE recon_attempts SET ended_at=' + self.placeholder +
                            ",outcome='failed' WHERE job_id=" + self.placeholder +
                            ' AND epoch=' + self.placeholder, (now, job_id, epoch))
            conn.commit()

    def close_attempt(self, job_id: int, epoch: int, outcome: str, detail: str) -> None:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute('UPDATE recon_attempts SET ended_at=' + self.placeholder +
                        ',outcome=' + self.placeholder + ',detail=' + self.placeholder +
                        ' WHERE job_id=' + self.placeholder + ' AND epoch=' + self.placeholder,
                        (time.time(), outcome, detail, job_id, epoch))
            conn.commit()

    def save_basis(self, job_id: int, frozen_basis: dict) -> None:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute('UPDATE recon_jobs SET frozen_basis=' + self.placeholder +
                        ' WHERE id=' + self.placeholder,
                        (json.dumps(frozen_basis, ensure_ascii=False), job_id))
            conn.commit()

    def attempts(self, job_id: int) -> list:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute('SELECT job_id,attempt_no,worker_id,epoch,started_at,ended_at,'
                        'outcome,detail FROM recon_attempts WHERE job_id=' +
                        self.placeholder + ' ORDER BY attempt_no', (job_id,))
            cols = ('job_id', 'attempt_no', 'worker_id', 'epoch', 'started_at',
                    'ended_at', 'outcome', 'detail')
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def audits(self) -> list:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute('SELECT id,at,actor,action,detail FROM recon_audit ORDER BY id')
            return [dict(zip(('id', 'at', 'actor', 'action', 'detail'), r)) for r in cur.fetchall()]
