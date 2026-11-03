# -*- coding: utf-8 -*-
"""行业补全检查与留痕：三入口（登录/导入/添加）统一复用。

check_and_fill_position(service, code, ...) 负责：
  1) 读账本「补全前」行业名/代码；
  2) 离线优先解析（industry_resolver.resolve_stock_industry）；
  3) 缺哪个补哪个，写回账本；
  4) 落库一条留痕（sector_fill_log）—— 页面「行业补全检查报告」据此可见。
返回 trace dict，便于调用方即时展示。
"""
from typing import Optional

from portfolio.industry_resolver import resolve_stock_industry, _em_name_to_sw


def _blank(v) -> bool:
    """判断行业名/代码是否为「缺失」。

    必须同时识别 None、空串、以及 pandas/numpy 的缺失值（NaN / NA / NaT）——
    账本中 NULL 行业经 pandas 读出后是 NaN（float），旧的判断只认 None/空串，
    会把这些 NaN 误判为「已有值」，导致行业永远不补全（本项目「行业无法自动补全」
    的真正根因之一）。
    """
    if v is None:
        return True
    # NaN 是唯一「不等于自身」的浮点值：float(v) != float(v) 仅对 NaN 成立
    try:
        if float(v) != float(v):
            return True
    except (TypeError, ValueError):
        pass
    s = str(v).strip().lower()
    return s in ("", "none", "nan", "nat", "na", "<na>")


def check_and_fill_position(service, code: str, security_name: str = None,
                            source_tag: str = "手动检查", online_fallback: bool = False) -> dict:
    """检查一只持仓并补全行业（离线优先），返回留痕 dict。

    trace 字段：code, name, before_name, before_code, after_name, after_code,
    resolved_name, resolved_code, source, action, filled(bool)。

    online_fallback=False（默认）：仅用离线包+桥接，绝不联网——用于登录自检等
    启动期场景，避免云端断网时卡住；离线包已覆盖全 A 股，绝大多数可命中。
    online_fallback=True：离线缺失时再尝试在线反查（交互式录入/导入时兜底）。
    """
    code = str(code or "").strip()
    trace = {
        "code": code, "name": security_name,
        "before_name": None, "before_code": None,
        "after_name": None, "after_code": None,
        "resolved_name": None, "resolved_code": None,
        "source": "", "action": "未处理", "filled": False,
    }
    if not code:
        return trace

    positions = service.positions()
    row = None
    if positions is not None and not positions.empty and "security_code" in positions.columns:
        sub = positions[positions["security_code"] == code]
        if not sub.empty:
            row = sub.iloc[0]
    before_name = (row.get("sector_name") if row is not None else None)
    before_code = (row.get("sector_code") if row is not None else None)
    trace["before_name"] = before_name
    trace["before_code"] = before_code
    if security_name is None and row is not None:
        security_name = row.get("security_name")
    trace["name"] = security_name

    name_blank = _blank(before_name)
    code_blank = _blank(before_code)

    resolved_name, resolved_code = None, None
    if name_blank or code_blank:
        try:
            em_name, em_code = resolve_stock_industry(code, online_fallback=online_fallback)
        except Exception:  # noqa: BLE001
            em_name, em_code = None, None
        if name_blank and em_name:
            resolved_name = em_name
        if code_blank:
            # 行业代码优先从「账本已有行业名」推导：用户手填/历史的行业名可能与该 code
            # 在 EM 离线包里的名不同（如账本名为「IT服务Ⅱ」→ 881271，而非该 code 的
            # EM 名「软件开发」→ 881272）。只有账本名也缺失时，才退用 code 的 EM 桥接码。
            if (not name_blank) and before_name:
                derived = _em_name_to_sw(str(before_name).strip())
                if derived:
                    resolved_code = derived
            if not resolved_code and em_code:
                resolved_code = em_code
    trace["resolved_name"] = resolved_name
    trace["resolved_code"] = resolved_code

    if not name_blank and not code_blank:
        # 行业名+代码都已齐全：仍要落库一条「检查留痕」（用户要看到每次检查），
        # 走 else 之外的分支，不再进入下方的补全逻辑。
        trace["action"] = "已齐全"
        trace["after_name"] = before_name
        trace["after_code"] = before_code
        trace["source"] = "账本已有"
    else:
        updates = {}
        src_parts = []
        if name_blank and resolved_name:
            updates["sector_name"] = resolved_name
            src_parts.append("离线包" if not _online_used(resolved_name) else "在线")
        if code_blank and resolved_code:
            updates["sector_code"] = resolved_code
            src_parts.append("桥接/映射")

        if updates:
            try:
                service.update_metadata(code, **updates)
            except Exception:  # noqa: BLE001
                pass
            trace["after_name"] = updates.get("sector_name", before_name)
            trace["after_code"] = updates.get("sector_code", before_code)
            trace["source"] = "+".join(src_parts) or "离线"
            if "sector_name" in updates and "sector_code" in updates:
                trace["action"] = "补行业名+代码"
            elif "sector_name" in updates:
                trace["action"] = "补行业名"
            else:
                trace["action"] = "补行业代码"
            trace["filled"] = True
        else:
            # 离线+在线都没解析到 -> 无法补全
            trace["after_name"] = before_name
            trace["after_code"] = before_code
            if name_blank and code_blank:
                trace["action"] = "未能补全(无行业数据)"
            elif name_blank:
                trace["action"] = "行业名未补全(无数据)"
            else:
                trace["action"] = "行业代码未补全(无数据)"
            trace["source"] = "无数据"

    # 落库留痕：无论「已齐全 / 已补全 / 未能补全」都写一条，供页面「检查报告」可见。
    # source 用 source_tag（登录自检 / 添加记录 / 导入 / 手动检查）标识触发入口，
    # 让用户一眼看出是哪次操作做的检查；trace["source"] 仅作内部解析来源记录。
    try:
        service.store.log_sector_fill(
            user_id=getattr(service, "user_id", "") or "",
            security_code=code, security_name=security_name or "",
            before_name=before_name, before_code=before_code,
            after_name=trace["after_name"], after_code=trace["after_code"],
            action=trace["action"], source=source_tag,
        )
    except Exception:  # noqa: BLE001
        pass
    return trace


def _online_used(name) -> bool:
    """占位：当前解析器离线优先，resolved_name 来自离线包即视为离线；
    在线兜底仅在离线缺失时触发，这里简化返回 False（来源统一标为离线/桥接）。"""
    return False


def summarize_traces(traces: list) -> dict:
    """汇总多条留痕，给出给用户看的结论计数。"""
    out = {
        "total": len(traces),
        "ok": sum(1 for t in traces if not _blank(t.get("after_name")) and not _blank(t.get("after_code"))),
        "filled": sum(1 for t in traces if t.get("filled")),
        "failed": sum(1 for t in traces if "未补全" in (t.get("action") or "") or "未能补全" in (t.get("action") or "")),
        "already": sum(1 for t in traces if t.get("action") == "已齐全"),
    }
    return out
