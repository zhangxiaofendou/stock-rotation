"""腾讯实时行情源（替代被反爬的同花顺实时接口）。

提供主要指数 + 行业 ETF 的盘中实时快照（价格、涨跌幅）。
数据来自腾讯 qt.gtimg.cn，运行时直连可用（同花顺实时接口被反爬，返回 None/401）。

注意：腾讯 qt.gtimg.cn 仅支持个股/指数/ETF，不支持行业板块批量查询，
且腾讯板块代码体系与同花顺 881xxx 不对应。因此本源输出「指数 + 行业 ETF」
级别的实时行情，用于顶部盘中实时行情条，不用于 881xxx 行业级聚合。
"""
import logging
import urllib.request
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://gu.qq.com/",
}

# 宽基指数 + 代表性行业 ETF（腾讯代码）。覆盖主要板块情绪。
DEFAULT_REALTIME_CODES = [
    # 宽基指数
    "sh000001",  # 上证指数
    "sz399001",  # 深证成指
    "sz399006",  # 创业板指
    "sh000300",  # 沪深300
    "sh000905",  # 中证500
    "sh000688",  # 科创50
    # 行业 ETF（映射板块情绪）
    "sh512480",  # 半导体ETF
    "sz159995",  # 芯片ETF
    "sz515030",  # 新能源车ETF
    "sh512660",  # 军工ETF
    "sh512010",  # 医药ETF
    "sz159928",  # 消费ETF
    "sh515790",  # 光伏ETF
    "sz159869",  # 游戏ETF
    "sh512690",  # 酒ETF
    "sz159841",  # 证券ETF
    "sh516160",  # 新能源ETF
    "sz159892",  # 恒生医药ETF
]


class TencentRealtimeSource:
    """腾讯实时行情源：批量拉取指数/ETF 实时快照。"""

    def __init__(self, timeout: int = 10):
        self.timeout = timeout

    def get_realtime_quotes(self, codes: Optional[List[str]] = None) -> List[Dict]:
        """返回 [{code, name, price, pct}, ...]，失败返回 []。"""
        codes = codes or DEFAULT_REALTIME_CODES
        raw = self._fetch(",".join(codes))
        if not raw:
            logger.warning("腾讯实时行情拉取失败，返回空")
            return []
        return self._parse(raw)

    def _fetch(self, code_str: str) -> Optional[str]:
        url = _TENCENT_QUOTE_URL + code_str
        req = urllib.request.Request(url, headers=_DEFAULT_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("gbk", "ignore")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"腾讯实时行情请求失败: {e}")
            return None

    @staticmethod
    def _parse(raw: str) -> List[Dict]:
        quotes: List[Dict] = []
        for line in raw.strip().split(";"):
            line = line.strip()
            if not line or "=" not in line:
                continue
            head, _, body = line.partition("=")
            code = head.replace("v_", "").strip()
            if not body.startswith('"'):
                continue
            body = body[1:-1]
            f = body.split("~")
            # 字段：[1]=名称 [3]=当前价 [32]=涨跌%
            if len(f) < 33 or not f[1]:
                continue
            try:
                quotes.append(
                    {
                        "code": code,
                        "name": f[1],
                        "price": float(f[3]) if f[3] else 0.0,
                        "pct": float(f[32]) if f[32] else 0.0,
                    }
                )
            except (ValueError, IndexError):
                continue
        return quotes
