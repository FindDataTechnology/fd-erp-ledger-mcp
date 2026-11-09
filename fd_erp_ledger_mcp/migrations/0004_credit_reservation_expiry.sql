-- 信用占用的失效时点与处置追溯（change: close-orphan-credit-reservations）
--
-- 设计约束（design.md D5）：存量行 expires_at 一律留 NULL，MUST NOT 在迁移里
-- 统一补一个未来时间——那会让最老的孤儿看起来仍在有效期内，把问题藏起来。
-- 存量行由只读报表按 created_at + 配置上限判定，逐条处置后再回填。
--
-- status 不扩枚举：'expired' 是派生判定（status='reserved' AND now() > expires_at），
-- 不落库，避免"一处按列、一处按时间"的双口径。

ALTER TABLE credit_reservations ADD COLUMN IF NOT EXISTS expires_at    TIMESTAMPTZ;
ALTER TABLE credit_reservations ADD COLUMN IF NOT EXISTS released_by   TEXT;
ALTER TABLE credit_reservations ADD COLUMN IF NOT EXISTS release_reason TEXT;

-- 可用额度求和与过期扫描都按 (tenant, customer, status, expires_at) 走。
CREATE INDEX IF NOT EXISTS credit_reservations_expiry_idx
    ON credit_reservations (tenant_id, customer_id, status, expires_at);

COMMENT ON COLUMN credit_reservations.expires_at IS
    '占用失效时点；NULL 表示存量行（迁移前写入），由报表按 created_at + 配置上限判定';
COMMENT ON COLUMN credit_reservations.released_by IS
    '释放/处置的发起方标识（可追溯要求：谁）';
COMMENT ON COLUMN credit_reservations.release_reason IS
    '释放/处置的依据（可追溯要求：凭什么）';
