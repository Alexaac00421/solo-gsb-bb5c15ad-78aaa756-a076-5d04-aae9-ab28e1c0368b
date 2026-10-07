# 展会断网闸机 · 入场凭证对账服务

展会现场闸机断网本地工作，恢复联网后批量上报核销记录。本服务负责：

- 导入**凭证名单**（凭证号、有效时段、可入场次数）；
- 接收终端批量上报的 **(终端号, 递增序号, 凭证号, 核销时间)**；
- 同终端号+序号的**同内容重传幂等**；内容不同则**整批拒绝、不写入任何记录**；
- 批次允许**乱序到达**；每张凭证统一按
  **核销时间 → 终端号 → 序号** 排序分配有效次数；
- 对**不在名单**、**时段外**、**超次**的记录分别给出无效原因；
- **迟到记录**到达后重算该凭证，并返回所有被**改判**的记录；
- 查询接口返回每条记录的**当前判定**与凭证**剩余次数**；
- 并发上报通过事务级咨询锁串行化，结果与同一排序规则严格一致。

技术栈：Python 3.12 / FastAPI / uvicorn / psycopg3 / PostgreSQL 16，容器化交付。

---

## 1. 运行

```bash
# 构建并启动（db 健康后 app 自动启动，启动时幂等建表）
docker compose up --build

# 运行端到端示例（乱序/幂等/冲突/迟到改判/并发，含断言）
python3 demo/demo.py
```

服务地址：`http://localhost:8080`
交互式文档（Swagger）：`http://localhost:8080/docs`
健康检查：`GET http://localhost:8080/healthz`

停止并清理数据卷：

```bash
docker compose down -v
```

### 本地不使用容器运行

```bash
pip install -r requirements.txt
# 需自行准备 PostgreSQL，然后通过环境变量指向它
export DATABASE_URL=postgresql://expo:expo@localhost:5432/expo
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

服务首次启动会等待数据库就绪并自动执行 `db/schema.sql`（幂等），无需手动初始化。

---

## 2. 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://expo:expo@localhost:5432/expo` | 数据库连接串；Compose 中自动指向 `db` 服务 |
| `APP_HOST` / `APP_PORT` | `0.0.0.0` / `8080` | 服务监听地址与容器内端口 |
| `DB_POOL_MIN` / `DB_POOL_MAX` | `2` / `10` | psycopg 连接池大小 |
| `DB_CONNECT_TIMEOUT` | `10` | 取连接超时（秒） |
| `DB_STARTUP_WAIT` | `60` | 启动时等待数据库就绪的最长时间（秒） |
| `MAX_BATCH_SIZE` | `2000` | 单次导入/单批上报的最大条数 |
| `LOCK_NAMESPACE` | `88001` | 凭证咨询锁的固定命名空间（int4） |
| `POSTGRES_USER/PASSWORD/DB` | `expo/expo/expo` | Compose 中数据库账户与库名 |
| `APP_PORT_PUBLISH` / `POSTGRES_PORT_PUBLISH` | `8080` / `5432` | 宿主机发布端口，端口冲突时覆盖即可 |

时间字段均使用 ISO 8601；不带时区偏移的时间戳按 **UTC** 处理，终端上报请直接携带本地时区偏移。有效时段为闭区间 `[valid_from, valid_to]`。

---

## 3. 判定规则

每张凭证的全部记录按 `(verified_at, terminal_no, seq)` 升序排序后：

| 判定 | reason | 含义 |
| --- | --- | --- |
| `valid` | `null` | 凭证存在、核销时间在有效时段内、排序位次 ≤ 可入场次数 |
| `invalid` | `not_in_list` | 凭证号不在名单（不占用任何名额） |
| `invalid` | `outside_window` | 核销时间早于 `valid_from` 或晚于 `valid_to`（不占用名额） |
| `invalid` | `exhausted` | 时段内但排序位次超出可入场次数 |

任何新批次写入后，服务都会对批次涉及的每张凭证做**全量重算**，因此迟到的更早记录会把原先占用名额的记录改判为 `exhausted`，反之亦然。改判记录通过上报响应的 `changed` 字段返回（含改判前后判定与最新剩余次数）。

---

## 4. HTTP API 调用示例

### 4.1 导入凭证

```bash
curl -sS -X POST http://localhost:8080/api/v1/credentials/import \
  -H 'Content-Type: application/json' \
  -d '{
    "credentials": [
      {
        "code": "VIP-001",
        "valid_from": "2026-10-07T08:00:00Z",
        "valid_to":   "2026-10-07T18:00:00Z",
        "max_entries": 2
      }
    ]
  }'
```

响应（导入是 upsert，重复导入即更新时段/次数并重算已有记录，改判记录在 `changed` 中返回）：

```json
{
  "credentials": [
    {"code": "VIP-001", "valid_from": "...", "valid_to": "...",
     "max_entries": 2, "used_entries": 0, "remaining_entries": 2}
  ],
  "changed": []
}
```

### 4.2 批量上报（可乱序）

```bash
curl -sS -X POST http://localhost:8080/api/v1/reports/batch \
  -H 'Content-Type: application/json' \
  -d '{
    "records": [
      {"terminal_no": "T-A", "seq": 2, "credential_code": "VIP-001",
       "verified_at": "2026-10-07T09:10:00Z"},
      {"terminal_no": "T-A", "seq": 1, "credential_code": "VIP-001",
       "verified_at": "2026-10-07T09:05:00Z"}
    ]
  }'
```

响应：

```json
{
  "inserted": [ /* 本批首次写入的记录，携带最终判定与剩余次数 */ ],
  "duplicate": [ /* 同终端号+序号同内容的幂等重传 */ ],
  "changed": [   /* 因本批迟到数据而被改判的历史记录 */ ],
  "rejected": false
}
```

记录对象示例：

```json
{
  "terminal_no": "T-A", "seq": 1, "credential_code": "VIP-001",
  "verified_at": "2026-10-07T09:05:00Z",
  "judgment": "valid", "reason": null, "remaining_entries": 1
}
```

- **同内容重传**（凭证号、核销时间都一致）：HTTP 200，记录出现在 `duplicate`，不重复计数。
- **内容不同**（如同一 `T-A/1` 改了核销时间或凭证号）：HTTP **409**，整批拒绝、**零写入**：

```json
{
  "rejected": true,
  "error": "相同终端号+序号的记录内容不一致，整批拒绝",
  "conflicts": [
    {"terminal_no": "T-A", "seq": 1,
     "existing": {"credential_code": "VIP-001", "verified_at": "2026-10-07T09:05:00Z"},
     "received": {"credential_code": "VIP-001", "verified_at": "2026-10-07T10:40:00Z"}}
  ]
}
```

### 4.3 迟到记录与改判

凭证只有 1 个名额、`T-C/1 @09:30` 已占用时，再上报一条更早的迟到记录：

```bash
curl -sS -X POST http://localhost:8080/api/v1/reports/batch \
  -H 'Content-Type: application/json' \
  -d '{"records":[{"terminal_no":"T-D","seq":7,
      "credential_code":"VIP-002","verified_at":"2026-10-07T09:00:00Z"}]}'
```

```json
{
  "inserted": [{"terminal_no": "T-D", "seq": 7, "judgment": "valid", ...}],
  "duplicate": [],
  "changed": [
    {"terminal_no": "T-C", "seq": 1,
     "old_judgment": "valid", "old_reason": null,
     "new_judgment": "invalid", "new_reason": "exhausted",
     "remaining_entries": 0, ...}
  ],
  "rejected": false
}
```

### 4.4 查询

```bash
# 某张凭证的全部记录（按核销时间、终端号、序号排序）
curl -sS 'http://localhost:8080/api/v1/records?credential_code=VIP-001&limit=200'

# 某个终端的记录
curl -sS 'http://localhost:8080/api/v1/records?terminal_no=T-A'

# 凭证当前用量与剩余次数
curl -sS http://localhost:8080/api/v1/credentials/VIP-001
```

`/records` 每条记录都带 `judgment`、`reason` 和该凭证当前 `remaining_entries`（凭证不在名单时为 `null`）。

---

## 5. 并发一致性说明

- 每张凭证由 `pg_advisory_xact_lock(<命名空间>, hashtext(凭证号))` 保护，事务按凭证号**升序**取锁，多凭证批次之间不会死锁；
- 同一凭证的上报、导入因此串行化，重算只基于 `(verified_at, terminal_no, seq)` 的确定性顺序；
- `(terminal_no, seq)` 上的唯一约束兜底并发首报：撞键的事务整体回滚并重开一次，随后按“同内容幂等 / 异内容 409”处理；
- 尚未导入的凭证同样按凭证号取锁，“先上报 not_in_list、后导入名单”与“先导名单、后上报”两种时序的最终状态一致。

## 6. 目录结构

```
app/            FastAPI 服务（main 入口、service 对账逻辑、db 连接池、schemas 模型）
db/schema.sql   建表脚本（启动时幂等执行）
demo/demo.py    纯标准库的端到端调用示例与场景自检
Dockerfile / docker-compose.yml  容器化交付
```
