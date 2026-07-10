"""Vibry AI Core — 邮件服务

发送验证码邮件（忘记密码流程）。
通过 AgentMail (agentmail.to) API 发送。
"""

import logging
import os
from agentmail import AgentMail

log = logging.getLogger("vibry.email")

# AgentMail 配置从环境变量读取
AGENTMAIL_API_KEY = os.getenv("AGENTMAIL_API_KEY", "")
AGENTMAIL_FROM_EMAIL = os.getenv("AGENTMAIL_FROM_EMAIL", "vibryai@agentmail.to")

_client = None
_inbox_id = None


def _get_client() -> AgentMail:
    """获取或初始化 AgentMail 客户端"""
    global _client
    if _client is None:
        if not AGENTMAIL_API_KEY:
            raise RuntimeError("AGENTMAIL_API_KEY not configured")
        _client = AgentMail(api_key=AGENTMAIL_API_KEY)
    return _client


def _get_inbox_id() -> str:
    """获取或创建发件邮箱 inbox"""
    global _inbox_id
    if _inbox_id is not None:
        return _inbox_id

    client = _get_client()
    try:
        # 先尝试列出已有 inbox
        inboxes = client.inboxes.list()
        if inboxes:
            _inbox_id = inboxes[0].inbox_id
            log.info(f"📬 AgentMail inbox: {_inbox_id} ({AGENTMAIL_FROM_EMAIL})")
            return _inbox_id
    except Exception as e:
        log.warning(f"Failed to list inboxes: {e}")

    # 没有则创建一个
    inbox = client.inboxes.create()
    _inbox_id = inbox.inbox_id
    log.info(f"📬 AgentMail inbox created: {_inbox_id}")
    return _inbox_id


def send_verification_code(to_email: str, code: str) -> bool:
    """发送验证码邮件

    Args:
        to_email: 收件人邮箱
        code: 6位验证码

    Returns:
        是否发送成功
    """
    if not AGENTMAIL_API_KEY:
        log.warning("⚠️ AgentMail API Key 未配置，验证码将打印到日志")
        log.info(f"📧 [模拟] 验证码发送至 {to_email}: {code}")
        return True  # 开发模式下模拟成功

    try:
        inbox_id = _get_inbox_id()
        client = _get_client()

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

        log.info(f"📧 验证码已通过 AgentMail 发送至 {to_email}")
        return True

    except Exception as e:
        log.error(f"❌ AgentMail 邮件发送失败: {e}")
        # 失败时打印到日志作为后备
        log.info(f"📧 [后备] 验证码: {code} → {to_email}")
        return False
