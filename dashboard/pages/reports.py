"""盘后报告页面。

三个页签（PRD 5.6 / 阶段 D）：
- 今日报告：展示当日已生成的盘后报告；若无则一键生成（约 1–2 分钟）。
- 历史归档：按交易日查看已生成的报告。
- 通知设置：配置"哪些事件值得通知"及查看渠道状态；渠道凭据独立存放于
  config/notification.json（不在此页面、不进版本控制）。

报告只汇总已计算结果，所有明细可链接回原页面（见 report/generator.py）。
"""

from __future__ import annotations

import time

import streamlit as st

from config.logger import get_logger
from notification.service import NotificationService
from report.generator import generate_report, list_reports, load_report
from dashboard.components.data_source_badge import render_src_badge

logger = get_logger(__name__)

TAB_TODAY = "今日报告"
TAB_ARCHIVE = "历史归档"
TAB_NOTIFY = "通知设置"


def _render_today():
    st.subheader("今日盘后报告")
    render_src_badge("derived")
    st.caption("报告由每日管线在收盘后自动生成；此处可查看或按需手动生成。仅汇总已计算的"
               "市场、持仓、绩效与风险结论，不重新计算指标。")

    html = load_report()
    if html is None:
        st.info("尚未生成今日报告。点击下方按钮生成（首次约需 1–2 分钟，需重算全市场状态）。")
        if st.button("▶ 生成今日报告", key="gen_today"):
            with st.spinner("正在生成盘后报告（重算全市场状态，请稍候）…"):
                try:
                    res = generate_report()
                    st.success(f"已生成 {res['as_of_date']} 的盘后报告。")
                    html = res["html"]
                except Exception as e:  # noqa: BLE001
                    st.error(f"生成失败：{e}")
                    logger.warning("页面手动生成报告失败: %s", e)
                    return
    else:
        st.success("已加载最新归档报告。")

    # 生成 / 重新生成按钮
    if st.button("🔄 重新生成", key="regen_today", help="用最新数据重算并覆盖当日报告"):
        with st.spinner("正在重新生成盘后报告…"):
            try:
                res = generate_report()
                st.success(f"已重新生成 {res['as_of_date']} 的盘后报告。")
                html = res["html"]
            except Exception as e:  # noqa: BLE001
                st.error(f"生成失败：{e}")
                logger.warning("页面重新生成报告失败: %s", e)
                return

    if html:
        # 通知（若已配置渠道且订阅开启）
        svc = NotificationService()
        if svc.configured_channels():
            if st.button("📨 推送报告摘要", key="push_today"):
                with st.spinner("正在推送…"):
                    result = svc.notify_event(
                        "report_generated",
                        "盘后报告已生成",
                        "盘后报告已生成，详见应用内「盘后报告」页。",
                    )
                    if result is None:
                        st.info("未发送：事件未订阅或渠道未配置。")
                    else:
                        st.json(result)
        st.divider()
        st.html(html)


def _render_archive():
    st.subheader("历史归档")
    render_src_badge("derived")
    reports = list_reports()
    if not reports:
        st.info("暂无归档报告。每日管线运行后会自动生成；也可在「今日报告」页手动生成。")
        return

    dates = [r["date"] for r in reports]
    sel = st.selectbox("选择报告日期", dates, index=0)
    chosen = next((r for r in reports if r["date"] == sel), None)
    if not chosen:
        return
    meta = chosen.get("meta", {})
    if meta:
        c1, c2, c3 = st.columns(3)
        c1.metric("数据截止", meta.get("as_of_date", "—"))
        c2.metric("代码版本", meta.get("git_hash", "—"))
        c3.metric("状态切换", meta.get("n_transitions", 0))
    html = load_report(sel)
    if html:
        st.divider()
        st.html(html)
    else:
        st.warning("该日期报告文件缺失。")


def _render_notify():
    st.subheader("通知设置")
    render_src_badge("derived")
    st.caption("通知只分发摘要与链接，不计算指标。渠道凭据独立存放于 "
               "`config/notification.json`（不进版本控制、不在此页面编辑）。")

    svc = NotificationService()
    channels = svc.configured_channels()
    st.markdown("**已配置渠道**")
    if channels:
        for ch in channels:
            st.success(f"✓ {ch}（{_channel_label(ch)}）")
    else:
        st.warning("未配置任何通知渠道。请在 `config/notification.json` 中填入凭据，例如：\n"
                   "```json\n"
                   '{\n  "serverchan": {"sendkey": "SCTxxxx"},\n'
                   '  "wecom": {"webhook": "https://qyapi.weixin.qq.com/...key=xxxx"},\n'
                   '  "email": {"smtp_host": "...", "smtp_port": 465, "user": "...", "password": "...", "to": "..."}\n'
                   "}\n```")

    st.divider()
    st.markdown("**事件订阅**（仅当对应渠道已配置时才真正发送）")
    subs = svc.event_subscriptions()
    for ev, on in subs.items():
        label = _event_label(ev)
        new_val = st.toggle(label, value=on, key=f"sub_{ev}")
        if new_val != on:
            svc.set_subscription(ev, new_val)
            st.rerun()

    st.divider()
    if st.button("📨 发送测试通知", key="test_notify"):
        with st.spinner("发送中…"):
            res = svc.send("盘后报告 · 测试通知", "这是一条来自盘后报告模块的测试通知。")
        st.json(res)


def _channel_label(ch: str) -> str:
    return {"serverchan": "Server酱", "wecom": "企业微信机器人", "email": "邮件"}.get(ch, ch)


def _event_label(ev: str) -> str:
    return {
        "report_generated": "盘后报告生成后推送",
        "data_failure": "数据更新失败时推送",
        "circuit_breaker": "市场进入防御/熔断时推送",
        "holdings_risk": "持仓触发风险事项时推送",
        "signal_failure": "信号失效预警时推送",
    }.get(ev, ev)


def render():
    st.title("盘后报告")
    tabs = st.tabs([TAB_TODAY, TAB_ARCHIVE, TAB_NOTIFY])
    with tabs[0]:
        _render_today()
    with tabs[1]:
        _render_archive()
    with tabs[2]:
        _render_notify()


if __name__ == "__main__":
    render()
