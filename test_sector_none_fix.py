"""复现并验证 sector_name=None 时的 KeyError 修复。"""
import pandas as pd
from unittest.mock import MagicMock

from portfolio.advisor import PortfolioAdvisor


def test_sector_name_none_does_not_crash():
    advisor = PortfolioAdvisor()
    advisor.holdings = MagicMock()
    advisor.holdings.positions.return_value = pd.DataFrame([
        {
            "security_code": "000001",
            "security_name": "平安银行",
            "quantity": 100.0,
            "avg_cost": 10.0,
            "cost_amount": 1000.0,
            "sector_code": None,
            "sector_name": None,
            "stop_loss": None,
        }
    ])

    result = advisor.build_pending_items(sector_states=pd.DataFrame())
    assert not result.empty, "应当生成至少一条'行业映射'提示"
    assert "行业映射" in result["类别"].values, f"类别应为行业映射，实际为 {result['类别'].tolist()}"
    print("PASS: sector_name=None 时未崩溃，且正确提示补全行业映射")


if __name__ == "__main__":
    test_sector_name_none_does_not_crash()
