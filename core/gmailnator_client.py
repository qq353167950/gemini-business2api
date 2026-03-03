import os
import time
from datetime import datetime
from typing import Optional
from urllib.parse import quote, urlparse

import requests

from core.mail_utils import extract_verification_code
from core.proxy_utils import request_with_proxy_fallback


class GmailnatorClient:
    """Gmailnator temp mailbox client (RapidAPI)."""

    def __init__(
        self,
        base_url: str = "https://gmailnator.p.rapidapi.com",
        api_key: str = "",
        proxy: str = "",
        verify_ssl: bool = True,
        log_callback=None,
    ) -> None:
        self.base_url = (base_url or "https://gmailnator.p.rapidapi.com").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.proxy_url = (proxy or "").strip()
        self.verify_ssl = verify_ssl
        self.log_callback = log_callback

        self.email: Optional[str] = None
        self.password: Optional[str] = None  # Keep interface compatibility.

        parsed = urlparse(self.base_url)
        self.rapidapi_host = parsed.netloc or "gmailnator.p.rapidapi.com"

    def _log(self, level: str, message: str) -> None:
        if self.log_callback:
            try:
                self.log_callback(level, message)
            except Exception:
                pass

    def set_credentials(self, email: str, password: str = "") -> None:
        self.email = email
        self.password = password or ""

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        headers = kwargs.pop("headers", None) or {}
        headers.setdefault("Accept", "application/json")
        if method.upper() in ("POST", "PUT", "PATCH"):
            headers.setdefault("Content-Type", "application/json")
        headers.setdefault("x-rapidapi-host", self.rapidapi_host)
        if self.api_key and "x-rapidapi-key" not in {k.lower() for k in headers}:
            headers["x-rapidapi-key"] = self.api_key
        kwargs["headers"] = headers

        proxies = {"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else None

        self._log("info", f"📤 发送{method}请求: {url}")
        if "json" in kwargs and kwargs["json"] is not None:
            self._log("info", f"📦 请求体: {kwargs['json']}")

        res = request_with_proxy_fallback(
            requests.request,
            method,
            url,
            proxies=proxies,
            verify=self.verify_ssl,
            timeout=kwargs.pop("timeout", 20),
            **kwargs,
        )
        self._log("info", f"📥 收到响应: HTTP {res.status_code}")

        log_body = os.getenv("GMAILNATOR_LOG_BODY", "").strip().lower() in ("1", "true", "yes", "y", "on")
        if res.content and (log_body or res.status_code >= 400):
            try:
                self._log("info", f"📄 响应内容: {res.text[:600]}")
            except Exception:
                pass
        return res

    def register_account(self, domain: Optional[str] = None) -> bool:
        """Generate one temp email via /api/emails/generate."""
        _ = domain  # Not used by this provider.
        try:
            res = self._request("POST", f"{self.base_url}/api/emails/generate", json={})
            if res.status_code != 200:
                self._log("error", f"❌ Gmailnator 注册失败: HTTP {res.status_code}")
                return False

            data = res.json() if res.content else {}
            email = (data.get("email") or "").strip()
            status = (data.get("status") or "").lower()
            if not email or (status and status != "success"):
                self._log("error", f"❌ Gmailnator 注册失败: 响应缺少 email 或 status 异常 ({status})")
                return False

            self.email = email
            self.password = ""
            self._log("info", f"✅ Gmailnator 邮箱生成成功: {email}")
            return True
        except Exception as exc:
            self._log("error", f"❌ Gmailnator 注册异常: {exc}")
            return False

    def login(self) -> bool:
        return True

    @staticmethod
    def _parse_timestamp(value) -> Optional[datetime]:
        if value is None:
            return None
        try:
            if isinstance(value, (int, float)):
                ts = float(value)
                if ts > 1e12:
                    ts /= 1000.0
                return datetime.fromtimestamp(ts).astimezone().replace(tzinfo=None)
            raw = str(value).strip()
            if not raw:
                return None
            if raw.isdigit():
                ts = float(raw)
                if ts > 1e12:
                    ts /= 1000.0
                return datetime.fromtimestamp(ts).astimezone().replace(tzinfo=None)
            return None
        except Exception:
            return None

    def fetch_verification_code(self, since_time: Optional[datetime] = None) -> Optional[str]:
        if not self.email:
            self._log("error", "❌ 邮箱地址未设置")
            return None

        try:
            self._log("info", "📬 正在拉取 Gmailnator 邮件列表...")
            res = self._request(
                "POST",
                f"{self.base_url}/api/inbox/",
                json={"email": self.email, "limit": 20},
            )
            if res.status_code != 200:
                self._log("error", f"❌ 获取邮件列表失败: HTTP {res.status_code}")
                return None

            payload = res.json() if res.content else {}
            messages = payload.get("messages", [])
            if not isinstance(messages, list) or not messages:
                self._log("info", "📭 邮箱为空，暂无邮件")
                return None

            messages = sorted(
                messages,
                key=lambda item: self._parse_timestamp(item.get("timestamp")) or datetime.min,
                reverse=True,
            )

            for idx, msg in enumerate(messages, 1):
                msg_time = self._parse_timestamp(msg.get("timestamp"))
                if since_time and msg_time and msg_time < since_time:
                    continue

                preview = f"{msg.get('subject', '')} {msg.get('from', '')}"
                code = extract_verification_code(preview)
                if code:
                    self._log("info", f"✅ 找到验证码: {code}")
                    return code

                msg_id = str(msg.get("id") or "").strip()
                if not msg_id:
                    continue

                encoded_id = quote(msg_id, safe="")
                self._log("info", f"🔍 正在读取邮件 {idx}/{len(messages)} 详情...")
                detail_res = self._request("GET", f"{self.base_url}/api/inbox/{encoded_id}")
                if detail_res.status_code != 200:
                    self._log("warning", f"⚠️ 读取邮件详情失败: HTTP {detail_res.status_code}")
                    continue

                detail = detail_res.json() if detail_res.content else {}
                content = (
                    (detail.get("subject") or "")
                    + " "
                    + (detail.get("content") or "")
                    + " "
                    + (detail.get("from") or "")
                )
                code = extract_verification_code(content)
                if code:
                    self._log("info", f"✅ 找到验证码: {code}")
                    return code

            self._log("warning", "⚠️ 所有邮件中均未找到验证码")
            return None
        except Exception as exc:
            self._log("error", f"❌ 获取验证码异常: {exc}")
            return None

    def poll_for_code(
        self,
        timeout: int = 120,
        interval: int = 4,
        since_time: Optional[datetime] = None,
    ) -> Optional[str]:
        max_retries = max(1, timeout // interval)
        self._log("info", f"⏱️ 开始轮询验证码 (超时 {timeout}s, 间隔 {interval}s)")
        for i in range(1, max_retries + 1):
            self._log("info", f"🔄 第 {i}/{max_retries} 次轮询...")
            code = self.fetch_verification_code(since_time=since_time)
            if code:
                self._log("info", f"🎉 验证码获取成功: {code}")
                return code
            if i < max_retries:
                time.sleep(interval)
        self._log("error", f"❌ 验证码获取超时 ({timeout}s)")
        return None

