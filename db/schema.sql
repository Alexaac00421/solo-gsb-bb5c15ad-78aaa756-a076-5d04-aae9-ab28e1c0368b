-- 展会断网闸机：入场凭证对账服务 数据库结构
-- 幂等执行，应用启动时运行。

CREATE TABLE IF NOT EXISTS credentials (
    code        TEXT PRIMARY KEY,
    valid_from  TIMESTAMPTZ NOT NULL,
    valid_to    TIMESTAMPTZ NOT NULL,
    max_entries INTEGER     NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT credentials_times_ok CHECK (valid_from <= valid_to),
    CONSTRAINT credentials_entries_nonneg CHECK (max_entries >= 0)
);

-- 核销记录：以 (终端号, 递增序号) 作为终端侧上报的天然唯一键。
-- valid 状态只用于凭证存在、时段内且获得有效名额的记录。
CREATE TABLE IF NOT EXISTS gate_records (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    terminal_no   TEXT        NOT NULL,
    seq           BIGINT      NOT NULL,
    credential_code TEXT      NOT NULL,
    verified_at   TIMESTAMPTZ NOT NULL,
    judgment      TEXT        NOT NULL,
    reason        TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT gate_records_terminal_seq_uniq UNIQUE (terminal_no, seq),
    CONSTRAINT gate_records_judgment_chk CHECK (judgment IN ('valid', 'invalid')),
    CONSTRAINT gate_records_reason_chk CHECK (
        reason IS NULL OR reason IN ('not_in_list', 'outside_window', 'exhausted')
    )
);

CREATE INDEX IF NOT EXISTS idx_gate_records_cred
    ON gate_records (credential_code, verified_at, terminal_no, seq);
CREATE INDEX IF NOT EXISTS idx_gate_records_terminal
    ON gate_records (terminal_no, seq);

-- judgment / reason 取值说明：
--   valid    / NULL           凭证存在、在有效时段内、按规则分配到有效名额
--   invalid  / not_in_list    凭证号不在名单
--   invalid  / outside_window 核销时间不在凭证的 [valid_from, valid_to] 内
--   invalid  / exhausted      时段内但有效名额已满（排序在其后）
