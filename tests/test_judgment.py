"""判定核心逻辑单元测试（仅依赖标准库）。

运行：python3 -m unittest tests.test_judgment -v
"""
import unittest
from datetime import datetime, timedelta, timezone

from app.judgment import (
    INVALID,
    NOT_IN_LIST,
    OUT_OF_WINDOW,
    OVER_QUOTA,
    VALID,
    CredentialRule,
    RecordKey,
    judge,
)

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def rec(terminal: str, seq: int, minutes: float) -> RecordKey:
    return RecordKey(terminal, seq, T0 + timedelta(minutes=minutes))


class JudgeTest(unittest.TestCase):
    def setUp(self):
        # 有效时段覆盖 T0 前后；可入场 2 次
        self.rule = CredentialRule(
            valid_from=T0 - timedelta(hours=1),
            valid_to=T0 + timedelta(hours=10),
            max_entries=2,
        )

    @staticmethod
    def by_key(judgments):
        return {(j.terminal_id, j.seq): j for j in judgments}

    def test_not_in_list(self):
        js = judge([rec("T1", 1, 0)], None)
        self.assertEqual(js[0].status, INVALID)
        self.assertEqual(js[0].reason, NOT_IN_LIST)
        self.assertIsNone(js[0].entry_no)

    def test_out_of_window_does_not_consume_quota(self):
        js = judge([rec("T1", 1, -120), rec("T1", 2, 0), rec("T1", 3, 1)], self.rule)
        m = self.by_key(js)
        self.assertEqual(m[("T1", 1)].reason, OUT_OF_WINDOW)
        self.assertEqual(m[("T1", 2)].status, VALID)
        self.assertEqual(m[("T1", 2)].entry_no, 1)
        self.assertEqual(m[("T1", 3)].status, VALID)
        self.assertEqual(m[("T1", 3)].entry_no, 2)

    def test_over_quota_assigned_by_sort_order(self):
        # 同一时刻按 (终端号, 序号) 决胜：T1#9 先于 T2#5
        js = judge([rec("T2", 5, 0), rec("T1", 9, 0), rec("T1", 1, 1)], self.rule)
        m = self.by_key(js)
        self.assertEqual(m[("T1", 9)].entry_no, 1)
        self.assertEqual(m[("T2", 5)].entry_no, 2)
        self.assertEqual(m[("T1", 1)].status, INVALID)
        self.assertEqual(m[("T1", 1)].reason, OVER_QUOTA)

    def test_late_record_reorders_and_overturns(self):
        # 先到两条均有效；迟到一条更早的记录后，原第 2 次入场被顶为超次
        first = self.by_key(judge([rec("T1", 2, 10), rec("T1", 3, 20)], self.rule))
        self.assertEqual(first[("T1", 3)].status, VALID)

        second = self.by_key(
            judge([rec("T1", 1, 0), rec("T1", 2, 10), rec("T1", 3, 20)], self.rule)
        )
        self.assertEqual(second[("T1", 1)].entry_no, 1)
        self.assertEqual(second[("T1", 2)].entry_no, 2)
        self.assertEqual(second[("T1", 3)].status, INVALID)
        self.assertEqual(second[("T1", 3)].reason, OVER_QUOTA)
        self.assertIsNone(second[("T1", 3)].entry_no)

    def test_arrival_order_irrelevant(self):
        # 同一集合任意到达顺序，判定结果一致
        records = [rec("T2", 5, 30), rec("T1", 1, 0), rec("T1", 2, 10), rec("T2", 6, 20)]
        expect = judge(records, self.rule)
        self.assertEqual(judge(list(reversed(records)), self.rule), expect)

    def test_zero_quota(self):
        rule = CredentialRule(self.rule.valid_from, self.rule.valid_to, 0)
        js = judge([rec("T1", 1, 0)], rule)
        self.assertEqual(js[0].reason, OVER_QUOTA)

    def test_window_boundaries_inclusive(self):
        rule = CredentialRule(valid_from=T0, valid_to=T0 + timedelta(minutes=10), max_entries=5)
        js = judge([rec("T1", 1, -1), rec("T1", 2, 0), rec("T1", 3, 10), rec("T1", 4, 11)], rule)
        m = self.by_key(js)
        self.assertEqual(m[("T1", 1)].reason, OUT_OF_WINDOW)
        self.assertEqual(m[("T1", 2)].status, VALID)
        self.assertEqual(m[("T1", 3)].status, VALID)
        self.assertEqual(m[("T1", 4)].reason, OUT_OF_WINDOW)

    def test_tie_break_by_terminal_then_seq(self):
        js = judge([rec("T2", 1, 0), rec("T1", 2, 0), rec("T1", 1, 0)], self.rule)
        self.assertEqual(
            [(j.terminal_id, j.seq) for j in js],
            [("T1", 1), ("T1", 2), ("T2", 1)],
        )


if __name__ == "__main__":
    unittest.main()
