import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from core.account import load_accounts_from_source
from core.base_task_service import BaseTask, BaseTaskService, TaskCancelledError, TaskStatus
from core.config import config
from core.mail_providers import create_temp_mail_client
from core.gemini_automation import GeminiAutomation
from core.microsoft_mail_client import MicrosoftMailClient
from core.proxy_utils import parse_proxy_setting

logger = logging.getLogger("gemini.login")

# 常量定义
CONFIG_CHECK_INTERVAL_SECONDS = 60  # 配置检查间隔（秒）


@dataclass
class LoginTask(BaseTask):
    """登录任务数据类"""
    account_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """转换为字典"""
        base_dict = super().to_dict()
        base_dict["account_ids"] = self.account_ids
        return base_dict


class LoginService(BaseTaskService[LoginTask]):
    """登录服务类 - 统一任务管理"""

    def __init__(
        self,
        multi_account_mgr,
        http_client,
        user_agent: str,
        retry_policy,
        session_cache_ttl_seconds: int,
        global_stats_provider: Callable[[], dict],
        set_multi_account_mgr: Optional[Callable[[Any], None]] = None,
    ) -> None:
        super().__init__(
            multi_account_mgr,
            http_client,
            user_agent,
            retry_policy,
            session_cache_ttl_seconds,
            global_stats_provider,
            set_multi_account_mgr,
            log_prefix="REFRESH",
        )
        self._is_polling = False
        # 防重复：记录每个账号最后一次成功刷新的时间戳
        self._refresh_timestamps: Dict[str, float] = {}
        # cron 触发记录：避免同一时间点当天重复触发
        self._triggered_today: set = set()

    def _get_running_task(self) -> Optional[LoginTask]:
        """获取正在运行或等待中的任务"""
        for task in self._tasks.values():
            if isinstance(task, LoginTask) and task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                return task
        return None

    async def start_login(self, account_ids: List[str]) -> LoginTask:
        """
        启动登录任务 - 统一任务管理
        - 如果有正在运行的任务，将新账户添加到该任务（去重）
        - 如果没有正在运行的任务，创建新任务
        """
        async with self._lock:
            if not account_ids:
                raise ValueError("账户列表不能为空")

            # 检查是否有正在运行的任务
            running_task = self._get_running_task()

            if running_task:
                # 将新账户添加到现有任务（去重）
                new_accounts = [aid for aid in account_ids if aid not in running_task.account_ids]

                if new_accounts:
                    running_task.account_ids.extend(new_accounts)
                    self._append_log(
                        running_task,
                        "info",
                        f"📝 添加 {len(new_accounts)} 个账户到现有任务 (总计: {len(running_task.account_ids)})"
                    )
                else:
                    self._append_log(running_task, "info", "📝 所有账户已在当前任务中")

                return running_task

            # 创建新任务
            task = LoginTask(id=str(uuid.uuid4()), account_ids=list(account_ids))
            self._tasks[task.id] = task
            self._append_log(task, "info", f"📝 创建刷新任务 (账号数量: {len(task.account_ids)})")

            # 直接启动任务
            self._current_task_id = task.id
            asyncio.create_task(self._run_task_directly(task))
            return task

    async def _run_task_directly(self, task: LoginTask) -> None:
        """直接执行任务"""
        try:
            await self._run_one_task(task)
        finally:
            # 任务完成后清理
            async with self._lock:
                if self._current_task_id == task.id:
                    self._current_task_id = None

    def _execute_task(self, task: LoginTask):
        return self._run_login_async(task)

    async def _run_login_async(self, task: LoginTask) -> None:
        """异步执行登录任务（支持取消）。"""
        loop = asyncio.get_running_loop()
        self._append_log(task, "info", f"🚀 刷新任务已启动 (共 {len(task.account_ids)} 个账号)")

        for idx, account_id in enumerate(task.account_ids, 1):
            # 检查是否请求取消
            if task.cancel_requested:
                self._append_log(task, "warning", f"login task cancelled: {task.cancel_reason or 'cancelled'}")
                task.status = TaskStatus.CANCELLED
                task.finished_at = time.time()
                return

            try:
                self._append_log(task, "info", f"📊 进度: {idx}/{len(task.account_ids)}")
                self._append_log(task, "info", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                self._append_log(task, "info", f"🔄 开始刷新账号: {account_id}")
                self._append_log(task, "info", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                result = await loop.run_in_executor(self._executor, self._refresh_one, account_id, task)
            except TaskCancelledError:
                # 线程侧已触发取消，直接结束任务
                task.status = TaskStatus.CANCELLED
                task.finished_at = time.time()
                return
            except Exception as exc:
                result = {"success": False, "email": account_id, "error": str(exc)}
            task.progress += 1
            task.results.append(result)

            if result.get("success"):
                task.success_count += 1
                # 记录刷新成功时间（防重复层 1）
                self._refresh_timestamps[account_id] = time.time()
                self._append_log(task, "info", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                self._append_log(task, "info", f"🎉 刷新成功: {account_id}")
                self._append_log(task, "info", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            else:
                task.fail_count += 1
                error = result.get('error', '未知错误')
                self._append_log(task, "error", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                self._append_log(task, "error", f"❌ 刷新失败: {account_id}")
                self._append_log(task, "error", f"❌ 失败原因: {error}")
                self._append_log(task, "error", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

                # 403 自动禁用账户
                if "403" in error:
                    try:
                        accounts = load_accounts_from_source()
                        for acc in accounts:
                            if acc.get("id") == account_id:
                                acc["disabled"] = True
                                acc["disabled_reason"] = "403 Access Restricted"
                                break
                        self._apply_accounts_update(accounts)
                        # 同步到内存中的 account manager
                        if account_id in self.multi_account_mgr.accounts:
                            mgr = self.multi_account_mgr.accounts[account_id]
                            mgr.config.disabled = True
                            mgr.disabled_reason = "403 Access Restricted"
                        self._append_log(task, "error", f"⛔ 已自动禁用账户: {account_id}")
                    except Exception as e:
                        self._append_log(task, "warning", f"⚠️ 自动禁用失败: {e}")

            # 账号之间等待 10 秒，避免资源争抢和风控
            if idx < len(task.account_ids) and not task.cancel_requested:
                self._append_log(task, "info", "⏳ 等待 10 秒后处理下一个账号...")
                await asyncio.sleep(10)

        if task.cancel_requested:
            task.status = TaskStatus.CANCELLED
        else:
            task.status = TaskStatus.SUCCESS if task.fail_count == 0 else TaskStatus.FAILED
        task.finished_at = time.time()
        self._append_log(task, "info", f"login task finished ({task.success_count}/{len(task.account_ids)})")
        self._current_task_id = None
        self._append_log(task, "info", f"🏁 刷新任务完成 (成功: {task.success_count}, 失败: {task.fail_count}, 总计: {len(task.account_ids)})")

    def _refresh_one(self, account_id: str, task: LoginTask) -> dict:
        """刷新单个账户"""
        accounts = load_accounts_from_source()
        account = next((acc for acc in accounts if acc.get("id") == account_id), None)
        if not account:
            return {"success": False, "email": account_id, "error": "账号不存在"}

        if account.get("disabled"):
            return {"success": False, "email": account_id, "error": "账号已禁用"}

        # 获取邮件提供商
        mail_provider = (account.get("mail_provider") or "").lower()
        if not mail_provider:
            if account.get("mail_client_id") or account.get("mail_refresh_token"):
                mail_provider = "microsoft"
            else:
                mail_provider = "duckmail"

        # 获取邮件配置
        mail_password = account.get("mail_password") or account.get("email_password")
        mail_client_id = account.get("mail_client_id")
        mail_refresh_token = account.get("mail_refresh_token")
        mail_tenant = account.get("mail_tenant") or "consumers"
        proxy_for_auth, _ = parse_proxy_setting(config.basic.proxy_for_auth)

        def log_cb(level, message):
            self._append_log(task, level, f"[{account_id}] {message}")

        log_cb("info", f"📧 邮件提供商: {mail_provider}")

        # 创建邮件客户端
        if mail_provider == "microsoft":
            if not mail_client_id or not mail_refresh_token:
                return {"success": False, "email": account_id, "error": "Microsoft OAuth 配置缺失"}
            mail_address = account.get("mail_address") or account_id
            client = MicrosoftMailClient(
                client_id=mail_client_id,
                refresh_token=mail_refresh_token,
                tenant=mail_tenant,
                proxy=proxy_for_auth,
                log_callback=log_cb,
            )
            client.set_credentials(mail_address)
        elif mail_provider in ("duckmail", "moemail", "freemail", "gptmail", "cfmail", "gmailnator"):
            if mail_provider not in ("freemail", "gptmail", "cfmail", "gmailnator") and not mail_password:
                error_message = "邮箱密码缺失" if mail_provider == "duckmail" else "mail password (email_id) missing"
                return {"success": False, "email": account_id, "error": error_message}
            if mail_provider == "freemail" and not account.get("mail_jwt_token") and not config.basic.freemail_jwt_token:
                return {"success": False, "email": account_id, "error": "Freemail JWT Token 未配置"}
            if mail_provider == "gmailnator" and not account.get("mail_api_key") and not config.basic.gmailnator_api_key:
                return {"success": False, "email": account_id, "error": "Gmailnator RapidAPI Key 未配置"}

            # 创建邮件客户端，优先使用账户级别配置
            mail_address = account.get("mail_address") or account_id

            # 构建账户级别的配置参数
            account_config = {}
            if account.get("mail_base_url"):
                account_config["base_url"] = account["mail_base_url"]
            if account.get("mail_api_key"):
                account_config["api_key"] = account["mail_api_key"]
            if account.get("mail_jwt_token"):
                account_config["jwt_token"] = account["mail_jwt_token"]
            if account.get("mail_verify_ssl") is not None:
                account_config["verify_ssl"] = account["mail_verify_ssl"]
            if account.get("mail_domain"):
                account_config["domain"] = account["mail_domain"]

            # 创建客户端（工厂会优先使用传入的参数，其次使用全局配置）
            client = create_temp_mail_client(
                mail_provider,
                log_cb=log_cb,
                **account_config
            )
            client.set_credentials(mail_address, mail_password)
            if mail_provider == "moemail":
                client.email_id = mail_password  # 设置 email_id 用于获取邮件
        else:
            return {"success": False, "email": account_id, "error": f"不支持的邮件提供商: {mail_provider}"}

        headless = config.basic.browser_headless

        log_cb("info", f"🌐 启动浏览器 (无头模式={headless})...")

        automation = GeminiAutomation(
            user_agent=self.user_agent,
            proxy=proxy_for_auth,
            headless=headless,
            log_callback=log_cb,
        )
        # 允许外部取消时立刻关闭浏览器
        self._add_cancel_hook(task.id, lambda: getattr(automation, "stop", lambda: None)())
        try:
            log_cb("info", "🔐 执行 Gemini 自动登录...")
            result = automation.login_and_extract(account_id, client)
        except Exception as exc:
            log_cb("error", f"❌ 自动登录异常: {exc}")
            return {"success": False, "email": account_id, "error": str(exc)}
        if not result.get("success"):
            error = result.get("error", "自动化流程失败")
            log_cb("error", f"❌ 自动登录失败: {error}")
            return {"success": False, "email": account_id, "error": error}

        log_cb("info", "✅ Gemini 登录成功，正在保存配置...")

        # 更新账户配置
        config_data = result["config"]
        config_data["mail_provider"] = mail_provider
        if mail_provider in ("freemail", "gptmail", "gmailnator"):
            config_data["mail_password"] = ""
        elif mail_provider == "cfmail":
            config_data["mail_password"] = mail_password  # 保留 JWT token
        else:
            config_data["mail_password"] = mail_password
        if mail_provider == "microsoft":
            config_data["mail_address"] = account.get("mail_address") or account_id
            config_data["mail_client_id"] = mail_client_id
            config_data["mail_refresh_token"] = mail_refresh_token
            config_data["mail_tenant"] = mail_tenant
        config_data["disabled"] = account.get("disabled", False)

        for acc in accounts:
            if acc.get("id") == account_id:
                acc.update(config_data)
                break

        self._apply_accounts_update(accounts)

        # 清除该账户的所有冷却状态（重新登录后恢复可用）
        if account_id in self.multi_account_mgr.accounts:
            account_mgr = self.multi_account_mgr.accounts[account_id]
            account_mgr.quota_cooldowns.clear()  # 清除配额冷却
            account_mgr.is_available = True  # 恢复可用状态
            log_cb("info", "✅ 已清除账户冷却状态")

        log_cb("info", "✅ 配置已保存到数据库")
        return {"success": True, "email": account_id, "config": config_data}


    def _get_expiring_accounts(self) -> List[str]:
        """获取即将过期的账户列表"""
        accounts = load_accounts_from_source()
        expiring = []
        beijing_tz = timezone(timedelta(hours=8))
        now = datetime.now(beijing_tz)

        for account in accounts:
            account_id = account.get("id")
            if not account_id:
                continue

            if account.get("disabled"):
                continue
            mail_provider = (account.get("mail_provider") or "").lower()
            if not mail_provider:
                if account.get("mail_client_id") or account.get("mail_refresh_token"):
                    mail_provider = "microsoft"
                else:
                    mail_provider = "duckmail"

            mail_password = account.get("mail_password") or account.get("email_password")
            if mail_provider == "microsoft":
                if not account.get("mail_client_id") or not account.get("mail_refresh_token"):
                    continue
            elif mail_provider in ("duckmail", "moemail"):
                if not mail_password:
                    continue
            elif mail_provider == "freemail":
                if not config.basic.freemail_jwt_token:
                    continue
            elif mail_provider == "gptmail":
                # GPTMail 不需要密码，允许直接刷新
                pass
            elif mail_provider == "gmailnator":
                # Gmailnator requires RapidAPI key (account-level or global).
                if not account.get("mail_api_key") and not config.basic.gmailnator_api_key:
                    continue
            elif mail_provider == "cfmail":
                # cfmail 需要 JWT token（存在 mail_password 中）或全局配置
                if not mail_password and not config.basic.cfmail_api_key:
                    continue
            else:
                continue
            expires_at = account.get("expires_at")
            if not expires_at:
                continue

            try:
                expire_time = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S")
                expire_time = expire_time.replace(tzinfo=beijing_tz)
                remaining = (expire_time - now).total_seconds() / 3600
            except Exception:
                continue

            if remaining > config.basic.refresh_window_hours:
                continue

            # 冷却检查（防重复层 1）：跳过最近刚刷新过的账号
            cooldown_seconds = config.retry.refresh_cooldown_hours * 3600
            if account_id in self._refresh_timestamps:
                elapsed = time.time() - self._refresh_timestamps[account_id]
                if elapsed < cooldown_seconds:
                    logger.debug(f"[LOGIN] skip {account_id}: refreshed {elapsed/3600:.1f}h ago, cooldown {config.retry.refresh_cooldown_hours}h")
                    continue

            if True:  # 通过所有检查
                expiring.append(account_id)

        return expiring

    async def check_and_refresh(self) -> Optional[LoginTask]:
        if os.environ.get("ACCOUNTS_CONFIG"):
            logger.info("[LOGIN] ACCOUNTS_CONFIG set, skipping refresh")
            return None
        expiring_accounts = self._get_expiring_accounts()
        if not expiring_accounts:
            logger.debug("[LOGIN] no accounts need refresh")
            return None

        try:
            return await self.start_login(expiring_accounts)
        except Exception as exc:
            logger.warning("[LOGIN] refresh enqueue failed: %s", exc)
            return None

    @staticmethod
    def _parse_cron(cron_str: str) -> dict:
        """解析 cron 表达式。
        支持两种格式:
          - '08:00,20:00' → {'mode': 'daily', 'times': ['08:00', '20:00']}
          - '*/120'       → {'mode': 'interval', 'minutes': 120}
        """
        cron_str = cron_str.strip()
        if cron_str.startswith("*/"):
            try:
                minutes = int(cron_str[2:])
                return {"mode": "interval", "minutes": max(minutes, 5)}
            except ValueError:
                return {"mode": "interval", "minutes": 120}
        else:
            times = [t.strip() for t in cron_str.split(",") if t.strip()]
            valid = []
            for t in times:
                parts = t.split(":")
                if len(parts) == 2:
                    try:
                        h, m = int(parts[0]), int(parts[1])
                        if 0 <= h <= 23 and 0 <= m <= 59:
                            valid.append(f"{h:02d}:{m:02d}")
                    except ValueError:
                        pass
            return {"mode": "daily", "times": valid or ["08:00", "20:00"]}

    async def _wait_for_next_trigger(self) -> None:
        """等待下一个触发时间点。
        - interval 模式：等 N 分钟
        - daily 模式：等到下一个匹配的 HH:MM，每个时间点每天只触发一次
        """
        cron_str = config.retry.scheduled_refresh_cron
        # 向后兼容：如果旧字段有值且新字段是默认值，转换为 interval 模式
        if (not cron_str or cron_str == "08:00,20:00") and config.retry.scheduled_refresh_interval_minutes > 0:
            cron_str = f"*/{config.retry.scheduled_refresh_interval_minutes}"

        cron = self._parse_cron(cron_str)

        if cron["mode"] == "interval":
            minutes = cron["minutes"]
            logger.info(f"[LOGIN] 间隔模式：{minutes} 分钟后下一次检查")
            await asyncio.sleep(minutes * 60)
            return

        # daily 模式：每秒检查一次当前时间是否命中
        beijing_tz = timezone(timedelta(hours=8))
        while self._is_polling:
            now = datetime.now(beijing_tz)
            current_time = now.strftime("%H:%M")
            today_str = now.strftime("%Y-%m-%d")

            # 新的一天，清空触发记录
            old_keys = [k for k in self._triggered_today if not k.startswith(today_str)]
            for k in old_keys:
                self._triggered_today.discard(k)

            for t in cron["times"]:
                trigger_key = f"{today_str}_{t}"
                if current_time == t and trigger_key not in self._triggered_today:
                    self._triggered_today.add(trigger_key)
                    logger.info(f"[LOGIN] 定时触发: {t}")
                    return

            await asyncio.sleep(30)  # 每 30 秒检查一次

    async def _wait_task_complete(self, task: LoginTask) -> None:
        """等待任务完成（防重复层 3：串行等待）"""
        while task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
            await asyncio.sleep(5)

    async def start_polling(self) -> None:
        if self._is_polling:
            logger.warning("[LOGIN] polling already running")
            return

        self._is_polling = True
        logger.info("[LOGIN] 智能刷新调度器已启动")
        try:
            while self._is_polling:
                # 检查是否启用
                if not config.retry.scheduled_refresh_enabled:
                    logger.debug("[LOGIN] scheduled refresh disabled")
                    await asyncio.sleep(CONFIG_CHECK_INTERVAL_SECONDS)
                    continue

                # 等待下一个触发时间点
                await self._wait_for_next_trigger()
                if not self._is_polling:
                    break

                # 获取所有待刷新账号（已含冷却过滤）
                expiring = self._get_expiring_accounts()
                if not expiring:
                    logger.info("[LOGIN] 本轮无需刷新的账号")
                    continue

                batch_size = config.retry.refresh_batch_size
                total_batches = (len(expiring) + batch_size - 1) // batch_size
                logger.info(f"[LOGIN] 待刷新 {len(expiring)} 个账号，分 {total_batches} 批（每批 {batch_size} 个）")

                # 分批执行
                for i in range(0, len(expiring), batch_size):
                    if not self._is_polling:
                        break

                    batch = expiring[i:i + batch_size]
                    batch_num = i // batch_size + 1
                    logger.info(f"[LOGIN] 第 {batch_num}/{total_batches} 批: {batch}")

                    try:
                        task = await self.start_login(batch)
                        # 等待这批完成（防重复层 3）
                        await self._wait_task_complete(task)
                        logger.info(f"[LOGIN] 第 {batch_num} 批完成 (成功: {task.success_count}, 失败: {task.fail_count})")
                    except Exception as exc:
                        logger.warning(f"[LOGIN] 第 {batch_num} 批异常: {exc}")

                    # 批次间等待（最后一批不等）
                    remaining = expiring[i + batch_size:]
                    if remaining and self._is_polling:
                        interval = config.retry.refresh_batch_interval_minutes * 60
                        logger.info(f"[LOGIN] 等待 {config.retry.refresh_batch_interval_minutes} 分钟后开始下一批...")
                        await asyncio.sleep(interval)

                logger.info("[LOGIN] 本轮刷新完成")

        except asyncio.CancelledError:
            logger.info("[LOGIN] polling stopped")
        except Exception as exc:
            logger.error("[LOGIN] polling error: %s", exc)
        finally:
            self._is_polling = False

    def stop_polling(self) -> None:
        self._is_polling = False
        logger.info("[LOGIN] stopping polling")
