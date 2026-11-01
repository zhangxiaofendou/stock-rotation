"""交易导入解析器自检：列名识别 / 数值清洗 / 方向 / 代码 / 数量反算 / 缺列报错。

说明：trade_import 是新增模块，无「旧实现」可复现 FAIL；这里用正向用例 +
边界用例确保解析器真的做了这些清洗（而非空壳），并验证坏输入不崩溃且给出提示。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from portfolio.trade_import import (  # noqa: E402
    parse_trades,
    _clean_number,
    _extract_code,
    _extract_side,
)


def _main() -> int:
    fails = []

    # ---- 用例1：标准中文券商交割单表头 ----
    csv1 = (
        "日期,证券代码,证券名称,买卖,成交价格,成交数量,手续费\n"
        "2026-01-05,600519,贵州茅台,买入,1680.00,100,0\n"
        "2026-01-06,000001,平安银行,卖出,12.50,200,5.20\n"
    )
    trades1, errs1 = parse_trades(raw_text=csv1)
    if errs1:
        fails.append(f"用例1 不应报错: {errs1}")
    if len(trades1) != 2:
        fails.append(f"用例1 解析笔数应为2，实得{len(trades1)}")
    else:
        t0, t1 = trades1
        if t0.security_code != "600519" or t0.security_name != "贵州茅台":
            fails.append("用例1 行1代码/名称错误")
        if t0.side != "BUY" or t1.side != "SELL":
            fails.append(f"用例1 方向识别错误: {t0.side},{t1.side}")
        if abs(t0.price - 1680.0) > 1e-6 or abs(t0.quantity - 100) > 1e-6:
            fails.append("用例1 价格/数量错误")
        if abs(t1.fee - 5.20) > 1e-6:
            fails.append("用例1 费用错误")
        if not t0.ok or not t1.ok:
            fails.append("用例1 应为可导入(ok=True)")

    # ---- 用例2：英文别名 + 货币符号 + 数量反算 ----
    # 注意：带千分位逗号的值必须用引号包裹，否则 CSV 逗号会被当分隔符
    csv2 = (
        'date,code,name,side,price,amount,fee\n'
        '2026-02-01,SH600519,贵州茅台,BUY,"¥1,680.00",168000.00,10\n'
    )
    trades2, _ = parse_trades(raw_text=csv2)
    if len(trades2) != 1:
        fails.append(f"用例2 解析笔数应为1，实得{len(trades2)}")
    else:
        t = trades2[0]
        if t.security_code != "600519":
            fails.append(f"用例2 代码提取错误(应600519): {t.security_code}")
        if t.side != "BUY":
            fails.append("用例2 方向错误")
        if abs(t.price - 1680.0) > 1e-6:
            fails.append("用例2 价格清洗错误(去¥逗号)")
        if abs(t.quantity - 100.0) > 1e-3:
            fails.append(f"用例2 数量反算错误(应≈100): {t.quantity}")
        if "数量由金额/价格反算" not in t.warn:
            fails.append("用例2 应提示数量反算")

    # ---- 用例3：代码带交易所后缀 600519.SH ----
    csv3 = "代码,名称,方向,价格,数量\n600519.SH,贵州茅台,买入,1680,100\n"
    trades3, _ = parse_trades(raw_text=csv3)
    if not trades3 or trades3[0].security_code != "600519":
        fails.append(f"用例3 代码后缀提取错误: {trades3[0].security_code if trades3 else '无'}")

    # ---- 用例4：缺买卖方向列 → 报错且该行 ok=False ----
    csv4 = "日期,证券代码,证券名称,成交价格,成交数量\n2026-03-01,600519,贵州茅台,1680,100\n"
    trades4, errs4 = parse_trades(raw_text=csv4)
    if not errs4 or not any("买卖" in e or "方向" in e for e in errs4):
        fails.append("用例4 应提示缺少买卖/方向列")
    if not trades4 or trades4[0].ok:
        fails.append("用例4 缺方向行应 ok=False")

    # ---- 用例5：单位函数独立验证 ----
    if _clean_number("¥1,680.00") != 1680.0:
        fails.append("clean_number 货币符号/逗号失败")
    if _clean_number("(50.00)") != -50.0:
        fails.append("clean_number 括号负数失败")
    if _extract_code("600519.SH") != "600519":
        fails.append("extract_code 后缀失败")
    if _extract_side("证券买入") != "BUY" or _extract_side("SELL") != "SELL" or _extract_side("调仓") != "ADJUST":
        fails.append("extract_side 失败")

    # ---- 用例6：坏输入不崩溃 ----
    bad, bad_errs = parse_trades(raw_text="这是一段完全无关的文本没有表格结构")
    if bad:
        fails.append("用例6 乱文本不应解析出交易")
    if not bad_errs:
        fails.append("用例6 乱文本应给出错误提示而非静默")

    if fails:
        print("❌ 失败:")
        for f in fails:
            print("  -", f)
        return 1
    print("✅ 全部通过：6 组用例（中文表头/英文别名+货币+反算/代码后缀/缺列报错/单位函数/坏输入）")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
