# AGENTS.md

单文件 MCP server：所有代码在 `goploy_mcp.py`（goploy 运维平台 stdio MCP）。无 CI、无 lint 配置；离线单测在 `test_goploy_mcp.py`。

## 运行

- `uv run goploy_mcp.py` — 依赖走 PEP 723 内联声明（仅 `mcp` SDK），uv 自动安装；无 pyproject/requirements
- 运行时需 `~/.config/goploy-mcp/config.json`（host/account/password）；缺配置时 server 能启动，但所有 tool 调用报错。token 与分组缓存在 `~/.cache/goploy-mcp/state.json`
- 测试：`uv run test_goploy_mcp.py`（测试文件自带 PEP 723 头，全 mock 离线，不连真实实例）
- 改动后最低验证：`python3 -m py_compile goploy_mcp.py` + 跑单测；功能验证只能连真实 goploy 实例手动测

## 关键约束

- **登录失败绝不重试**：goploy 连续错 5 次锁号 15 分钟。不要对真实实例反复测登录逻辑；token 失效仅自动重登一次后重试（HTTP code 10086 与 WS 握手被拒 `WSRejectedError` 两条路径都是），这是有意为之，勿再加第二次
- **依赖只用 stdlib + mcp SDK**：HTTP 走 urllib、WebSocket 是手写 RFC6455 客户端（`_WS` 类，https 走 `ssl.wrap_socket`），有意不引入 requests/websockets
- 所有已鉴权请求必带 `G-N-ID` 头（值为 namespace id）
- token 滑动续期：每个响应 Set-Cookie 里的新 token 必须即时 `save_state` 持久化，漏掉会导致 token 过期
- `ws_exec` 哨兵用引号拆分（`__MCP_"DONE_xx"__`）区分回显与真实输出，避免 pty 回显导致提前截断——改这块逻辑时保持该技巧
- 登录响应兼容两种分组 schema：新版 `namespaceId/namespaceName` 与部署版 `id/name/role_id`，解析处勿只留一种
- 注释、docstring、tool 描述与错误信息均为中文（错误消息直接呈现给 LLM/用户）

## 已知坑

- 交互式命令（top 等）会被 `timeout_s` 切断；`timeout_s` 被 clamp 到 1–120 秒
- 多 namespace / 多 server 时未显式传 id 会报错并返回列表提示，这是设计行为，不是 bug
- 提示符探测 `PROMPT_RE`（`\][#$] `）依赖 PS1 含 `]# `/`]$ `：不匹配的主机（zsh 默认提示符等）阶段 1 每次耗满 5s，横幅不被截掉——已知限制，非 bug
- mock 测试 `socket.create_connection` 时必须给 FakeSock 返回真实的 101 响应；返回 MagicMock 会让握手 while 循环死循环挂死测试
- 目标实例（hiloconn 自建 goploy）缺 `/deploy/getPublishProgress` 路由（报 No such method），且 `/deploy/publish` 响应不带 token——所以 `deploy_publish` 空 token 时用 `/deploy/getList` 兜底取 `lastPublishToken`，`deploy_progress` 失败时回退 getList 按令牌匹配 `deployState`，勿删这两个兜底
- 两套状态语义易混：project 的 `deployState` 0=未部署/1=部署中/2=成功/3=失败（`DEPLOY_STATE` 表）；getPublishProgress 的 `state` 0=失败/1=进行中/2=完成
