#!/usr/bin/env bash
# 端到端冒烟脚本：需服务已启动（默认 http://localhost:8000）
# 用法：BASE_URL=http://localhost:8000 ./scripts/smoke.sh
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"

say()  { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }
post() { curl -sS -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"; }
check() { # check <json> <python 断言表达式>
  python3 -c "
import sys, json
d = json.loads('''$1''')
assert ($2), '断言失败: $2  实际: ' + json.dumps(d, ensure_ascii=False)
print('OK: $2')
"
}

say "0. 健康检查"
curl -sf "$BASE/health" && echo

say "1. 导入凭证（VIP-001 可入场 2 次）"
post /credentials/import '{
  "credentials": [
    {"credential_no": "VIP-001",
     "valid_from": "2026-10-01T00:00:00Z",
     "valid_to":   "2026-10-31T23:59:59Z",
     "max_entries": 2}
  ]
}' && echo

say "2. 终端 T1 上报 3 条（第 3 条应超次）"
R=$(post /reports '{
  "records": [
    {"terminal_id": "T1", "seq": 1, "credential_no": "VIP-001", "verified_at": "2026-10-07T10:00:00Z"},
    {"terminal_id": "T1", "seq": 2, "credential_no": "VIP-001", "verified_at": "2026-10-07T11:00:00Z"},
    {"terminal_id": "T1", "seq": 3, "credential_no": "VIP-001", "verified_at": "2026-10-07T12:00:00Z"}
  ]
}')
echo "$R" | python3 -m json.tool
check "$R" "[r['status'] for r in d['results']] == ['VALID', 'VALID', 'INVALID']"
check "$R" "d['results'][2]['reason'] == 'OVER_QUOTA'"

say "3. 同内容重传（幂等，全部 DUPLICATE）"
R=$(post /reports '{
  "records": [
    {"terminal_id": "T1", "seq": 1, "credential_no": "VIP-001", "verified_at": "2026-10-07T10:00:00Z"},
    {"terminal_id": "T1", "seq": 2, "credential_no": "VIP-001", "verified_at": "2026-10-07T11:00:00Z"}
  ]
}')
echo "$R" | python3 -m json.tool
check "$R" "all(r['outcome'] == 'DUPLICATE' for r in d['results'])"

say "4. 同终端同序号不同内容（应 409 且整批不写入）"
CODE=$(curl -sS -o /tmp/conflict.json -w '%{http_code}' -X POST "$BASE/reports" \
  -H 'Content-Type: application/json' -d '{
  "records": [
    {"terminal_id": "T1", "seq": 1, "credential_no": "VIP-001", "verified_at": "2026-10-07T10:00:01Z"},
    {"terminal_id": "T1", "seq": 99, "credential_no": "VIP-001", "verified_at": "2026-10-07T13:00:00Z"}
  ]
}')
cat /tmp/conflict.json | python3 -m json.tool
[ "$CODE" = "409" ] && echo "OK: HTTP 409"

say "5. 迟到记录（09:30，比 seq1 更早）触发重算：seq3 维持超次，seq2 被顶掉"
R=$(post /reports '{
  "records": [
    {"terminal_id": "T2", "seq": 1, "credential_no": "VIP-001", "verified_at": "2026-10-07T09:30:00Z"}
  ]
}')
echo "$R" | python3 -m json.tool
check "$R" "any(c['terminal_id']=='T1' and c['seq']==2 and c['new']['reason']=='OVER_QUOTA' for c in d['changed'])"

say "6. 查询凭证当前判定与剩余次数"
R=$(curl -sS "$BASE/credentials/VIP-001")
echo "$R" | python3 -m json.tool
check "$R" "d['remaining_entries'] == 0"
check "$R" "[r['status'] for r in d['records']] == ['VALID', 'VALID', 'INVALID', 'INVALID']"

say "7. 不在名单的凭证"
post /reports '{"records": [
  {"terminal_id": "T9", "seq": 1, "credential_no": "GHOST-1", "verified_at": "2026-10-07T10:00:00Z"}
]}' && echo
R=$(curl -sS "$BASE/credentials/GHOST-1")
check "$R" "d['in_list'] == False and d['records'][0]['reason'] == 'NOT_IN_LIST'"
echo "OK: NOT_IN_LIST"

say "全部冒烟检查通过"
