import hashlib
import html
import logging
import os
import re
import secrets
import smtplib
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

logger = logging.getLogger(__name__)


def _smtp_configured() -> bool:
    """Return True when the minimum SMTP env vars are present."""
    host = os.getenv("SMTP_HOST", "")
    user = os.getenv("SMTP_USERNAME", "") or os.getenv("SMTP_USER", "")
    return bool(host and user)


def _base_url() -> str:
    """Return the configured application base URL for links in emails."""
    return os.getenv("APP_BASE_URL", "https://your-domain.com")


def _log_email_to_audit(*, to: str, subject: str, html_body: str) -> None:
    """Dev-mode fallback: persist the email in audit_logs so tests and
    local development can verify the flow without a real SMTP server."""
    try:
        from backend.database import add_audit_log

        add_audit_log(
            actor_type="system",
            action="EMAIL_SENT_DEV",
            target_type="email",
            target_id=to,
            metadata={
                "to": to,
                "subject": subject,
                "body_preview": html_body[:500],
            },
        )
    except Exception:
        logger.info("Dev-mode email logged: to=%s subject=%s", to, subject)


class EmailService:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        # 优先从数据库读取配置，如果没有则从环境变量读取
        self._load_config()

    def _load_config(self):
        """从数据库或环境变量加载SMTP配置"""
        try:
            from backend.database import get_setting
            
            # 从数据库读取配置
            db_host = get_setting("smtp_host")
            db_port = get_setting("smtp_port")
            db_user = get_setting("smtp_user")
            db_password = get_setting("smtp_password")
            db_from = get_setting("smtp_from")
            
            # 如果数据库有配置，使用数据库配置
            if db_host and db_user:
                self.smtp_host = db_host
                self.smtp_port = int(db_port or "587")
                self.smtp_user = db_user
                self.smtp_password = db_password or ""
                self.from_email = db_from or db_user
                self.use_tls = True  # 默认启用TLS
                return
        except Exception:
            pass
        
        # 如果数据库没有配置，从环境变量读取
        self.smtp_host = os.getenv("SMTP_HOST", "")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SMTP_USER", "")
        self.smtp_password = os.getenv("SMTP_PASSWORD", "")
        self.from_email = os.getenv("SMTP_FROM", self.smtp_user)
        self.use_tls = os.getenv("SMTP_USE_TLS", "true").lower() == "true"

    def reload_config(self):
        """重新加载配置（当管理员更新配置后调用）"""
        self._initialized = False
        self.__init__()

    @staticmethod
    def generate_verification_code(length: int = 6) -> str:
        return "".join(secrets.choice("0123456789") for _ in range(length))

    @staticmethod
    def generate_token() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def send_email(self, to_email: str, subject: str, html_content: str) -> tuple[bool, str]:
        if not self.smtp_host or not self.smtp_user:
            logger.info("Email skipped: SMTP not configured")
            return True, "Email configuration not set, logged to console"

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = Header(subject, "utf-8")
            msg["From"] = self.from_email
            msg["To"] = to_email

            text_part = MIMEText(html_content.replace("<[^<]+?>", ""), "plain", "utf-8")
            html_part = MIMEText(html_content, "html", "utf-8")

            msg.attach(text_part)
            msg.attach(html_part)

            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                if self.use_tls:
                    server.starttls()
                server.login(self.smtp_user, self.smtp_password)
                server.sendmail(self.from_email, [to_email], msg.as_string())

            return True, "Email sent successfully"
        except Exception as e:
            logger.warning("Email send failed: %s", type(e).__name__)
            return False, "邮件发送失败"

    def send_verification_email(self, to_email: str, code: str) -> tuple[bool, str]:
        subject = "MiniMax 中转平台 - 邮箱验证"
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: 'Helvetica Neue', Arial, sans-serif; background-color: #f5f5f5; margin: 0; padding: 20px; }}
                .container {{ max-width: 480px; margin: 0 auto; background: #ffffff; border-radius: 16px; overflow: hidden; box-shadow: 0 10px 40px rgba(0,0,0,0.1); }}
                .header {{ background: linear-gradient(135deg, #0f172a 0%, #334155 100%); padding: 40px; text-align: center; }}
                .header h1 {{ color: #ffffff; margin: 0; font-size: 24px; font-weight: 600; }}
                .content {{ padding: 40px; text-align: center; }}
                .code {{ font-size: 36px; font-weight: 700; color: #0f172a; letter-spacing: 8px; margin: 30px 0; padding: 20px; background: #f8fafc; border-radius: 12px; }}
                .text {{ color: #64748b; font-size: 14px; line-height: 1.6; }}
                .footer {{ padding: 20px 40px; background: #f8fafc; text-align: center; border-top: 1px solid #e2e8f0; }}
                .footer p {{ color: #94a3b8; font-size: 12px; margin: 0; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>MiniMax 中转平台</h1>
                </div>
                <div class="content">
                    <p class="text">您好，</p>
                    <p class="text">您的邮箱验证码为：</p>
                    <div class="code">{html.escape(code)}</div>
                    <p class="text">验证码有效期为 <strong>30分钟</strong>，请勿将验证码告知他人。</p>
                </div>
                <div class="footer">
                    <p>此邮件由系统自动发送，请勿回复。</p>
                </div>
            </div>
        </body>
        </html>
        """
        return self.send_email(to_email, subject, html_content)

    def send_password_reset_email(
        self, to_email: str, token: str, base_url: str
    ) -> tuple[bool, str]:
        reset_url = f"{base_url}/reset-password?token={token}&email={to_email}"
        subject = "MiniMax 中转平台 - 密码重置"
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: 'Helvetica Neue', Arial, sans-serif; background-color: #f5f5f5; margin: 0; padding: 20px; }}
                .container {{ max-width: 480px; margin: 0 auto; background: #ffffff; border-radius: 16px; overflow: hidden; box-shadow: 0 10px 40px rgba(0,0,0,0.1); }}
                .header {{ background: linear-gradient(135deg, #0f172a 0%, #334155 100%); padding: 40px; text-align: center; }}
                .header h1 {{ color: #ffffff; margin: 0; font-size: 24px; font-weight: 600; }}
                .content {{ padding: 40px; text-align: center; }}
                .button {{ display: inline-block; padding: 14px 32px; background: linear-gradient(135deg, #0f172a 0%, #334155 100%); color: #ffffff; text-decoration: none; border-radius: 10px; font-weight: 600; margin: 20px 0; }}
                .text {{ color: #64748b; font-size: 14px; line-height: 1.6; }}
                .link {{ word-break: break-all; font-size: 12px; color: #94a3b8; margin-top: 20px; }}
                .footer {{ padding: 20px 40px; background: #f8fafc; text-align: center; border-top: 1px solid #e2e8f0; }}
                .footer p {{ color: #94a3b8; font-size: 12px; margin: 0; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>MiniMax 中转平台</h1>
                </div>
                <div class="content">
                    <p class="text">您好，</p>
                    <p class="text">我们收到了您的密码重置请求。请点击下方按钮重置密码：</p>
                    <a href="{html.escape(reset_url)}" class="button">重置密码</a>
                    <p class="text">或者复制以下链接到浏览器打开：</p>
                    <p class="link">{html.escape(reset_url)}</p>
                    <p class="text">链接有效期为 <strong>30分钟</strong>。</p>
                    <p class="text">如果您未发起密码重置请求，请忽略此邮件。</p>
                </div>
                <div class="footer">
                    <p>此邮件由系统自动发送，请勿回复。</p>
                </div>
            </div>
        </body>
        </html>
        """
        return self.send_email(to_email, subject, html_content)

    # ------------------------------------------------------------------
    # Static async API (new)
    # ------------------------------------------------------------------

    @staticmethod
    async def send(
        *, to: str, subject: str, html_body: str, text_body: Optional[str] = None
    ) -> bool:
        """Send an email. Returns True on success, False on failure.

        Uses SMTP if configured (DB settings preferred, env fallback);
        otherwise logs the email to the audit_logs table (for dev) and
        returns True.
        """
        smtp_host = ""
        smtp_port = 587
        smtp_user = ""
        smtp_password = ""
        from_email = ""
        use_tls = True

        try:
            from backend.database import get_setting
            db_host = get_setting("smtp_host")
            db_user = get_setting("smtp_user")
            if db_host and db_user:
                smtp_host = db_host
                smtp_port = int(get_setting("smtp_port") or "587")
                smtp_user = db_user
                smtp_password = get_setting("smtp_password") or ""
                from_email = get_setting("smtp_from") or db_user
        except Exception:
            pass

        if not smtp_host or not smtp_user:
            smtp_host = os.getenv("SMTP_HOST", "")
            smtp_port = int(os.getenv("SMTP_PORT", "587"))
            smtp_user = os.getenv("SMTP_USERNAME", "") or os.getenv("SMTP_USER", "")
            smtp_password = os.getenv("SMTP_PASSWORD", "")
            from_email = os.getenv("SMTP_FROM_EMAIL", "") or os.getenv("SMTP_FROM", smtp_user)
            use_tls = os.getenv("SMTP_USE_TLS", "true").lower() == "true"

        if not smtp_host or not smtp_user:
            logger.info("Email (dev mode): to=%s subject=%s", to, subject)
            _log_email_to_audit(to=to, subject=subject, html_body=html_body)
            return True

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = Header(subject, "utf-8")
            msg["From"] = from_email
            msg["To"] = to

            plain = text_body or re.sub(r"<[^<]+?>", "", html_body)
            msg.attach(MIMEText(plain, "plain", "utf-8"))
            msg.attach(MIMEText(html_body, "html", "utf-8"))

            with smtplib.SMTP(smtp_host, smtp_port) as server:
                if use_tls:
                    server.starttls()
                server.login(smtp_user, smtp_password)
                server.sendmail(from_email, [to], msg.as_string())

            return True
        except Exception as e:
            logger.warning("Email send failed: %s — %s", type(e).__name__, e)
            return False

    @staticmethod
    async def send_password_reset(*, email: str, reset_url: str, username: str) -> bool:
        """Send a password reset email with a localized template."""
        subject = "API Hub — Password Reset / 密码重置"
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: 'Helvetica Neue', Arial, sans-serif; background-color: #f5f5f5; margin: 0; padding: 20px; }}
                .container {{ max-width: 480px; margin: 0 auto; background: #ffffff; border-radius: 16px; overflow: hidden; box-shadow: 0 10px 40px rgba(0,0,0,0.1); }}
                .header {{ background: linear-gradient(135deg, #0f172a 0%, #334155 100%); padding: 40px; text-align: center; }}
                .header h1 {{ color: #ffffff; margin: 0; font-size: 24px; font-weight: 600; }}
                .content {{ padding: 40px; text-align: center; }}
                .button {{ display: inline-block; padding: 14px 32px; background: linear-gradient(135deg, #0f172a 0%, #334155 100%); color: #ffffff; text-decoration: none; border-radius: 10px; font-weight: 600; margin: 20px 0; }}
                .text {{ color: #64748b; font-size: 14px; line-height: 1.6; }}
                .link {{ word-break: break-all; font-size: 12px; color: #94a3b8; margin-top: 20px; }}
                .footer {{ padding: 20px 40px; background: #f8fafc; text-align: center; border-top: 1px solid #e2e8f0; }}
                .footer p {{ color: #94a3b8; font-size: 12px; margin: 0; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>API Hub</h1>
                </div>
                <div class="content">
                    <p class="text">Hi {html.escape(username)},</p>
                    <p class="text">We received a request to reset your password. Click the button below to set a new password:</p>
                    <a href="{html.escape(reset_url)}" class="button">Reset Password / 重置密码</a>
                    <p class="text">Or copy this link into your browser:</p>
                    <p class="link">{html.escape(reset_url)}</p>
                    <p class="text">This link expires in <strong>1 hour</strong>.</p>
                    <p class="text">If you did not request a password reset, please ignore this email.</p>
                </div>
                <div class="footer">
                    <p>This is an automated message. Please do not reply.</p>
                </div>
            </div>
        </body>
        </html>
        """
        return await EmailService.send(to=email, subject=subject, html_body=html_body)

    @staticmethod
    async def send_subscription_expiry(
        *, email: str, username: str, plan_name: str, expires_at: datetime
    ) -> bool:
        """Send a subscription expiry reminder."""
        expires_str = expires_at.strftime("%Y-%m-%d %H:%M UTC")
        subject = "API Hub — Subscription Expiring Soon / 订阅即将到期"
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: 'Helvetica Neue', Arial, sans-serif; background-color: #f5f5f5; margin: 0; padding: 20px; }}
                .container {{ max-width: 480px; margin: 0 auto; background: #ffffff; border-radius: 16px; overflow: hidden; box-shadow: 0 10px 40px rgba(0,0,0,0.1); }}
                .header {{ background: linear-gradient(135deg, #0f172a 0%, #334155 100%); padding: 40px; text-align: center; }}
                .header h1 {{ color: #ffffff; margin: 0; font-size: 24px; font-weight: 600; }}
                .content {{ padding: 40px; text-align: center; }}
                .text {{ color: #64748b; font-size: 14px; line-height: 1.6; }}
                .highlight {{ background: #fef3c7; color: #92400e; padding: 12px 20px; border-radius: 10px; font-weight: 600; margin: 20px 0; }}
                .footer {{ padding: 20px 40px; background: #f8fafc; text-align: center; border-top: 1px solid #e2e8f0; }}
                .footer p {{ color: #94a3b8; font-size: 12px; margin: 0; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>API Hub</h1>
                </div>
                <div class="content">
                    <p class="text">Hi {html.escape(username)},</p>
                    <p class="text">Your <strong>{html.escape(plan_name)}</strong> subscription is expiring soon.</p>
                    <div class="highlight">Expires at: {html.escape(expires_str)}</div>
                    <p class="text">Please renew your subscription to continue enjoying all features.</p>
                </div>
                <div class="footer">
                    <p>This is an automated message. Please do not reply.</p>
                </div>
            </div>
        </body>
        </html>
        """
        return await EmailService.send(to=email, subject=subject, html_body=html_body)

    # ------------------------------------------------------------------
    # Order notification templates
    # ------------------------------------------------------------------

    @staticmethod
    async def send_order_approved(
        *, email: str, username: str, order_no: str, amount: float, credits: float
    ) -> bool:
        """Notify a user that their order has been approved and credits added."""
        base = _base_url()
        subject = f"订单 {order_no} 已通过 — Order Approved"
        text_body = (
            f"Hi {username},\n\n"
            f"Your order {order_no} has been approved.\n"
            f"Amount: ¥{amount:.2f}\n"
            f"Credits added: {credits}\n\n"
            f"View your wallet: {base}/wallet\n"
        )
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="UTF-8"></head>
        <body style="font-family:'Helvetica Neue',Arial,sans-serif;background:#f5f5f5;margin:0;padding:20px;">
          <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 10px 40px rgba(0,0,0,0.1);">
            <div style="background:linear-gradient(135deg,#0f172a 0%,#334155 100%);padding:40px;text-align:center;">
              <h1 style="color:#fff;margin:0;font-size:24px;font-weight:600;">API Hub</h1>
            </div>
            <div style="padding:40px;text-align:center;">
              <p style="color:#64748b;font-size:14px;line-height:1.6;">Hi {html.escape(username)},</p>
              <p style="color:#64748b;font-size:14px;line-height:1.6;">
                Your order <strong>{html.escape(order_no)}</strong> has been approved. / 您的订单已通过。
              </p>
              <ul style="color:#64748b;font-size:14px;line-height:1.8;text-align:left;display:inline-block;">
                <li>Amount / 金额: ¥{amount:.2f}</li>
                <li>Credits added / 充值积分: {credits}</li>
              </ul>
              <p style="color:#64748b;font-size:14px;line-height:1.6;">
                <a href="{html.escape(base)}/wallet" style="color:#0f172a;font-weight:600;">View your wallet / 查看钱包</a>
              </p>
            </div>
            <div style="padding:20px 40px;background:#f8fafc;text-align:center;border-top:1px solid #e2e8f0;">
              <p style="color:#94a3b8;font-size:12px;margin:0;">This is an automated message. Please do not reply.</p>
            </div>
          </div>
        </body>
        </html>
        """
        return await EmailService.send(
            to=email, subject=subject, html_body=html_body, text_body=text_body
        )

    @staticmethod
    async def send_order_rejected(
        *, email: str, username: str, order_no: str, reason: str = None
    ) -> bool:
        """Notify a user that their order has been rejected."""
        base = _base_url()
        subject = f"订单 {order_no} 已被拒绝 — Order Rejected"
        reason_line = f"Reason / 原因: {reason}\n" if reason else ""
        reason_html = (
            f'<p style="color:#64748b;font-size:14px;line-height:1.6;">Reason / 原因: {html.escape(reason)}</p>'
            if reason
            else ""
        )
        text_body = (
            f"Hi {username},\n\n"
            f"Your order {order_no} has been rejected.\n"
            f"{reason_line}\n"
            f"View your orders: {base}/billing\n"
        )
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="UTF-8"></head>
        <body style="font-family:'Helvetica Neue',Arial,sans-serif;background:#f5f5f5;margin:0;padding:20px;">
          <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 10px 40px rgba(0,0,0,0.1);">
            <div style="background:linear-gradient(135deg,#0f172a 0%,#334155 100%);padding:40px;text-align:center;">
              <h1 style="color:#fff;margin:0;font-size:24px;font-weight:600;">API Hub</h1>
            </div>
            <div style="padding:40px;text-align:center;">
              <p style="color:#64748b;font-size:14px;line-height:1.6;">Hi {html.escape(username)},</p>
              <p style="color:#64748b;font-size:14px;line-height:1.6;">
                Your order <strong>{html.escape(order_no)}</strong> has been rejected. / 您的订单已被拒绝。
              </p>
              {reason_html}
              <p style="color:#64748b;font-size:14px;line-height:1.6;">
                <a href="{html.escape(base)}/billing" style="color:#0f172a;font-weight:600;">View your orders / 查看订单</a>
              </p>
            </div>
            <div style="padding:20px 40px;background:#f8fafc;text-align:center;border-top:1px solid #e2e8f0;">
              <p style="color:#94a3b8;font-size:12px;margin:0;">This is an automated message. Please do not reply.</p>
            </div>
          </div>
        </body>
        </html>
        """
        return await EmailService.send(
            to=email, subject=subject, html_body=html_body, text_body=text_body
        )

    @staticmethod
    async def send_order_refunded(
        *, email: str, username: str, order_no: str, amount: float
    ) -> bool:
        """Notify a user that their order has been refunded."""
        base = _base_url()
        subject = f"订单 {order_no} 已退款 — Order Refunded"
        text_body = (
            f"Hi {username},\n\n"
            f"Your order {order_no} has been refunded.\n"
            f"Refund amount / 退款金额: ¥{amount:.2f}\n\n"
            f"View your wallet: {base}/wallet\n"
        )
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="UTF-8"></head>
        <body style="font-family:'Helvetica Neue',Arial,sans-serif;background:#f5f5f5;margin:0;padding:20px;">
          <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 10px 40px rgba(0,0,0,0.1);">
            <div style="background:linear-gradient(135deg,#0f172a 0%,#334155 100%);padding:40px;text-align:center;">
              <h1 style="color:#fff;margin:0;font-size:24px;font-weight:600;">API Hub</h1>
            </div>
            <div style="padding:40px;text-align:center;">
              <p style="color:#64748b;font-size:14px;line-height:1.6;">Hi {html.escape(username)},</p>
              <p style="color:#64748b;font-size:14px;line-height:1.6;">
                Your order <strong>{html.escape(order_no)}</strong> has been refunded. / 您的订单已退款。
              </p>
              <p style="color:#64748b;font-size:14px;line-height:1.6;">
                Refund amount / 退款金额: <strong>¥{amount:.2f}</strong>
              </p>
              <p style="color:#64748b;font-size:14px;line-height:1.6;">
                <a href="{html.escape(base)}/wallet" style="color:#0f172a;font-weight:600;">View your wallet / 查看钱包</a>
              </p>
            </div>
            <div style="padding:20px 40px;background:#f8fafc;text-align:center;border-top:1px solid #e2e8f0;">
              <p style="color:#94a3b8;font-size:12px;margin:0;">This is an automated message. Please do not reply.</p>
            </div>
          </div>
        </body>
        </html>
        """
        return await EmailService.send(
            to=email, subject=subject, html_body=html_body, text_body=text_body
        )

    # ------------------------------------------------------------------
    # Subscription notification templates
    # ------------------------------------------------------------------

    @staticmethod
    async def send_subscription_renewed(
        *, email: str, username: str, plan_name: str, expires_at: datetime
    ) -> bool:
        """Notify a user that their subscription has been renewed."""
        base = _base_url()
        expires_str = expires_at.strftime("%Y-%m-%d %H:%M UTC")
        subject = "订阅已续期 — Subscription Renewed"
        text_body = (
            f"Hi {username},\n\n"
            f"Your {plan_name} subscription has been renewed.\n"
            f"New expiry / 新到期时间: {expires_str}\n\n"
            f"View your subscriptions: {base}/subscriptions\n"
        )
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="UTF-8"></head>
        <body style="font-family:'Helvetica Neue',Arial,sans-serif;background:#f5f5f5;margin:0;padding:20px;">
          <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 10px 40px rgba(0,0,0,0.1);">
            <div style="background:linear-gradient(135deg,#0f172a 0%,#334155 100%);padding:40px;text-align:center;">
              <h1 style="color:#fff;margin:0;font-size:24px;font-weight:600;">API Hub</h1>
            </div>
            <div style="padding:40px;text-align:center;">
              <p style="color:#64748b;font-size:14px;line-height:1.6;">Hi {html.escape(username)},</p>
              <p style="color:#64748b;font-size:14px;line-height:1.6;">
                Your <strong>{html.escape(plan_name)}</strong> subscription has been renewed. / 您的订阅已续期。
              </p>
              <div style="background:#d1fae5;color:#065f46;padding:12px 20px;border-radius:10px;font-weight:600;margin:20px 0;">
                New expiry / 新到期时间: {html.escape(expires_str)}
              </div>
              <p style="color:#64748b;font-size:14px;line-height:1.6;">
                <a href="{html.escape(base)}/subscriptions" style="color:#0f172a;font-weight:600;">View your subscriptions / 查看订阅</a>
              </p>
            </div>
            <div style="padding:20px 40px;background:#f8fafc;text-align:center;border-top:1px solid #e2e8f0;">
              <p style="color:#94a3b8;font-size:12px;margin:0;">This is an automated message. Please do not reply.</p>
            </div>
          </div>
        </body>
        </html>
        """
        return await EmailService.send(
            to=email, subject=subject, html_body=html_body, text_body=text_body
        )

    @staticmethod
    async def send_subscription_expired(*, email: str, username: str, plan_name: str) -> bool:
        """Notify a user that their subscription has expired."""
        base = _base_url()
        subject = "订阅已到期 — Subscription Expired"
        text_body = (
            f"Hi {username},\n\n"
            f"Your {plan_name} subscription has expired. / 您的订阅已到期。\n"
            f"You have been moved to the free tier.\n\n"
            f"Renew now: {base}/subscriptions\n"
        )
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="UTF-8"></head>
        <body style="font-family:'Helvetica Neue',Arial,sans-serif;background:#f5f5f5;margin:0;padding:20px;">
          <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 10px 40px rgba(0,0,0,0.1);">
            <div style="background:linear-gradient(135deg,#0f172a 0%,#334155 100%);padding:40px;text-align:center;">
              <h1 style="color:#fff;margin:0;font-size:24px;font-weight:600;">API Hub</h1>
            </div>
            <div style="padding:40px;text-align:center;">
              <p style="color:#64748b;font-size:14px;line-height:1.6;">Hi {html.escape(username)},</p>
              <p style="color:#64748b;font-size:14px;line-height:1.6;">
                Your <strong>{html.escape(plan_name)}</strong> subscription has expired. / 您的订阅已到期。
              </p>
              <div style="background:#fee2e2;color:#991b1b;padding:12px 20px;border-radius:10px;font-weight:600;margin:20px 0;">
                You have been moved to the free tier. / 您已切换至免费套餐。
              </div>
              <p style="color:#64748b;font-size:14px;line-height:1.6;">
                <a href="{html.escape(base)}/subscriptions" style="color:#0f172a;font-weight:600;">Renew now / 立即续期</a>
              </p>
            </div>
            <div style="padding:20px 40px;background:#f8fafc;text-align:center;border-top:1px solid #e2e8f0;">
              <p style="color:#94a3b8;font-size:12px;margin:0;">This is an automated message. Please do not reply.</p>
            </div>
          </div>
        </body>
        </html>
        """
        return await EmailService.send(
            to=email, subject=subject, html_body=html_body, text_body=text_body
        )

    @staticmethod
    async def send_auto_recharge_triggered(
        *, email: str, username: str, amount: float, order_no: str
    ) -> bool:
        """Notify a user that an automatic recharge has been triggered."""
        base = _base_url()
        subject = "自动充值已触发 — Auto Recharge Triggered"
        text_body = (
            f"Hi {username},\n\n"
            f"An automatic recharge has been triggered for your account.\n"
            f"Amount / 金额: ¥{amount:.2f}\n"
            f"Order / 订单: {order_no}\n\n"
            f"View your wallet: {base}/wallet\n"
        )
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="UTF-8"></head>
        <body style="font-family:'Helvetica Neue',Arial,sans-serif;background:#f5f5f5;margin:0;padding:20px;">
          <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 10px 40px rgba(0,0,0,0.1);">
            <div style="background:linear-gradient(135deg,#0f172a 0%,#334155 100%);padding:40px;text-align:center;">
              <h1 style="color:#fff;margin:0;font-size:24px;font-weight:600;">API Hub</h1>
            </div>
            <div style="padding:40px;text-align:center;">
              <p style="color:#64748b;font-size:14px;line-height:1.6;">Hi {html.escape(username)},</p>
              <p style="color:#64748b;font-size:14px;line-height:1.6;">
                An automatic recharge has been triggered for your account. / 您的账户已触发自动充值。
              </p>
              <ul style="color:#64748b;font-size:14px;line-height:1.8;text-align:left;display:inline-block;">
                <li>Amount / 金额: ¥{amount:.2f}</li>
                <li>Order / 订单: {html.escape(order_no)}</li>
              </ul>
              <p style="color:#64748b;font-size:14px;line-height:1.6;">
                <a href="{html.escape(base)}/wallet" style="color:#0f172a;font-weight:600;">View your wallet / 查看钱包</a>
              </p>
            </div>
            <div style="padding:20px 40px;background:#f8fafc;text-align:center;border-top:1px solid #e2e8f0;">
              <p style="color:#94a3b8;font-size:12px;margin:0;">This is an automated message. Please do not reply.</p>
            </div>
          </div>
        </body>
        </html>
        """
        return await EmailService.send(
            to=email, subject=subject, html_body=html_body, text_body=text_body
        )

    def send_welcome_email(self, to_email: str, username: str) -> tuple[bool, str]:
        subject = "欢迎使用 MiniMax 中转平台"
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: 'Helvetica Neue', Arial, sans-serif; background-color: #f5f5f5; margin: 0; padding: 20px; }}
                .container {{ max-width: 480px; margin: 0 auto; background: #ffffff; border-radius: 16px; overflow: hidden; box-shadow: 0 10px 40px rgba(0,0,0,0.1); }}
                .header {{ background: linear-gradient(135deg, #0f172a 0%, #334155 100%); padding: 40px; text-align: center; }}
                .header h1 {{ color: #ffffff; margin: 0; font-size: 24px; font-weight: 600; }}
                .content {{ padding: 40px; text-align: center; }}
                .content h2 {{ color: #0f172a; margin: 0 0 20px; font-size: 20px; }}
                .text {{ color: #64748b; font-size: 14px; line-height: 1.6; }}
                .footer {{ padding: 20px 40px; background: #f8fafc; text-align: center; border-top: 1px solid #e2e8f0; }}
                .footer p {{ color: #94a3b8; font-size: 12px; margin: 0; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>MiniMax 中转平台</h1>
                </div>
                <div class="content">
                    <h2>欢迎，{html.escape(username)}！</h2>
                    <p class="text">感谢您注册 MiniMax 中转平台。您的账号已成功创建，现在可以开始使用了。</p>
                    <p class="text">如有任何问题，请联系管理员。</p>
                </div>
                <div class="footer">
                    <p>此邮件由系统自动发送，请勿回复。</p>
                </div>
            </div>
        </body>
        </html>
        """
        return self.send_email(to_email, subject, html_content)
