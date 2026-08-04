"""AstrBot plugin: astrbot_plugin_tavilyquota.

查询 Tavily 联网搜索额度（套餐限额与用量）。

触发指令: /tvquota（别名 /tvq /tavilyquota）
- auto 模式：自动读取 AstrBot 框架配置中的 provider_settings.websearch_tavily_key
- manual 模式：手动在插件配置中填写多个 Key
- 两种模式可独立开关，也可同时开启后聚合去重一次性展示
- 输出一律打码，不展示 Key 明文

Logging contract:
  - Always use `from astrbot.api import logger` (never print for ops signals).
  - Prefix messages with [astrbot_plugin_tavilyquota] so remote greps stay stable.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN = "astrbot_plugin_tavilyquota"
USAGE_URL = "https://api.tavily.com/usage"
CONFIG_KEY_NAME = "websearch_tavily_key"

# Tavily /usage 返回 HTTP 状态码含义（与官方文档一致）
_STATUS_HINTS = {
    401: "Key 无效或已失效",
    429: "请求过于频繁，被限流",
    432: "Key 或套餐额度已用完",
    433: "PayGo 上限已用完",
}


def _mask_key(key: str) -> str:
    """打码 Key：去掉公共前缀 tvly-dev- 后仅保留前 2 位与后 2 位，中间用 *** 代替。"""
    k = (key or "").strip()
    if k.startswith("tvly-dev-"):
        k = k[len("tvly-dev-") :]
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
        return f"{int(round(v)):,}"
    except (ValueError, TypeError, OverflowError):
        return str(v)


def _remaining_pct(usage: float | None, limit: float | None) -> float | None:
    if usage is None or limit is None or limit <= 0:
        return None
    pct = (1 - usage / limit) * 100
    return max(0.0, min(100.0, pct))


def _dedup_keys(auto_keys: list[str], manual_keys: list[str]) -> list[str]:
    """聚合 auto/manual 两个来源的 Key 并按首次出现顺序去重。"""
    merged: list[str] = []
    seen: set[str] = set()
    for k in [*auto_keys, *manual_keys]:
        k = (k or "").strip()
        if not k or k in seen:
            continue
        seen.add(k)
        merged.append(k)
    return merged


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


@register(
    "astrbot_plugin_tavilyquota",
    "konley",
    "Tavily 套餐额度查询：/tvquota 输出各 Key 打码额度，支持自动读取框架配置 Key 与手动填写两种模式",
    version="0.1.2",
    repo="https://github.com/konley/astrbot_plugin_tavilyquota",
)
class TavilyQuota(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config or {}

    # ---------------- Key 收集 ----------------

    def _auto_keys(self) -> list[str]:
        """auto 模式：从框架配置 provider_settings.websearch_tavily_key 读取。"""
        try:
            cfg = self.context.get_config(umo=None)
            provider_settings = cfg.get("provider_settings", {})
        except Exception as e:
            logger.error(f"[{PLUGIN}] 读取框架配置失败: {e}")
            return []
        raw = provider_settings.get(CONFIG_KEY_NAME, [])
        if isinstance(raw, str):
            raw = [raw] if raw.strip() else []
        if not isinstance(raw, list):
            return []
        return [str(k).strip() for k in raw if str(k).strip()]

    def _manual_keys(self) -> list[str]:
        raw = self.config.get("manual_keys", [])
        if isinstance(raw, str):
            raw = raw.replace("\n", ",").split(",")
        if not isinstance(raw, list):
            return []
        return [str(k).strip() for k in raw if str(k).strip()]

    def _collect_keys(self) -> tuple[list[str], list[str]]:
        """返回 (auto_keys, manual_keys)，并按各模式开关过滤。"""
        auto_keys = self._auto_keys() if self.config.get("auto_mode", True) else []
        manual_keys = (
            self._manual_keys() if self.config.get("manual_mode", False) else []
        )
        return auto_keys, manual_keys

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

    async def _fetch_all(self, keys: list[str]) -> list[KeyQuota]:
        timeout = self.config.get("timeout", 15) or 15
        try:
            timeout = max(1, min(60, int(timeout)))
        except (ValueError, TypeError):
            timeout = 15
        concurrency = max(1, min(6, len(keys)))
        sem = asyncio.Semaphore(concurrency)

        async def _bounded(key: str) -> KeyQuota:
            async with sem:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    return await self._fetch_one(client, key)

        return list(await asyncio.gather(*(_bounded(k) for k in keys)))

    # ---------------- 展示 ----------------

    def _build_message(
        self, results: list[KeyQuota], auto_count: int, manual_count: int
    ) -> str:
        _ = (auto_count, manual_count)
        if not results:
            return "📊 Tavily 套餐额度查询\n未获取到任何 Key。"

        ok_results = [r for r in results if r.ok]
        # 所有 Key 套餐一致时合并进标题，行内不再重复
        plans = {r.plan for r in ok_results if r.plan}
        plan_suffix = f" · {next(iter(plans))}" if len(plans) == 1 else ""
        lines = [f"📊 Tavily 套餐额度（{len(results)} 个 Key{plan_suffix}）"]

        for r in results:
            if not r.ok:
                lines.append(f"❌ {r.masked}｜{r.error}")
                continue
            plan_pct = _remaining_pct(r.plan_usage, r.plan_limit)
            pct_str = f"（{plan_pct:.0f}%）" if plan_pct is not None else ""
            usage = f"{_fmt_credits(r.plan_usage)}/{_fmt_credits(r.plan_limit)}"
            if len(plans) == 1:
                lines.append(f"{_light(plan_pct)} {r.masked} {usage}{pct_str}")
            else:
                lines.append(
                    f"{_light(plan_pct)} {r.masked}｜{r.plan} {usage}{pct_str}"
                )
        return "\n".join(lines)

    # ---------------- 指令入口 ----------------

    @filter.command("tvquota", alias={"tvq", "tavilyquota"})
    async def tvquota(self, event: AstrMessageEvent):
        """查询 Tavily 联网搜索额度。"""
        auto_keys, manual_keys = self._collect_keys()
        if not auto_keys and not manual_keys:
            logger.warning(f"[{PLUGIN}] 无可用 Key 来源")
            yield event.plain_result(
                "未获取到任何 Tavily Key。\n"
                "可在插件配置中：\n"
                "· 开启 auto_mode 自动读取框架配置（provider_settings → websearch_tavily_key）\n"
                "· 或开启 manual_mode 并填写 manual_keys"
            )
            return

        keys = _dedup_keys(auto_keys, manual_keys)
        dup_count = len(auto_keys) + len(manual_keys) - len(keys)
        logger.info(
            f"[{PLUGIN}] command=tvquota sender={event.get_sender_name()} "
            f"auto={len(auto_keys)} manual={len(manual_keys)} dedup={dup_count}"
        )

        try:
            results = await self._fetch_all(keys)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[{PLUGIN}] 批量查询失败: {e}")
            yield event.plain_result("查询失败：发生未知错误，请查看日志。")
            return

        yield event.plain_result(
            self._build_message(results, len(auto_keys), len(manual_keys))
        )

    async def terminate(self):
        """Called when plugin is reloaded/unloaded."""
        logger.info(f"[{PLUGIN}] terminate")
