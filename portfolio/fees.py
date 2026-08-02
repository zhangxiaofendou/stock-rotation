"""A股交易费用估算。仅作为录入辅助，实际以券商交割单为准。"""
from dataclasses import dataclass

COMMISSION_RATE = 0.0003
COMMISSION_MIN = 5.0
STAMP_RATE = 0.0005
TRANSFER_RATE = 0.00001

@dataclass(frozen=True)
class FeeEstimate:
    commission: float
    stamp_tax: float
    transfer_fee: float

    @property
    def total(self) -> float:
        return round(self.commission + self.stamp_tax + self.transfer_fee, 2)

def _is_etf(code: str) -> bool:
    return str(code or "").strip().startswith(("15", "16", "50", "51", "56", "58"))

def estimate_trade_fee(code: str, side: str, quantity: float, price: float) -> FeeEstimate:
    qty, px = max(float(quantity or 0), 0.0), max(float(price or 0), 0.0)
    amount = qty * px
    side = str(side or "").upper()
    if side not in {"BUY", "SELL"} or amount <= 0:
        return FeeEstimate(0.0, 0.0, 0.0)
    etf = _is_etf(code)
    commission = max(amount * COMMISSION_RATE, COMMISSION_MIN)
    stamp_tax = amount * STAMP_RATE if side == "SELL" and not etf else 0.0
    transfer_fee = amount * TRANSFER_RATE if not etf else 0.0
    return FeeEstimate(round(commission, 2), round(stamp_tax, 2), round(transfer_fee, 2))
