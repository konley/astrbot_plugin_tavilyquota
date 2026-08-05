"""AstrBot plugin: astrbot_plugin_tavilyquota.

Tavily 联网搜索额度查询与 Key 接力管理。

- /tvquota：只查询「当前活跃 Key」（框架配置 provider_settings.websearch_tavily_key
  中的唯一 Key）的套餐额度，展示备用 Key 池数量与上次切换时间；
  开启 auto_rotate 且活跃 Key 额度耗尽/失效时自动接力到池中下一个 Key。
- /tvrotate：手动切换到池中下一个 Key（无条件），并自动更新框架配置。

设计原则（防风控）：
- 框架配置中永远只放 1 个活跃 Key，所有消费方（内置联网搜索、isittrue 等）
  只打这一个账号；
- 备用 Key 池存于插件 KV，不在框架配置中，平时不产生任何请求；
- 额度检查一天最多一次（rotate_check_interval_hours），且只查活跃 Key；
- 只有「额度已耗尽 / Key 已失效」才切换，绝不提前预切。

Logging contract:
  - Always use `from astrbot.api import logger` (never print for ops signals).
  - Prefix messages with [astrbot_plugin_tavilyquota].
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN = "astrbot_plugin_tavilyquota"
USAGE_URL = "https://api.tavily.com/usage"
CONFIG_KEY_NAME = "websearch_tavily_key"
_POOL_KV = "rotation_pool"
_ROTATE_TS_KV = "last_rotate_ts"

# Tavily /usage 返回 HTTP 状态码含义（与官方文档一致）
_STATUS_HINTS = {
    401: "Key 无效或账户已停用",
    429: "请求过于频繁，被限流",
    432: "Key 或套餐额度已用完",
    433: "PayGo 上限已用完",
}

# 这些状态码视为「该 Key 不可继续使用」，需要触发接力切换
_EXHAUSTED_STATUS_CODES = frozenset({401, 403, 432})


def _mask_key(key: str) -> str:
    """打码 Key：去掉公共前缀 tvly-dev- 后仅保留前 2 位与后 2 位。"""
    k = (key or "").strip()
    k = k.removeprefix("tvly-dev-")
    if len(k) <= 4:
        return "****"
    return f"{k[:2]}***{k[-2:]}"


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _light(remaining_pct: float | None) -> str:
    """根据剩余百分比返回信号灯 emoji：🟢充足 / 🟡偏紧 / 🔴告急。"""
    if remaining_pct is None:
        return "⚪"
    if remaining_pct <= 20:
        return "🔴"
    if remaining_pct < 60:
        return "🟡"
    return "🟢"


def _fmt_credits(v: float | None) -> str:
    if v is None:
        return "?"
    try:
        return f"{round(v):,}"
    except (ValueError, TypeError, OverflowError):
        return str(v)


def _remaining_pct(usage: float | None, limit: float | None) -> float | None:
    if usage is None or limit is None or limit <= 0:
        return None
    pct = (1 - usage / limit) * 100
    return max(0.0, min(100.0, pct))


def _fmt_relative(ts_iso: str | None) -> str:
    """把 ISO 时间戳转成 'x 分钟/小时/天前' 文案。"""
    if not ts_iso:
        return "从未"
    try:
        dt = datetime.fromisoformat(ts_iso)
        now = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = (now - dt).total_seconds()
    except (ValueError, TypeError):
        return "未知"
    if secs < 0:
        return "刚刚"
    if secs < 60:
        return "刚刚"
    if secs < 3600:
        return f"{int(secs // 60)} 分钟前"
    if secs < 86400:
        return f"{secs / 3600:.1f} 小时前"
    return f"{secs / 86400:.1f} 天前"


@dataclass
class KeyQuota:
    masked: str
    ok: bool
    plan: str | None = None
    plan_usage: float | None = None
    plan_limit: float | None = None
    paygo_usage: float | None = None
    paygo_limit: float | None = None
    key_usage: float | None = None
    key_limit: float | None = None
    search_usage: float | None = None
    error: str | None = None

    @property
    def exhausted(self) -> bool:
        """额度耗尽 / Key 失效 / 账户停用 → 需要切换。"""
        if not self.ok:
            return bool(
                self.error
                and any(
                    f"HTTP {code}" in self.error for code in _EXHAUSTED_STATUS_CODES
                )
            )
        if (
            self.plan_limit is not None
            and self.plan_usage is not None
            and self.plan_usage >= self.plan_limit
        ):
            return True
        return bool(
            self.key_limit is not None
            and self.key_usage is not None
            and self.key_usage >= self.key_limit
        )


@register(
    "astrbot_plugin_tavilyquota",
    "konley",
    "Tavily 额度查询与 Key 接力：/tvquota 查询当前活跃 Key 额度，/tvrotate 手动切换备用 Key，支持额度耗尽自动接力",
    version="0.2.0",
    repo="https://github.com/konley/astrbot_plugin_tavilyquota",
)
class TavilyQuota(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.scheduler = None
        self.auto_rotate: bool = bool(self.config.get("auto_rotate", False))
        try:
            self.rotate_interval: int = max(
                1, int(self.config.get("rotate_check_interval_hours", 24) or 24)
            )
        except (ValueError, TypeError):
            self.rotate_interval = 24
        if self.auto_rotate:
            self._setup_scheduler()

    # ---------------- 框架配置读写 ----------------

    def _active_key(self) -> str:
        """当前活跃 Key = 框架配置 websearch_tavily_key 列表的第一个（唯一）。"""
        try:
            cfg = self.context.get_config(umo=None)
            provider_settings = cfg.get("provider_settings", {}) or {}
            raw = provider_settings.get(CONFIG_KEY_NAME, [])
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{PLUGIN}] 读取框架配置失败: {e}")
            return ""
        if isinstance(raw, str):
            raw = [raw] if raw.strip() else []
        if not isinstance(raw, list):
            return ""
        keys = [str(k).strip() for k in raw if str(k).strip()]
        return keys[0] if keys else ""

    def _write_active_key(self, key: str) -> bool:
        """把新活跃 Key 写入框架配置（只保留它一个），立即生效无需 reload。"""
        try:
            cfg = self.context.get_config(umo=None)
            provider_settings = cfg.get("provider_settings", {}) or {}
            provider_settings[CONFIG_KEY_NAME] = [key]
            save = getattr(cfg, "save_config", None)
            if save is not None:
                save()
            logger.info(f"[{PLUGIN}] 框架配置已更新：活跃 Key -> {_mask_key(key)}")
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{PLUGIN}] 更新框架配置失败: {e}")
            return False

    # ---------------- 备用池（KV 持久化） ----------------

    def _pool_keys(self) -> list[str]:
        """备用池：优先 KV，首次从插件配置 rotation_pool 灌入。"""
        pool = self._sync_pool_kv()
        if pool:
            return pool
        raw = self.config.get("rotation_pool", [])
        if isinstance(raw, str):
            raw = raw.replace("\n", ",").split(",")
        if not isinstance(raw, list):
            return []
        pool = [str(k).strip() for k in raw if str(k).strip()]
        if pool:
            try:
                loop = asyncio.get_event_loop()
                if not loop.is_running():
                    loop.run_until_complete(self.put_kv_data(_POOL_KV, pool))
            except Exception:  # noqa: BLE001, S110
                pass  # KV 不可用时纯内存兜底
        return pool

    def _sync_pool_kv(self):
        """同步读取 KV 池（同步上下文用 run_until_complete 兜底）。"""
        try:
            return self._get_kv(_POOL_KV)
        except Exception:  # noqa: BLE001
            return None

    def _get_kv(self, key: str):
        import asyncio as _asyncio

        try:
            loop = _asyncio.get_event_loop()
            if loop.is_running():
                return None
            return loop.run_until_complete(self.get_kv_data(key, None))
        except Exception:  # noqa: BLE001
            return None

    async def _async_get_kv(self, key: str):
        try:
            return await self.get_kv_data(key, None)
        except Exception:  # noqa: BLE001
            return None

    async def _async_put_kv(self, key: str, value) -> None:
        try:
            await self.put_kv_data(key, value)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{PLUGIN}] KV 写入失败 {key}: {e}")

    async def _last_rotate_ts(self) -> str | None:
        return await self._async_get_kv(_ROTATE_TS_KV)

    # ---------------- 额度查询 ----------------

    async def _fetch_one(self, client: httpx.AsyncClient, key: str) -> KeyQuota:
        masked = _mask_key(key)
        try:
            resp = await client.get(
                USAGE_URL, headers={"Authorization": f"Bearer {key}"}
            )
        except httpx.RequestError as e:
            logger.error(f"[{PLUGIN}] 请求异常 key={masked}: {e}")
            return KeyQuota(masked=masked, ok=False, error="网络请求异常")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{PLUGIN}] 未知错误 key={masked}: {e}")
            return KeyQuota(masked=masked, ok=False, error="未知错误")

        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                return KeyQuota(masked=masked, ok=False, error="响应解析失败")
            account = data.get("account", {}) or {}
            key_data = data.get("key", {}) or {}
            return KeyQuota(
                masked=masked,
                ok=True,
                plan=str(account.get("current_plan") or "未知"),
                plan_usage=_to_float(account.get("plan_usage")),
                plan_limit=_to_float(account.get("plan_limit")),
                paygo_usage=_to_float(account.get("paygo_usage")),
                paygo_limit=_to_float(account.get("paygo_limit")),
                key_usage=_to_float(key_data.get("usage")),
                key_limit=_to_float(key_data.get("limit")),
                search_usage=_to_float(key_data.get("search_usage")),
            )

        hint = _STATUS_HINTS.get(resp.status_code, "接口异常")
        body = (resp.text or "")[:200].replace("\n", " ")
        logger.error(f"[{PLUGIN}] HTTP {resp.status_code} key={masked}: {body}")
        return KeyQuota(
            masked=masked, ok=False, error=f"HTTP {resp.status_code} · {hint}"
        )

    async def _query_active(self, key: str) -> KeyQuota:
        timeout = self.config.get("timeout", 15) or 15
        try:
            timeout = max(1, min(60, int(timeout)))
        except (ValueError, TypeError):
            timeout = 15
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await self._fetch_one(client, key)

    # ---------------- 切换 ----------------

    async def _rotate_to_next(self, reason: str) -> tuple[bool, str]:
        """切换到备用池下一个 Key：更新 KV 池顺序 + 写框架配置。

        旧的活跃 Key 放回池尾（循环，等待额度按月刷新后复用）。
        """
        active = self._active_key()
        pool = self._pool_keys()
        if not pool:
            return False, "备用池为空，没有可切换的 Key"
        next_key = pool[0]
        new_pool = [k for k in pool[1:] if k != next_key]
        if active and active != next_key or active == next_key:
            new_pool.append(active)

        ok = self._write_active_key(next_key)
        if not ok:
            return False, "写入框架配置失败，切换未生效"
        await self._async_put_kv(_POOL_KV, new_pool)
        await self._async_put_kv(_ROTATE_TS_KV, datetime.now(timezone.utc).isoformat())
        logger.info(
            f"[{PLUGIN}] 已切换 {_mask_key(active) if active else '无'} -> "
            f"{_mask_key(next_key)} | reason={reason} | 池剩余 {len(new_pool)}"
        )
        return True, f"{_mask_key(active) if active else '无'} → {_mask_key(next_key)}"

    # ---------------- 展示 ----------------

    async def _build_quota_message(
        self, quota: KeyQuota, pool_count: int, last_rotate: str | None
    ) -> str:
        plan_pct = _remaining_pct(quota.plan_usage, quota.plan_limit)
        pct_str = f"（{plan_pct:.0f}%）" if plan_pct is not None else ""
        usage = f"{_fmt_credits(quota.plan_usage)}/{_fmt_credits(quota.plan_limit)}"
        lines = ["📊 Tavily 套餐额度（当前活跃 Key）"]
        if quota.ok:
            lines.append(f"{_light(plan_pct)} {quota.masked} {usage}{pct_str}")
        else:
            lines.append(f"❌ {quota.masked}｜{quota.error}")
        lines.append(
            f"备用池：{pool_count} 个 · 上次切换：{_fmt_relative(last_rotate)}"
        )
        return "\n".join(lines)

    # ---------------- 指令入口 ----------------

    @filter.command("tvquota", alias={"tvq", "tavilyquota"})
    async def tvquota(self, event: AstrMessageEvent):
        """查询当前活跃 Tavily Key 的套餐额度与备用池状态。"""
        active = self._active_key()
        if not active:
            yield event.plain_result(
                "框架配置中未找到活跃 Tavily Key。\n"
                "请先在 AstrBot 设置 → 联网搜索 → websearch_tavily_key 填入当前使用的 Key"
            )
            return

        logger.info(
            f"[{PLUGIN}] command=tvquota sender={event.get_sender_name()} "
            f"active={_mask_key(active)}"
        )
        quota = await self._query_active(active)
        pool = self._pool_keys()
        last_rotate = await self._last_rotate_ts()

        if self.auto_rotate and quota.exhausted:
            ok, desc = await self._rotate_to_next("活跃 Key 额度耗尽/失效")
            if ok:
                new_quota = await self._query_active(self._active_key())
                pool = self._pool_keys()
                last_rotate = await self._last_rotate_ts()
                lines = [
                    f"🔄 已自动接力（{desc}）",
                    await self._build_quota_message(new_quota, len(pool), last_rotate),
                ]
                yield event.plain_result("\n".join(lines))
                return
            yield event.plain_result(f"⚠️ 活跃 Key 已耗尽但切换失败：{desc}")
            return

        yield event.plain_result(
            await self._build_quota_message(quota, len(pool), last_rotate)
        )

    @filter.command("tvrotate")
    async def tvrotate(self, event: AstrMessageEvent):
        """手动切换到备用池下一个 Key（无条件）。"""
        pool = self._pool_keys()
        if not pool:
            yield event.plain_result(
                "备用池为空。请在插件配置 rotation_pool 中填写备用 Key。"
            )
            return
        logger.info(f"[{PLUGIN}] command=tvrotate sender={event.get_sender_name()}")
        ok, desc = await self._rotate_to_next("手动切换")
        if not ok:
            yield event.plain_result(f"切换失败：{desc}")
            return
        new_quota = await self._query_active(self._active_key())
        pool = self._pool_keys()
        last_rotate = await self._last_rotate_ts()
        lines = [
            f"🔄 手动切换完成（{desc}）",
            await self._build_quota_message(new_quota, len(pool), last_rotate),
        ]
        yield event.plain_result("\n".join(lines))

    # ---------------- 定时检查 ----------------

    def _setup_scheduler(self):
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.interval import IntervalTrigger
        except ImportError:
            logger.error(f"[{PLUGIN}] 未安装 apscheduler，定时检查不可用")
            return
        self.scheduler = AsyncIOScheduler()
        self.scheduler.add_job(
            self._scheduled_check,
            IntervalTrigger(hours=self.rotate_interval),
            id="tvquota_rotate_check",
        )
        self.scheduler.start()
        logger.info(
            f"[{PLUGIN}] 定时检查已启用，每 {self.rotate_interval} 小时检查一次活跃 Key"
        )

    async def _scheduled_check(self):
        """定时检查：只查活跃 Key，额度耗尽/失效才切换，否则不动。"""
        active = self._active_key()
        if not active:
            logger.warning(f"[{PLUGIN}] 定时检查：无活跃 Key")
            return
        quota = await self._query_active(active)
        if not quota.exhausted:
            logger.info(
                f"[{PLUGIN}] 定时检查：活跃 Key {_mask_key(active)} 额度正常，无需切换"
            )
            return
        ok, desc = await self._rotate_to_next("定时检查发现额度耗尽/失效")
        if ok:
            logger.info(f"[{PLUGIN}] 定时检查自动接力完成：{desc}")

    async def terminate(self):
        """插件卸载/重载时停止调度器。"""
        if self.scheduler is not None:
            try:
                self.scheduler.shutdown(wait=False)
            except Exception:  # noqa: BLE001, S110
                pass  # 调度器已停止/未启动，忽略
            self.scheduler = None
        logger.info(f"[{PLUGIN}] terminate")
