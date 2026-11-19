# 002 两期差异归因

## 新增表（SQLAlchemy 不托管，由 `src/reconciliation/store.py` 原始 SQL 建表）

- `recon_jobs`：归因任务主表。`(base_run_id,current_run_id)` 唯一；含状态
  queued/running/completed/failed/rejected、fencing 代次 `epoch`、尝试序号
  `attempt_no`、租约 `lease_expires`、续跑游标 `cursor_no`、整份累加器
  `numerators`（(部门,维度) 的 6 倍分子 JSON）、两期重算/原报告总量、
  结果与冻结依据 JSON。
- `recon_lines`：冻结底稿分片表，主键 `(job_id,side,line_no)`，
  配对时按 line_no 双游标归并，百万行不整体载入。
- `recon_attempts`：每次认领/接管一条，记录 worker_id、epoch、起止与结果。
- `recon_audit`：发起、完成、拒算等动作留痕。

## 既有表变更

- `carbon_runs.snapshot.sources[*]` 新增冻结字段 `line_no`（提交时按底稿
  顺序赋予 1..N）。核算前在 `calculation.py` 剥离，不影响既有校验与结果。
- `carbon_runs.result.summary` 新增 `核算kgCO2e精确`（Decimal 字符串），
  作为归因对账的原报告总量权威值。

## 存储介质

- 生产：PostgreSQL（与业务库同 DATABASE_URL，psycopg）。
- 本地/测试：设置 `RECON_SQLITE_URL=<文件路径>` 即切换 SQLite，
  两套 DDL 在 `store.py` 内按方言生成。

升级方式：应用启动/worker 启动时 `SqlStore.create_schema()` 幂等建表；
旧报告无 `line_no`，发起归因时按 `missing_number` 拒算并提示重新提交核算。
