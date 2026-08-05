# astrbot_plugin_tavilyquota

Tavily 联网搜索额度查询与 Key 接力管理插件。触发指令 `/tvquota`（别名 `/tvq` / `/tavilyquota`）与 `/tvrotate`。

## 功能

- `/tvquota`：查询**当前活跃 Key**（框架配置 `provider_settings → websearch_tavily_key` 中的唯一 Key）的套餐额度、信号灯状态，并展示备用池数量与上次切换时间
- `/tvrotate`：手动切换到备用池下一个 Key（无条件）
- 可选 `auto_rotate`：活跃 Key 额度耗尽/失效时自动接力（默认关闭）

## 设计原则（防风控）

Tavily 对「同一 IP 多账号高频轮询」风控严格（曾导致整批账号被停用）。本插件因此采用：

1. **框架配置永远只放 1 个活跃 Key**——所有消费方（框架内置联网搜索、isittrue 等）只打这一个账号，不会多 Key 轮询
2. **备用池存插件 KV**，平时零请求，不暴露给框架
3. **额度检查低频**：定时检查默认 24 小时一次（`rotate_check_interval_hours`），且只查活跃 Key 一个账号
4. **用完才切**：只有「额度耗尽（plan_usage ≥ plan_limit）/ Key 失效 / 账户停用」才切换，绝不提前预切
5. 被换下的 Key 放回池尾循环——额度每月自动刷新后自然复用，无需人工管理

## 命令

- `/tvquota`（别名 `/tvq` `/tavilyquota`）— 查询活跃 Key 额度 + 池状态 + 上次切换时间
- `/tvrotate` — 手动切换到池中下一个 Key

## 配置

| 配置项 | 类型 | 说明 | 默认 |
|--------|------|------|------|
| `auto_mode` | bool | 自动读取框架配置的活跃 Key | true |
| `auto_rotate` | bool | 额度耗尽自动接力开关 | false |
| `rotate_check_interval_hours` | int | 自动检查间隔（小时） | 24 |
| `rotation_pool` | list | 备用 Key 池（每个一行） | [] |
| `timeout` | int | 请求超时（秒） | 15 |

## 使用流程

1. 框架配置 `websearch_tavily_key` 只填当前在用的 1 个 Key
2. 其余 Key 全部填进插件 `rotation_pool`
3. 平时 `/tvquota` 看额度；额度用完时 `/tvrotate` 切下一个（或开启 `auto_rotate` 自动切）
4. 框架配置更新即时生效，无需重启

## 额度数据口径

- 接口：`GET https://api.tavily.com/usage`（公开只读接口，不消耗 Credits）
- `/usage` 数据可能有分钟级刷新延迟，以 Tavily Dashboard 为最终依据

## Logs

Ops-visible logs use `from astrbot.api import logger` with a stable `[astrbot_plugin_tavilyquota]` prefix.

## Dev

```bash
ruff format .
pytest -q
```

Reload after code changes (WebUI -> Plugin management -> reload).
