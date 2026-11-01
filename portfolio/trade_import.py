"""从券商交割单 / 粘贴文本 解析逐笔交易记录，标准化为导入结构。

支持输入：
  - CSV 文本 / 文件内容（逗号、分号、制表符、空白分隔自动探测）
  - Excel 文件（xlsx / xls，调用 pandas.read_excel）
  - 纯文本（每行一笔，列间用空白 / 逗号 / 制表符分隔）

列名智能匹配（中文 + 英文常见别名），对券商 App 导出的交割单格式鲁棒。
代码、方向、日期、数值均做了清洗与容错，并对缺失数量做了金额/价格反算。
"""
from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass
from datetime import date
from typing import Optional

import pandas as pd


@dataclass
class ParsedTrade:
    """单笔解析结果。``ok`` 为 False 表示该行致命缺失，不应导入。"""

    trade_date: str
    security_code: str
    security_name: str
    side: str  # BUY / SELL / ADJUST
    quantity: float
    price: float
    fee: float = 0.0
    note: str = ""
    ok: bool = True
    warn: str = ""  # 该行的非致命提示（缺日期、名称占位等）


# 目标字段 → 候选别名（归一化后小写匹配）。顺序即匹配优先级，靠前的先抢列。
COLUMN_ALIASES: dict[str, list[str]] = {
    "trade_date": [
        "日期", "交易日期", "成交日期", "委托日期", "业务日期", "交割日期",
        "date", "时间", "datetime", "tradedate", "trade_date", "成交时间",
    ],
    "security_code": [
        "证券代码", "股票代码", "代码", "证券代码6位", "symbol", "code",
        "secucode", "stockcode", "stock_code", "股票", "证券",
    ],
    "security_name": [
        "证券名称", "股票名称", "名称", "证券简称", "name", "secuname",
        "stockname", "stock_name", "证券名称简",
    ],
    "side": [
        "买卖", "方向", "操作", "交易类型", "业务类型", "动作", "side", "tradetype",
        "trade_type", "bs", "flag", "operation", "业务", "成交方向",
    ],
    "price": [
        "成交价格", "价格", "成交价", "单价", "市价", "price", "avgprice", "cpx",
        "tradeprice", "dealprice", "成交单价",
    ],
    "quantity": [
        "成交数量", "数量", "股数", "成交股数", "qty", "volume", "amountshares",
        "shares", "count", "number", "股份", "成交股数股",
    ],
    "fee": [
        "手续费", "费用", "佣金", "交易费用", "fee", "commission", "commissionfee",
        "charges", "servicefee", "佣金费", "规费",
    ],
    "amount": [
        "成交金额", "金额", "成交额", "total", "dealamount", "turnover", "amt",
        "成交总额", "清算金额", "amount",
    ],
}


def _normalize_col(name: str) -> str:
    """列名归一化：转小写、去所有空白与中英文标点，便于别名匹配。"""
    s = str(name).strip().lower()
    s = re.sub(r"[\s\-_().，,；;:：()（）/、]", "", s)
    return s


def _match_columns(columns) -> dict[str, str]:
    """把 DataFrame 列名映射到目标字段，返回 {目标字段: 原始列名}。"""
    norm_to_orig = {_normalize_col(c): c for c in columns}
    result: dict[str, str] = {}
    used: set[str] = set()
    for target, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            na = _normalize_col(alias)
            if na in norm_to_orig:
                orig = norm_to_orig[na]
                if orig not in used:
                    result[target] = orig
                    used.add(orig)
                    break
    return result


def _clean_number(val) -> Optional[float]:
    """清洗货币/数值：去 ¥ 逗号 空格 单位字，括号表示负数。无法解析返回 None。"""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        if pd.isna(val):
            return None
        return float(val)
    s = str(val).strip()
    if s == "" or s in ("-", "—", "nan", "none", "na", "null"):
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    s = re.sub(r"[¥￥$,\s元股笔]", "", s)
    s = s.replace("，", "").replace("、", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _clean_date(val) -> Optional[str]:
    """解析多种日期格式为 ISO (YYYY-MM-DD)。失败返回 None。"""
    if val is None:
        return None
    if isinstance(val, (pd.Timestamp, date)):
        return pd.Timestamp(val).date().isoformat()
    s = str(val).strip()
    if not s or s in ("nan", "none", "na", "null"):
        return None
    try:
        ts = pd.to_datetime(s, errors="coerce")
        if pd.notna(ts):
            return ts.date().isoformat()
    except Exception:
        return None
    return None


def _extract_code(val) -> Optional[str]:
    """从单元格提取 6 位证券代码：支持纯数字、SH600519、600519.SH、600519.SZ。

    纯数字代码（如 000001）被 pandas 读成整数时会丢失前导零，这里补回 6 位。
    """
    if val is None:
        return None
    # 整数/浮点：可能是被去前导零的代码（000001 → 1）
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        try:
            n = int(val)
            if 1 <= n <= 999999:
                return f"{n:06d}"
        except (ValueError, OverflowError):
            pass
    s = str(val).strip()
    m = re.search(r"(\d{6})", s)
    return m.group(1) if m else None


def _extract_side(val) -> Optional[str]:
    """识别买卖方向：中文（买/卖/调）、英文（B/S/BUY/SELL/ADJUST）。"""
    if val is None:
        return None
    s = str(val).strip().lower()
    if re.search(r"买|购入|建仓|加仓|买入|申购", s):
        return "BUY"
    if re.search(r"卖|清仓|减仓|卖出|抛出|赎回", s):
        return "SELL"
    if re.search(r"调|调整|调仓|转|派", s):
        return "ADJUST"
    if s in ("b", "buy", "buyin", "long", "b1"):
        return "BUY"
    if s in ("s", "sell", "sellout", "short", "s1"):
        return "SELL"
    if s in ("adj", "adjust", "adjustment", "d"):
        return "ADJUST"
    return None


def _read_table(file_bytes: bytes, filename: str) -> pd.DataFrame:
    """从文件字节读取为 DataFrame（Excel 走 read_excel，其余走自动分隔 CSV）。"""
    if filename and filename.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(file_bytes))
    return _read_csv_bytes(file_bytes)


def _read_csv_bytes(file_bytes: bytes) -> pd.DataFrame:
    raw = file_bytes.decode("utf-8-sig", errors="ignore")
    return _read_csv_text(raw)


def _read_csv_text(raw_text: str) -> pd.DataFrame:
    """自动探测分隔符读取文本；列数不足时依次回退逗号/制表符。"""
    candidates = [None, ",", "\t", ";", r"\s+"]
    last_err: Optional[Exception] = None
    for sep in candidates:
        try:
            kw = {"sep": sep, "engine": "python"} if sep is not None else {
                "sep": None,
                "engine": "python",
            }
            df = pd.read_csv(io.StringIO(raw_text), **kw)
            if df.shape[1] >= 2:
                return df
            last_err = None
        except Exception as exc:  # 分隔符不匹配会抛错
            last_err = exc
    if last_err:
        raise last_err
    return pd.DataFrame()


def read_source(raw_text: str = None, file_bytes: bytes = None, filename: str = None) -> pd.DataFrame:
    """统一入口：file_bytes 优先，其次 raw_text。"""
    if file_bytes is not None:
        return _read_table(file_bytes, filename or "")
    if raw_text and raw_text.strip():
        return _read_csv_text(raw_text)
    raise ValueError("未提供任何数据（请上传文件或在文本框粘贴）")


def _parse_row(row: pd.Series, colmap: dict[str, str]) -> ParsedTrade:
    warn: list[str] = []

    code = _extract_code(row[colmap["security_code"]]) if "security_code" in colmap else None
    name_raw = row[colmap["security_name"]] if "security_name" in colmap else None
    name = str(name_raw).strip() if name_raw is not None and pd.notna(name_raw) else ""
    side = _extract_side(row[colmap["side"]]) if "side" in colmap else None
    price = _clean_number(row[colmap["price"]]) if "price" in colmap else None
    qty = _clean_number(row[colmap["quantity"]]) if "quantity" in colmap else None
    fee = _clean_number(row[colmap["fee"]]) if "fee" in colmap else 0.0
    trade_date = _clean_date(row[colmap["trade_date"]]) if "trade_date" in colmap else None
    amount = _clean_number(row[colmap["amount"]]) if "amount" in colmap else None

    # 数量缺失但金额与价格齐全 → 反算（金额通常含费用，取整近似）
    if qty is None and amount is not None and price:
        qty = abs(amount) / price
        warn.append("数量由金额/价格反算")

    # 名称为空 → 用代码占位，提示用户确认
    if not name and code:
        name = code
        warn.append("名称为空，已用代码占位，请确认")

    ok = True
    if not code:
        ok = False
        warn.append("无法识别证券代码")
    if side is None:
        ok = False
        warn.append("无法识别买卖方向")
    if price is None:
        ok = False
        warn.append("无法识别价格")
    if qty is None:
        ok = False
        warn.append("无法识别数量")
    if trade_date is None:
        warn.append("未识别日期，将用今天")

    return ParsedTrade(
        trade_date=trade_date or date.today().isoformat(),
        security_code=code or "",
        security_name=name,
        side=side or "",
        quantity=abs(qty) if qty is not None else 0.0,
        price=price or 0.0,
        fee=fee or 0.0,
        ok=ok,
        warn="；".join(warn),
    )


def parse_trades(
    raw_text: str = None, file_bytes: bytes = None, filename: str = None
) -> tuple[list[ParsedTrade], list[str]]:
    """解析交易记录。

    返回 (记录列表, 错误/警告列表)。记录列表可能为空（读取失败或全空行）。
    ``errors`` 中的项用于页面提示，不一定是致命错误。
    """
    errors: list[str] = []
    try:
        df = read_source(raw_text=raw_text, file_bytes=file_bytes, filename=filename)
    except Exception as exc:
        return [], [f"读取失败：{exc}"]
    if df.empty or df.shape[1] < 2:
        return [], ["未解析到任何数据行，请检查内容格式（至少应有代码、价格等两列）"]

    colmap = _match_columns(df.columns)
    missing_required = [k for k in ("security_code", "side", "price", "quantity") if k not in colmap]
    if missing_required:
        errors.append(
            "未识别到必要列："
            + "、".join(COLUMN_ALIASES[k][0] for k in missing_required)
            + "。请检查表头，或用「日期,代码,名称,方向,数量,价格,费用」标准表头粘贴。"
        )

    trades: list[ParsedTrade] = []
    for _, row in df.iterrows():
        if row.isna().all():
            continue
        trades.append(_parse_row(row, colmap))
    return trades, errors


def trades_to_records(trades: list[ParsedTrade]) -> list[dict]:
    """转成可直接展示/导入的 dict 列表（剔除 dataclass 内部字段的冗余）。"""
    keep = ["trade_date", "security_code", "security_name", "side", "quantity", "price", "fee", "warn", "ok"]
    return [{k: getattr(t, k) for k in keep} for t in trades]
