# astrbot_plugin_tavilyquota

查询 Tavily 联网搜索额度（套餐限额与用量）的 AstrBot 插件。触发指令 `/tvquota`（别名 `/tvq` / `/tavilyquota`），一键输出每个 Key 的打码额度与信号灯状态。

## 功能

- 每个 Key 一行展示：信号灯（🟢充足 / 🟡偏紧 / 🔴告急）+ 套餐名 + 已用/上限 credits + 剩余百分比
- 附带 Search 用量、PayGo 用量等明细（接口有返回时显示）
- Key 一律打码（保留头尾，中间 `****`），不会输出明文

## Key 来源（两种模式互不干扰）

| 模式 | 说明 | 默认 |
|------|------|------|
| `auto_mode` | 自动读取 AstrBot 框架配置 `provider_settings → websearch_tavily_key`（即 WebUI「联网搜索」里配置的 Key），无需手动填写 | 开 |
| `manual_mode` | 手动在插件配置 `manual_keys` 中填写想额外查询的 Key | 关 |

两种模式可独立开关，也可同时开启：查询时会聚合两个来源的 Key 并按首次出现顺序去重，一次性展示。

## Install

In AstrBot WebUI → Plugin market → install from `https://github.com/konley/astrbot_plugin_tavilyquota`.

## Commands

- `/tvquota`（别名 `/tvq` `/tavilyquota`）— 查询全部可用 Key 的额度

## Config

| 配置项 | 类型 | 说明 |
|--------|------|------|
| `auto_mode` | bool | 自动模式开关，默认开启 |
| `manual_mode` | bool | 手动模式开关，默认关闭 |
| `manual_keys` | list | 手动填写的 Key 列表，每个一行 |
| `timeout` | int | 请求 Tavily `/usage` 接口超时（秒），默认 15 |

## 额度数据口径

- 接口：`GET https://api.tavily.com/usage`（Tavily 公开接口，只读，不消耗 Credits）
- 注意：`/usage` 返回的数据可能存在刷新延迟（分钟级），以 Tavily Dashboard 为最终依据
- 单个 Key 对应一个 Tavily 账户，`account.plan_usage/plan_limit` 即该账户套餐用量与上限

## Logs

Ops-visible logs use `from astrbot.api import logger` with a stable `[astrbot_plugin_tavilyquota]` prefix.

## Dev

```bash
ruff format .
pytest -q
```

Reload after code changes (WebUI -> Plugin management -> reload).
