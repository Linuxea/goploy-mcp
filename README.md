# goploy-mcp

goploy 运维平台的 MCP server（stdio），提供用户/分组/服务器查询与远程命令执行。

## 配置

`~/.config/goploy-mcp/config.json`（600 权限，含明文密码，勿入库）：

```json
{
  "host": "http://goploy.example.com:9001",
  "account": "xxx",
  "password": "xxx"
}
```

## 运行

```bash
uv run ~/project/goploy-mcp/goploy_mcp.py
```

依赖通过 PEP 723 内联声明（仅 `mcp` SDK），首次运行由 uv 自动安装。

## 测试

```bash
uv run test_goploy_mcp.py
```

离线单元测试（全部 mock，不连真实 goploy 实例，不会触发锁号）。

## Tools

| tool | 参数 | 说明 |
|---|---|---|
| `user_info` | - | 当前登录用户信息 |
| `namespaces` | - | 分组列表（登录响应自带，冷启动零配置） |
| `servers` | `namespace_id?` | 分组下服务器列表；分组唯一自动选，多分组提示选择 |
| `exec` | `command`、`server_id?`、`namespace_id?`、`timeout_s?`(默认8s) | 一次性 WS 连接执行命令，输出已清理 ANSI 与登录横幅；分组/服务器唯一时自动选 |

## 认证机制

- 首次调用自动 `POST /user/login`，一次拿到 token + 分组列表
- goploy 对每个已鉴权请求都会重签 token（Set-Cookie 滑动续期），本服务即时把新 token
  持久化到 `~/.cache/goploy-mcp/state.json`，活跃状态下永不过期
- token 失效（10086）自动重登一次；`exec` 的 WS 握手被拒（token 失效）同样自动重登一次后重试；
  登录失败不重试（goploy 连续错 5 次锁号 15 分钟）

## 原理备注

- HTTP 走 `urllib`；`G-N-ID` 请求头带分组 ID（goploy 所有已鉴权接口必带）
- `exec` 走 `/ws/xterm` WebSocket（goploy 后端 SSH 到目标机开 pty），每次执行
  建立新连接、跑完即关（无会话残留）；用引号拆分哨兵 `echo __MCP_"DONE_xx"__`
  判断命令结束，规避 pty 回显造成的提前截断
