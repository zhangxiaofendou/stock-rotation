"""自检：行情带出的行业名（如「旅游」）应能自动关联到同花顺 881xxx 行业代码。

覆盖两个闭环点：
1) resolve_sector：行业名(含简称/变体) -> (881xxx 代码, 规范名, 板块组)
2) _build_position_analysis：存量记录只存了行业名(无代码)时，反查 881xxx 并命中板块状态机，
   不再落空成「数据不足」。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from portfolio.stock_lookup import resolve_sector
import dashboard.views.portfolio as P


class TestResolveSector(unittest.TestCase):
    def test_travel_shortname_resolves(self):
        code, name, grp = resolve_sector("旅游")
        self.assertEqual(code, "881160")
        self.assertEqual(name, "旅游及酒店")
        self.assertEqual(grp, "消费")

    def test_travel_alias_variant(self):
        # 东财偶尔返回「旅游酒店」这类变体，也应命中 881160
        self.assertEqual(resolve_sector("旅游酒店")[0], "881160")
        self.assertEqual(resolve_sector("旅游及酒店")[0], "881160")

    def test_other_names(self):
        self.assertEqual(resolve_sector("白酒Ⅱ")[0], "881273")
        self.assertEqual(resolve_sector("半导体")[0], "881121")
        self.assertEqual(resolve_sector("银行")[0], "881155")

    def test_unknown_name_falls_to_group(self):
        code, name, grp = resolve_sector("随便乱写")
        self.assertIsNone(code)
        self.assertEqual(name, "随便乱写")
        self.assertEqual(grp, "其他")

    def test_empty_name(self):
        code, name, grp = resolve_sector("")
        self.assertIsNone(code)
        self.assertEqual(grp, "其他")


class TestAnalysisBackfill(unittest.TestCase):
    def _positions_quotes(self):
        # 真实 positions 来自持仓账本，不含 market_price（现价由 quotes 合并进来）
        positions = pd.DataFrame([{
            "security_code": "600138", "security_name": "中青旅",
            "sector_code": "", "sector_name": "旅游",
            "quantity": 100, "avg_cost": 10.0,
        }])
        quotes = pd.DataFrame([{
            "security_code": "600138", "market_price": 11.0,
            "quote_pct": 1.0, "quote_source": "eastmoney",
        }])
        return positions, quotes

    def test_legacy_record_with_name_only_gets_linked(self):
        """只存了行业名「旅游」、没代码的老记录，应反查 881160 并命中状态机。"""
        positions, quotes = self._positions_quotes()
        states = pd.DataFrame([{
            "sector_code": "881160", "sector_name": "旅游及酒店",
            "state": "⑥弱转强", "trend": "", "date": "2026-08-06",
        }])
        out = P._build_position_analysis(positions, quotes, states)
        self.assertEqual(len(out), 1)
        row = out.iloc[0]
        self.assertEqual(row["sector_code"], "881160", "反查后应有 881160 代码")
        self.assertEqual(row["sector_state"], "⑥弱转强", "应命中板块状态机")
        self.assertNotEqual(row["priority"], "数据不足", "不应仍显示数据不足")

    def test_record_with_code_still_matches(self):
        """已经带了 881160 代码的新记录，直接命中。"""
        positions, quotes = self._positions_quotes()
        positions.loc[0, "sector_code"] = "881160"
        states = pd.DataFrame([{
            "sector_code": "881160", "sector_name": "旅游及酒店",
            "state": "⑥弱转强", "trend": "", "date": "2026-08-06",
        }])
        out = P._build_position_analysis(positions, quotes, states)
        self.assertEqual(out.iloc[0]["sector_code"], "881160")
        self.assertEqual(out.iloc[0]["sector_state"], "⑥弱转强")

    def test_no_sector_info_stays_data_insufficient(self):
        """既没有代码也没有行业名，确实无法关联，保持数据不足（行为不变）。"""
        positions, quotes = self._positions_quotes()
        positions.loc[0, "sector_name"] = ""
        out = P._build_position_analysis(positions, quotes, pd.DataFrame())
        self.assertEqual(out.iloc[0]["priority"], "数据不足")


if __name__ == "__main__":
    unittest.main(verbosity=2)
