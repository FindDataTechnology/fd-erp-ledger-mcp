-- 对账任务重启续跑（change: optimize-erp-solution 任务 6.5）。
--
-- 背景：对账 worker 是进程内 daemon 线程，任务状态虽在 DB，但「重排」需要原始请求
-- （左右源规格 + retry_task_id）——旧 schema 只存状态不存请求，重启后除了标
-- interrupted 别无选择（这正是「静默留 interrupted」的根因）。
--
-- 本迁移只加一列 request JSONB（幂等）：
--   {left: {...}, right: {...}, retry_task_id: "...", enqueued_at: "..."}
-- 存量行 request 为 NULL：无法续跑，启动处置时如实标 interrupted 并把原因写进
-- 健康输出（不静默，也不伪造一次「重排成功」）。
ALTER TABLE recon_reports ADD COLUMN IF NOT EXISTS request JSONB;

COMMENT ON COLUMN recon_reports.request IS
    '任务原始请求快照（左右源规格 + retry_task_id）；重启续跑/重排的唯一依据。'
    '存量行（0006 之前创建）为 NULL，只能显式标 interrupted 并在健康输出可见。';
