# 展会闸机入场凭证对账服务

面向展会断网闸机场景的入场凭证对账服务。终端离线核销、恢复联网后批量上报，
服务端按统一排序规则对每张凭证分配有效入场次数，给出每条记录的当前判定与无效原因。

- 技术栈：Python 3.12 · FastAPI · PostgreSQL 16（psycopg 3）
- 交付物：HTTP API、Dockerfile、Compose 配置（启动即完成数据库初始化）

## 判定规则

同一凭证的全部核销记录按 **（核销时间， 终端号， 序号）** 升序排列，依次分配有效次数：

| 情形 | status | reason |
|---|---|---|
| 凭证不在名单 | `INVALID` | `NOT_IN_LIST` |
| 核销时间在有效时段外（不占用次数） | `INVALID` | `OUT_OF_WINDOW` |
| 有效次数已用完 | `INVALID` | `OVER_QUOTA` |
| 其余 | `VALID`（分配 `entry_no` 入场次序） | — |

判定只依赖记录集合与排序规则，与到达顺序无关：乱序、迟到、并发上报的最终结果，
与按同一排序规则串行处理完全一致。迟到记录插入后会触发该凭证全量重算，
响应的 `changed` 字段列出所有被改判的记录。

## 快速开始

```bash
docker compose up --build
```

- 服务地址：**http://localhost:8000**
- 交互式接口文档：http://localhost:8000/docs （Swagger UI）
- 健康检查：`curl http://localhost:8000/health`

启动流程：Postgres 健康检查通过后 app 启动，app 启动时自动等待数据库就绪并执行幂等建表 DDL。

### 冒烟验证

服务启动后可直接运行内置场景脚本（覆盖导入、上报、幂等重传、409 冲突、迟到改判、查询）：

```bash
./scripts/smoke.sh
```

> 冒烟脚本基于全新数据库设计，重复运行前请先 `docker compose down -v` 清空数据。

## 配置项

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `APP_PORT` | `8000` | 宿主机映射的服务端口（compose） |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `gate` / `gate` / `gate` | 数据库账号（compose） |
| `POSTGRES_PORT` | `5432` | 宿主机映射的数据库端口（compose） |
| `DATABASE_URL` | `postgresql://gate:gate@localhost:5432/gate` | 应用连接串（compose 中自动指向 db 服务） |
| `POOL_MIN_SIZE` / `POOL_MAX_SIZE` | `1` / `10` | 数据库连接池大小 |
| `DB_CONNECT_RETRIES` / `DB_CONNECT_DELAY` | `30` / `1.0` | 启动时等待数据库就绪的重试次数与间隔（秒） |
| `SUBMIT_RETRY_ATTEMPTS` | `3` | 上报批次遇瞬态冲突（唯一键竞争/死锁）的自动重试次数 |

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/credentials/import` | 批量导入/更新凭证（凭证号、有效时段、可入场次数） |
| `POST` | `/reports` | 终端批量上报核销记录（幂等、冲突拒绝、迟到重算） |
| `GET` | `/credentials/{credential_no}` | 查询凭证：每条记录当前判定 + 剩余次数 |
| `GET` | `/health` | 健康检查 |

时间字段均为 ISO 8601；不带时区时按 UTC 处理。有效时段为闭区间。

### 1. 导入凭证

```bash
curl -X POST http://localhost:8000/credentials/import \
  -H 'Content-Type: application/json' \
  -d '{
    "credentials": [
      {"credential_no": "VIP-001",
       "valid_from": "2026-10-01T00:00:00Z",
       "valid_to":   "2026-10-31T23:59:59Z",
       "max_entries": 2}
    ]
  }'
```

响应（`changed` 为导入后名单变化导致的历史记录改判）：

```json
{"imported": 1, "changed": []}
```

重复导入同一凭证号为**更新**（upsert），并自动重算该凭证全部记录。

### 2. 批量上报核销记录

```bash
curl -X POST http://localhost:8000/reports \
  -H 'Content-Type: application/json' \
  -d '{
    "records": [
      {"terminal_id": "T1", "seq": 1, "credential_no": "VIP-001", "verified_at": "2026-10-07T10:00:00Z"},
      {"terminal_id": "T1", "seq": 2, "credential_no": "VIP-001", "verified_at": "2026-10-07T11:00:00Z"},
      {"terminal_id": "T1", "seq": 3, "credential_no": "VIP-001", "verified_at": "2026-10-07T12:00:00Z"}
    ]
  }'
```

响应（每条记录给出当前判定；第 3 条超次）：

```json
{
  "accepted": true,
  "results": [
    {"terminal_id": "T1", "seq": 1, "credential_no": "VIP-001", "verified_at": "2026-10-07T10:00:00Z",
     "outcome": "RECEIVED", "status": "VALID",   "reason": null,         "entry_no": 1},
    {"terminal_id": "T1", "seq": 2, "credential_no": "VIP-001", "verified_at": "2026-10-07T11:00:00Z",
     "outcome": "RECEIVED", "status": "VALID",   "reason": null,         "entry_no": 2},
    {"terminal_id": "T1", "seq": 3, "credential_no": "VIP-001", "verified_at": "2026-10-07T12:00:00Z",
     "outcome": "RECEIVED", "status": "INVALID", "reason": "OVER_QUOTA", "entry_no": null}
  ],
  "changed": []
}
```

#### 幂等重传（相同终端号 + 序号 + 相同内容）

原样重发上面的请求：全部返回 `"outcome": "DUPLICATE"`，不重复写入、不重复计数，HTTP 200。

#### 冲突拒绝（相同终端号 + 序号、不同内容）

```bash
curl -X POST http://localhost:8000/reports \
  -H 'Content-Type: application/json' \
  -d '{"records": [
    {"terminal_id": "T1", "seq": 1, "credential_no": "VIP-001", "verified_at": "2026-10-07T10:00:01Z"},
    {"terminal_id": "T1", "seq": 99, "credential_no": "VIP-001", "verified_at": "2026-10-07T13:00:00Z"}
  ]}'
```

HTTP **409**，**整批拒绝、不写入任何数据**（包括批内本无冲突的 seq=99）：

```json
{
  "detail": {
    "error": "REPORT_CONFLICT",
    "message": "相同终端号与序号的记录内容不一致，整批拒绝，未写入任何数据",
    "terminal_id": "T1",
    "seq": 1,
    "existing": {"credential_no": "VIP-001", "verified_at": "2026-10-07T10:00:00Z"},
    "received": {"credential_no": "VIP-001", "verified_at": "2026-10-07T10:00:01Z"}
  }
}
```

#### 迟到记录触发改判

批次可乱序到达。再上报一条核销时间更早的记录（另一台终端）：

```bash
curl -X POST http://localhost:8000/reports \
  -H 'Content-Type: application/json' \
  -d '{"records": [
    {"terminal_id": "T2", "seq": 1, "credential_no": "VIP-001", "verified_at": "2026-10-07T09:30:00Z"}
  ]}'
```

该凭证被重算：T2#1（09:30）与 T1#1（10:00）占满 2 次名额，T1#2 被改判为超次，
响应的 `changed` 列出被改判记录：

```json
{
  "accepted": true,
  "results": [
    {"terminal_id": "T2", "seq": 1, "outcome": "RECEIVED", "status": "VALID", "reason": null, "entry_no": 1, "...": "..."}
  ],
  "changed": [
    {"credential_no": "VIP-001", "terminal_id": "T1", "seq": 1,
     "old": {"status": "VALID", "reason": null, "entry_no": 1},
     "new": {"status": "VALID", "reason": null, "entry_no": 2}},
    {"credential_no": "VIP-001", "terminal_id": "T1", "seq": 2,
     "old": {"status": "VALID", "reason": null, "entry_no": 2},
     "new": {"status": "INVALID", "reason": "OVER_QUOTA", "entry_no": null}}
  ]
}
```

### 3. 查询凭证当前判定与剩余次数

```bash
curl http://localhost:8000/credentials/VIP-001
```

```json
{
  "credential_no": "VIP-001",
  "in_list": true,
  "valid_from": "2026-10-01T00:00:00Z",
  "valid_to": "2026-10-31T23:59:59Z",
  "max_entries": 2,
  "used_entries": 2,
  "remaining_entries": 0,
  "records": [
    {"terminal_id": "T2", "seq": 1, "verified_at": "2026-10-07T09:30:00Z", "status": "VALID",   "reason": null,         "entry_no": 1},
    {"terminal_id": "T1", "seq": 1, "verified_at": "2026-10-07T10:00:00Z", "status": "VALID",   "reason": null,         "entry_no": 2},
    {"terminal_id": "T1", "seq": 2, "verified_at": "2026-10-07T11:00:00Z", "status": "INVALID", "reason": "OVER_QUOTA", "entry_no": null},
    {"terminal_id": "T1", "seq": 3, "verified_at": "2026-10-07T12:00:00Z", "status": "INVALID", "reason": "OVER_QUOTA", "entry_no": null}
  ]
}
```

不在名单的凭证同样可查询（`in_list: false`，记录 `reason` 为 `NOT_IN_LIST`，`remaining_entries` 为 `null`）；
凭证不在名单且无任何上报记录时返回 404。

## 并发一致性

- 每张凭证的重算在事务内先取 `pg_advisory_xact_lock(hashtext(credential_no))`，同一凭证的判定串行化；
- 批次涉及多张凭证时按凭证号字典序加锁，避免交叉死锁；
- 判定仅依赖 （核销时间， 终端号， 序号） 的全量排序，与到达顺序无关，故并发上报的最终结果与按同一规则串行处理一致；
- 并发提交相同 （终端号， 序号） 产生的唯一键竞争/死锁为瞬态错误，自动回滚重试后收敛到幂等（DUPLICATE）或冲突（409）的确定结果。

## 本地开发（不用 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
# 准备一个 PostgreSQL 实例，然后：
export DATABASE_URL=postgresql://gate:gate@localhost:5432/gate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行判定核心逻辑单元测试（仅依赖标准库）：

```bash
python3 -m unittest tests.test_judgment -v
```

## 项目结构

```
app/
  main.py       # FastAPI 入口与路由
  service.py    # 业务编排：导入/上报/幂等/冲突/重算/查询
  judgment.py   # 判定核心（纯函数：排序 + 分配有效次数）
  models.py     # 请求体模型
  db.py         # 连接池与启动建表 DDL
  config.py     # 环境变量配置
tests/test_judgment.py   # 判定逻辑单元测试
scripts/smoke.sh         # 端到端冒烟脚本
Dockerfile / docker-compose.yml / requirements.txt
```
