"""验证行情 per-code 缓存：删除一个标的不会导致其余标的整批重拉（删一笔变快的关健）。

用 fake st 提供 session_state 字典，mock _fetch_quotes_batch 返回假行情，
断言：
1) 首次拉 [A, B, C] 会调用 _fetch_quotes_batch 一次，且 3 个 code 进缓存；
2) 删掉 A 后，再拉 [B, C] 时 _fetch_quotes_batch 不再被调用（B、C 命中缓存）；
3) 缓存 TTL 过期后才会重新拉取。
"""
import os
import sys
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, ".")

import pandas as pd

import dashboard.pages.portfolio as P


def _fake_row(code):
    return {
        "security_code": code,
        "market_price": 10.0,
        "quote_name": code,
        "quote_sector_name": None,
        "quote_pct": 1.0,
        "quote_source": "eastmoney_realtime",
    }


class QuoteCacheTest(unittest.TestCase):
    def _run(self, codes, fake_st, fetch_side_effect):
        with mock.patch.object(P, "st", fake_st), \
             mock.patch.object(P, "_fetch_quotes_batch", side_effect=fetch_side_effect) as m:
            out = P.load_live_quotes_for_portfolio(tuple(codes))
        return out, m

    def test_per_code_cache_avoid_repull_on_delete(self):
        fetch_log = []

        def fake_fetch(codes):
            fetch_log.append(tuple(codes))
            return [_fake_row(c) for c in codes]

        fake_st = types.SimpleNamespace(session_state={})
        # 首次拉 A,B,C
        out1, m1 = self._run(["A", "B", "C"], fake_st, fake_fetch)
        self.assertEqual(len(out1), 3)
        self.assertEqual(m1.call_count, 1)
        self.assertEqual(fetch_log[0], ("A", "B", "C"))

        # 删掉 A：再拉 [B, C]，B、C 应命中缓存，不重新拉取
        out2, m2 = self._run(["B", "C"], fake_st, fake_fetch)
        self.assertEqual(len(out2), 2)
        self.assertEqual(m2.call_count, 0, "删除一个标的后其余应命中 per-code 缓存，不应整批重拉")
        self.assertEqual(len(fetch_log), 1, "fetch 不应被再次调用")

    def test_cache_expiry_triggers_refetch(self):
        fetch_log = []
        fake_st = types.SimpleNamespace(session_state={})

        def fake_fetch(codes):
            fetch_log.append(tuple(codes))
            return [_fake_row(c) for c in codes]

        out1, m1 = self._run(["A", "B"], fake_st, fake_fetch)
        self.assertEqual(len(out1), 2)
        # 把缓存时间改成已过期
        for v in fake_st.session_state["_quote_cache"].values():
            v["t"] = time.time() - (P._QUOTE_TTL + 10)
        out2, m2 = self._run(["A", "B"], fake_st, fake_fetch)
        self.assertEqual(len(out2), 2)
        self.assertEqual(m2.call_count, 1, "TTL 过期后应重新拉取")

    def test_empty_codes_returns_empty_frame(self):
        fake_st = types.SimpleNamespace(session_state={})
        with mock.patch.object(P, "st", fake_st):
            out = P.load_live_quotes_for_portfolio(())
        self.assertTrue(out.empty)
        self.assertIn("security_code", out.columns)


if __name__ == "__main__":
    unittest.main(verbosity=2)
