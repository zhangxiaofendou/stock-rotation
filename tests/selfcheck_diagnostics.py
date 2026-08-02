"""诊断器有效性回归。

必须证明它能抓出真实踩过的坑，否则「全绿」毫无意义。
用例全部取自线上实际填错过的 DATABASE_URL 形态。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import diagnostics as D  # noqa: E402

_passed = 0
_failed = 0


def check(name, cond, extra=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [OK]   {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name} {extra}")


def diag_url(url):
    """在给定 DATABASE_URL 下跑 L4 配置层，返回 {检查项: (状态, 详情)}。"""
    old = os.environ.get("DATABASE_URL")
    if url is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = url
    try:
        rep = D.Report()
        D.check_database_url(rep)
        return {c.name: (c.status, c.detail, c.fix) for c in rep.checks}
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old


REF = "shzmkckegklrtjwiwhdd"
POOLER = "aws-1-ap-south-1.pooler.supabase.com"

print("=" * 62)
print("  诊断器有效性回归（用线上真实踩过的错误连接串）")
print("=" * 62)

# ---------- 坑 1：Supabase 直连地址仅 IPv6，Streamlit Cloud 连不上 ----------
print("\n[坑1] 直连地址 db.xxx.supabase.co（仅 IPv6）")
r = diag_url(f"postgresql://postgres:Zwy529740779@db.{REF}.supabase.co:5432/postgres")
check("识别为阻断", r["主机可达性类型"][0] == D.FAIL, r["主机可达性类型"])
check("修复建议提到 pooler", "pooler" in r["主机可达性类型"][2])

# ---------- 坑 2：密码被当成用户名（缺少 用户名: 部分）----------
print("\n[坑2] 密码写在用户名位置：postgresql://529740779@host")
r = diag_url(f"postgresql://529740779@{POOLER}:5432/postgres")
check("识别为阻断", r["密码特殊字符"][0] == D.FAIL, r["密码特殊字符"])
check("指出缺少密码", "密码" in r["密码特殊字符"][1])

# ---------- 坑 3：密码含未编码的 @ [ ] ----------
print("\n[坑3] 密码含未编码特殊字符 [Zwy@529740779]")
r = diag_url(f"postgresql://postgres.{REF}:[Zwy@529740779]@{POOLER}:5432/postgres")
check("识别为阻断", r["密码特殊字符"][0] == D.FAIL, r["密码特殊字符"])
check("列出需转义字符", "%40" in r["密码特殊字符"][2] and "%5B" in r["密码特殊字符"][2])

# ---------- 坑 4：占位符没替换 ----------
print("\n[坑4] [YOUR-PASSWORD] 占位符未替换")
r = diag_url(f"postgresql://postgres.{REF}:[YOUR-PASSWORD]@{POOLER}:5432/postgres")
check("识别为阻断", r["密码特殊字符"][0] == D.FAIL, r["密码特殊字符"])
check("明确指出是占位符", "占位符" in r["密码特殊字符"][1])

# ---------- 坑 5：用 pooler 却写裸 postgres 用户名 ----------
print("\n[坑5] pooler 主机 + 裸 postgres 用户名")
r = diag_url(f"postgresql://postgres:Zwy529740779@{POOLER}:5432/postgres")
check("识别为阻断", r["用户名格式"][0] == D.FAIL, r["用户名格式"])
check("给出 postgres.<ref> 格式", "postgres.<项目ref>" in r["用户名格式"][2])

# ---------- 坑 6：完全正确的连接串不应误报 ----------
print("\n[坑6] 正确的 Session pooler 连接串（不得误报）")
r = diag_url(f"postgresql://postgres.{REF}:Zwy529740779@{POOLER}:5432/postgres")
check("结构通过", r["连接串结构"][0] == D.PASS, r["连接串结构"])
check("密码字符通过", r["密码特殊字符"][0] == D.PASS, r["密码特殊字符"])
check("主机类型通过", r["主机可达性类型"][0] == D.PASS, r["主机可达性类型"])
check("用户名通过", r["用户名格式"][0] == D.PASS, r["用户名格式"])
check("端口通过", r["端口"][0] == D.PASS, r["端口"])

# ---------- 坑 7：未配置时给隐患提示而非崩溃 ----------
print("\n[坑7] 未配置 DATABASE_URL")
r = diag_url(None)
check("给出隐患警告", r["DATABASE_URL 是否配置"][0] == D.WARN, r["DATABASE_URL 是否配置"])
check("说明会丢数据", "清空" in r["DATABASE_URL 是否配置"][2])

# ---------- 坑 8：连接串里的密码不得在报告中明文泄露 ----------
print("\n[坑8] 报告脱敏")
r = diag_url(f"postgresql://postgres.{REF}:SuperSecret123@{POOLER}:5432/postgres")
check("密码被打码", "SuperSecret123" not in r["DATABASE_URL 是否配置"][1],
      r["DATABASE_URL 是否配置"][1])

# ---------- 坑 9：scheme 写错 ----------
print("\n[坑9] scheme 缺失 / 写错")
r = diag_url(f"postgres.{REF}:pwd@{POOLER}:5432/postgres")
check("缺 scheme 被拦截", r["连接串结构"][0] == D.FAIL, r["连接串结构"])
r = diag_url(f"mysql://postgres.{REF}:pwd@{POOLER}:3306/postgres")
check("错误 scheme 被拦截", r["连接串结构"][0] == D.FAIL, r["连接串结构"])

# ---------- 坑 10：诊断器整体不得崩溃 ----------
print("\n[坑10] 全量诊断稳定性")
rep = D.run_all()
check("能完整跑完", len(rep.checks) >= 15, f"仅 {len(rep.checks)} 项")
check("无内部异常项", not any("诊断器内部错误" in c.fix for c in rep.checks),
      [c.name for c in rep.checks if "诊断器内部错误" in c.fix])
txt = D.format_text(rep)
check("能生成文本报告", "诊断报告" in txt and len(txt) > 200)
check("报告含结论行", "结论：" in txt)

print("\n" + "=" * 62)
print(f"  结果：{_passed} 通过 / {_failed} 失败")
print("=" * 62)
sys.exit(1 if _failed else 0)
