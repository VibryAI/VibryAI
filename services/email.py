"""Vibry AI Core — 邮件服务

发送验证码邮件（忘记密码流程）。

支持两种方式，通过 EMAIL_PROVIDER 环境变量切换：
  - agentmail : 使用 AgentMail.to API（推荐，无需配置 SMTP）
  - smtp      : 使用传统 SMTP（QQ邮箱、Gmail 等）
"""

import logging
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

log = logging.getLogger("vibry.email")

EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "agentmail").lower()

# ---- AgentMail 配置 ----
AGENTMAIL_API_KEY = os.getenv("AGENTMAIL_API_KEY", "")
AGENTMAIL_FROM_EMAIL = os.getenv("AGENTMAIL_FROM_EMAIL", "vibryai@agentmail.to")

# ---- SMTP 配置 ----
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)


# ================================================================
# AgentMail
# ================================================================

_agentmail_client = None
_agentmail_inbox_id = None


def _get_agentmail_client():
    global _agentmail_client
    if _agentmail_client is None:
        import httpx
        from agentmail import AgentMail

        # 支持代理（HTTPS_PROXY 环境变量或 AGENTMAIL_PROXY）
        proxy_url = os.getenv("AGENTMAIL_PROXY", os.getenv("HTTPS_PROXY", ""))
        httpx_kwargs = {}
        if proxy_url:
            log.info(f"📬 AgentMail 通过代理连接: {proxy_url}")
            httpx_kwargs["proxy"] = proxy_url

        _agentmail_client = AgentMail(
            api_key=AGENTMAIL_API_KEY,
            httpx_client=httpx.Client(**httpx_kwargs) if proxy_url else None,
        )
    return _agentmail_client


def _get_agentmail_inbox_id() -> str:
    global _agentmail_inbox_id
    if _agentmail_inbox_id is not None:
        return _agentmail_inbox_id

    client = _get_agentmail_client()
    try:
        inboxes = client.inboxes.list()
        if inboxes:
            _agentmail_inbox_id = inboxes[0].inbox_id
            log.info(f"📬 AgentMail inbox: {_agentmail_inbox_id} ({AGENTMAIL_FROM_EMAIL})")
            return _agentmail_inbox_id
    except Exception as e:
        log.warning(f"Failed to list AgentMail inboxes: {e}")

    inbox = client.inboxes.create()
    _agentmail_inbox_id = inbox.inbox_id
    log.info(f"📬 AgentMail inbox created: {_agentmail_inbox_id}")
    return _agentmail_inbox_id


def _send_via_agentmail(to_email: str, code: str) -> bool:
    """通过 AgentMail API 发送"""
    try:
        inbox_id = _get_agentmail_inbox_id()
        client = _get_agentmail_client()

        client.inboxes.messages.send(
            inbox_id,
            to=to_email,
            subject="Vibry AI Core — Password Reset Code / 密码重置验证码",
            text=f"Your verification code is: {code}\n\nValid for 5 minutes.\n\n-- Vibry AI Core",
            html=f"""<div style="max-width:480px;margin:0 auto;font-family:Arial,sans-serif">
<h2 style="color:#4fc3f7">🧠 Vibry AI Core</h2>
<p>You are requesting to reset your admin panel password. / 您正在请求重置管理后台密码。</p>
<p style="font-size:28px;font-weight:bold;letter-spacing:6px;color:#333;background:#f0f0f0;padding:16px;text-align:center;border-radius:8px">{code}</p>
<p>The code is valid for 5 minutes. If this was not you, please ignore this email. / 验证码 5 分钟内有效。如非本人操作，请忽略此邮件。</p>
<hr style="border:none;border-top:1px solid #eee">
<p style="color:#999;font-size:12px">Vibry AI Core · Digital Prefrontal Cortex Memory Proxy</p>
</div>""",
        )
        log.info(f"📧 [AgentMail] 验证码已发送至 {to_email}")
        return True

    except Exception as e:
        log.error(f"❌ [AgentMail] 发送失败: {e}")
        return False


# ================================================================
# SMTP
# ================================================================

def _send_via_smtp(to_email: str, code: str) -> bool:
    """通过 SMTP 发送"""
    if not SMTP_USER or not SMTP_PASS:
        log.warning("⚠️ SMTP 未配置")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Vibry AI Core — 密码重置验证码"
        msg["From"] = SMTP_FROM
        msg["To"] = to_email

        html = f"""<div style="max-width:480px;margin:0 auto;font-family:Arial,sans-serif">
<h2 style="color:#4fc3f7">🧠 Vibry AI Core</h2>
<p>您正在请求重置管理后台密码。</p>
<p style="font-size:28px;font-weight:bold;letter-spacing:6px;color:#333;background:#f0f0f0;padding:16px;text-align:center;border-radius:8px">{code}</p>
<p>验证码 5 分钟内有效。如非本人操作，请忽略此邮件。</p>
<hr style="border:none;border-top:1px solid #eee">
<p style="color:#999;font-size:12px">Vibry AI Core · 数字前额叶记忆代理</p>
</div>"""
        msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, to_email, msg.as_string())

        log.info(f"📧 [SMTP] 验证码已发送至 {to_email}")
        return True

    except Exception as e:
        log.error(f"❌ [SMTP] 发送失败: {e}")
        return False


# ================================================================
# Public API
# ================================================================

def send_verification_code(to_email: str, code: str) -> bool:
    """发送验证码邮件（自动选择 provider）

    Returns:
        是否发送成功
    """
    if EMAIL_PROVIDER == "agentmail":
        if not AGENTMAIL_API_KEY:
            log.warning("⚠️ AgentMail API Key 未配置")
            _log_fallback(to_email, code)
            return False
        ok = _send_via_agentmail(to_email, code)
    elif EMAIL_PROVIDER == "smtp":
        ok = _send_via_smtp(to_email, code)
    else:
        log.error(f"❌ 未知的 EMAIL_PROVIDER: {EMAIL_PROVIDER}")
        ok = False

    if not ok:
        _log_fallback(to_email, code)

    return ok


def _log_fallback(to_email: str, code: str):
    """后备：打印验证码到日志"""
    log.info(f"📧 [后备] 验证码 → {to_email}: {code}")


def get_email_status() -> dict:
    """返回邮件服务状态（供 admin API 使用）"""
    if EMAIL_PROVIDER == "agentmail":
        return {
            "provider": "agentmail",
            "configured": bool(AGENTMAIL_API_KEY),
            "from_email": AGENTMAIL_FROM_EMAIL,
            "desc": "AgentMail API" if AGENTMAIL_API_KEY else "AgentMail (not configured)",
        }
    elif EMAIL_PROVIDER == "smtp":
        return {
            "provider": "smtp",
            "configured": bool(SMTP_USER and SMTP_PASS),
            "from_email": SMTP_FROM or SMTP_USER,
            "desc": f"SMTP ({SMTP_HOST}:{SMTP_PORT})" if SMTP_USER else "SMTP (not configured)",
        }
    else:
        return {"provider": EMAIL_PROVIDER, "configured": False, "from_email": "", "desc": "Unknown"}
