#!/usr/bin/env python3
"""端到端调用示例 + 场景自检（乱序 / 幂等 / 冲突整批拒绝 / 迟到改判 / 并发）。

用法（服务已通过 docker compose 启动）：
    python3 demo/demo.py
    BASE_URL=http://192.168.1.10:8080 python3 demo/demo.py

脚本仅依赖标准库；所有断言失败会以非零退出码结束。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

BASE_URL = os.getenv("BASE_URL", "http://localhost:8080").rstrip("/")

passed = 0


def check(name: str, cond: bool, detail=None) -> None:
    global passed
    if cond:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail or ''}")
        sys.exit(1)


def call(method: str, path: str, payload=None, expect_error: int | None = None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if expect_error is not None:
                check(f"{method} {path} 应返回 {expect_error}", False, body)
                sys.exit(1)
            return body
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode("utf-8"))
        if expect_error is not None and exc.code == expect_error:
            return body
        raise


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def view_map(batch_result: str = "inserted", result=None):
    return {(r["terminal_no"], r["seq"]): r for r in result[batch_result]}


def main() -> None:
    now = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)
    start = now - timedelta(hours=1)
    end = now + timedelta(hours=8)

    print("== 1. 健康检查 ==")
    check("healthz", call("GET", "/healthz")["status"] == "ok")

    print("== 2. 导入凭证 ==")
    imp = call(
        "POST",
        "/api/v1/credentials/import",
        {
            "credentials": [
                {"code": "VIP-001", "valid_from": iso(start), "valid_to": iso(end), "max_entries": 2},
                {"code": "VIP-002", "valid_from": iso(start), "valid_to": iso(end), "max_entries": 1},
                {"code": "VIP-003", "valid_from": iso(now + timedelta(hours=2)),
                 "valid_to": iso(end), "max_entries": 3},
            ]
        },
    )
    rem = {c["code"]: c["remaining_entries"] for c in imp["credentials"]}
    check("导入后剩余次数 2/1/3", rem == {"VIP-001": 2, "VIP-002": 1, "VIP-003": 3}, rem)

    print("== 3. 批量乱序上报（先到的核销时间反而更晚）==")
    b1 = call(
        "POST",
        "/api/v1/reports/batch",
        {
            "records": [
                # 序号递增但按核销时间看，9:10 排在 9:05 前面
                {"terminal_no": "T-A", "seq": 2, "credential_code": "VIP-001",
                 "verified_at": iso(now + timedelta(minutes=10))},
                {"terminal_no": "T-A", "seq": 1, "credential_code": "VIP-001",
                 "verified_at": iso(now + timedelta(minutes=5))},
                # 凭证不存在
                {"terminal_no": "T-A", "seq": 3, "credential_code": "NOPE",
                 "verified_at": iso(now)},
                # 时段外（VIP-003 11:00 才开始）
                {"terminal_no": "T-B", "seq": 1, "credential_code": "VIP-003",
                 "verified_at": iso(now)},
            ]
        },
    )
    m = view_map("inserted", b1)
    check("9:05 记录 valid", m[("T-A", 1)]["judgment"] == "valid", m[("T-A", 1)])
    check("9:10 记录 valid（名额 2 个）", m[("T-A", 2)]["judgment"] == "valid", m[("T-A", 2)])
    check("不在名单 -> invalid/not_in_list",
          m[("T-A", 3)]["judgment"] == "invalid" and m[("T-A", 3)]["reason"] == "not_in_list",
          m[("T-A", 3)])
    check("时段外 -> invalid/outside_window",
          m[("T-B", 1)]["judgment"] == "invalid" and m[("T-B", 1)]["reason"] == "outside_window",
          m[("T-B", 1)])
    check("VIP-001 剩余 0", m[("T-A", 1)]["remaining_entries"] == 0, m[("T-A", 1)])
    check("不在名单的剩余次数为 null", m[("T-A", 3)]["remaining_entries"] is None)

    print("== 4. 相同终端+序号 同内容重传 => 幂等 ==")
    b2 = call(
        "POST",
        "/api/v1/reports/batch",
        {
            "records": [
                {"terminal_no": "T-A", "seq": 1, "credential_code": "VIP-001",
                 "verified_at": iso(now + timedelta(minutes=5))},
                {"terminal_no": "T-A", "seq": 2, "credential_code": "VIP-001",
                 "verified_at": iso(now + timedelta(minutes=10))},
            ]
        },
    )
    check("幂等重传不新增", len(b2["inserted"]) == 0, b2["inserted"])
    check("两条都落在 duplicate", len(b2["duplicate"]) == 2, b2["duplicate"])
    check("幂等重传不改判", len(b2["changed"]) == 0, b2["changed"])

    print("== 5. 相同终端+序号 内容被篡改 => 409 整批拒绝不写入 ==")
    rejected = call(
        "POST",
        "/api/v1/reports/batch",
        {
            "records": [
                # seq=1 已有，但核销时间被改
                {"terminal_no": "T-A", "seq": 1, "credential_code": "VIP-001",
                 "verified_at": iso(now + timedelta(minutes=99))},
                # 同批里夹带一条全新记录，也不得写入
                {"terminal_no": "T-B", "seq": 99, "credential_code": "VIP-002",
                 "verified_at": iso(now)},
            ]
        },
        expect_error=409,
    )
    check("409 标记 rejected", rejected["rejected"] is True, rejected)
    after = call("GET", "/api/v1/records?terminal_no=T-B&limit=50")
    check("整批未写入：T-B 只有之前的 1 条",
          {r["seq"] for r in after["records"]} == {1}, after)

    print("== 6. 超次：VIP-002 只有 1 次，第 2 条 exhausted ==")
    call(
        "POST",
        "/api/v1/reports/batch",
        {
            "records": [
                {"terminal_no": "T-C", "seq": 1, "credential_code": "VIP-002",
                 "verified_at": iso(now + timedelta(minutes=30))},
                {"terminal_no": "T-C", "seq": 2, "credential_code": "VIP-002",
                 "verified_at": iso(now + timedelta(minutes=40))},
            ]
        },
    )
    rs = call("GET", "/api/v1/records?credential_code=VIP-002&limit=10")["records"]
    j = {r["terminal_no"] + "#" + str(r["seq"]): (r["judgment"], r["reason"]) for r in rs}
    check("第 1 条 valid", j["T-C#1"] == ("valid", None), j)
    check("第 2 条 invalid/exhausted",
          j["T-C#2"] == ("invalid", "exhausted"), j)

    print("== 7. 迟到记录到达 => 重算并返回被改判的记录 ==")
    # T-C#1 核销时间 9:30 正占着 VIP-002 唯一名额；
    # 迟到一条 9:00（更早）的核销，应把 T-C#1 挤成 exhausted。
    b3 = call(
        "POST",
        "/api/v1/reports/batch",
        {
            "records": [
                {"terminal_no": "T-D", "seq": 7, "credential_code": "VIP-002",
                 "verified_at": iso(now)},
            ]
        },
    )
    check("迟到记录自身 valid",
          b3["inserted"][0]["judgment"] == "valid", b3["inserted"])
    changed_keys = {(c["terminal_no"], c["seq"]): c for c in b3["changed"]}
    check("T-C#1 被改判 valid->invalid/exhausted",
          ("T-C", 1) in changed_keys
          and changed_keys[("T-C", 1)]["old_judgment"] == "valid"
          and changed_keys[("T-C", 1)]["new_reason"] == "exhausted",
          b3["changed"])
    rs = call("GET", "/api/v1/records?credential_code=VIP-002&limit=10")["records"]
    state = {(r["terminal_no"], r["seq"]): (r["judgment"], r["reason"], r["remaining_entries"])
             for r in rs}
    check("查询可见最新判定：T-D#7 valid", state[("T-D", 7)][0] == "valid", state)
    check("查询可见最新判定：T-C#1 exhausted", state[("T-C", 1)] == ("invalid", "exhausted", 0), state)

    print("== 8. 凭证查询接口 ==")
    v = call("GET", "/api/v1/credentials/VIP-002")
    check("VIP-002 used=1 remaining=0",
          v["used_entries"] == 1 and v["remaining_entries"] == 0, v)

    print("== 9. 并发上报：5 个终端同时抢 VIP-003 的 3 个时段内名额 ==")
    # VIP-003 11:00 起生效，使用 11:0x 的核销时间。
    def post_one(i: int):
        return call(
            "POST",
            "/api/v1/reports/batch",
            {
                "records": [
                    {"terminal_no": f"T-K{i}", "seq": 1,
                     "credential_code": "VIP-003",
                     "verified_at": iso(now + timedelta(hours=2, minutes=i))}
                ]
            },
        )

    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = [pool.submit(post_one, i) for i in range(1, 6)]
        statuses = [f.result() for f in as_completed(futs)]
    check("5 个并发上报全部成功返回",
          all(r["rejected"] is False for r in statuses), statuses)

    rs = call("GET", "/api/v1/records?credential_code=VIP-003&limit=50")["records"]
    valid = [r for r in rs if r["judgment"] == "valid"]
    exhausted = [r for r in rs if r["reason"] == "exhausted"]
    check("恰好 3 条 valid", len(valid) == 3, [r["terminal_no"] for r in valid])
    check("恰好 2 条 exhausted（更早的 T-B#1 仍是 outside_window）",
          len(exhausted) == 2, [r["terminal_no"] for r in exhausted])
    winners = sorted(r["terminal_no"] for r in valid)
    # 核销时间 11:01..11:05，取最早三个：T-K1/T-K2/T-K3
    check("名额按核销时间排序分配给最早三条",
          winners == ["T-K1", "T-K2", "T-K3"], winners)
    check("所有记录视角的剩余次数一致为 0",
          {r["remaining_entries"] for r in rs if r["credential_code"] == "VIP-003"} == {0})

    print(f"\n全部 {passed} 项检查通过 ✔")


if __name__ == "__main__":
    main()
