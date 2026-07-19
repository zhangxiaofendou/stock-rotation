"""统一通知服务。

仅负责分发"摘要 + 链接"，不计算指标、不持有业务数据。
渠道凭据独立存放于 config/notification.json（不进版本控制、不进页面代码）；
事件订阅开关存放于 data/storage/notification_subs.json（运行期产物）。
"""
