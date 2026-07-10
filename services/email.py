"""Vibry AI Core — 邮件服务

发送验证码邮件（忘记密码流程）。
支持 SMTP（QQ邮箱、Gmail、企业邮箱等）。
"""

import logging
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

log = logging.getLogger("vibry.email")

# SMTP 配置从环境变量读取
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)


def send_verification_code(to_email: str, code: str) -> bool:
    """发送验证码邮件

    Args:
        to_email: 收件人邮箱
        code: 6位验证码

    Returns:
        是否发送成功
    """
    if not SMTP_USER or not SMTP_PASS:
        log.warning("⚠️ SMTP 未配置，验证码将打印到日志")
        log.info(f"📧 [模拟] 验证码发送至 {to_email}: {code}")
        return True  # 开发模式下模拟成功

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

        log.info(f"📧 验证码已发送至 {to_email}")
        return True

    except Exception as e:
        log.error(f"❌ 邮件发送失败: {e}")
        return False
