# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2.0,<2"]
# ///
"""goploy-mcp: goploy 运维平台 MCP server (stdio)

封装 goploy 的用户/分组/服务器查询与远程命令执行：
  - user_info  当前登录用户信息
  - namespaces 分组(namespace)列表
  - servers    分组下的服务器列表
  - exec       在指定服务器上一次性执行 shell 命令

认证：账号密码自动登录换取 JWT cookie；每次响应的 Set-Cookie 新 token
即时持久化（goploy 为滑动续期，活跃状态下永不过期）。
"""

import base64
import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP

CONFIG_PATH = Path("~/.config/goploy-mcp/config.json").expanduser()
STATE_DIR = Path("~/.cache/goploy-mcp").expanduser()
STATE_PATH = STATE_DIR / "state.json"

LOGIN_EXPIRED = 10086
ACCOUNT_DISABLED = 10000

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[=>]")
PROMPT_RE = re.compile(r"\][#$] ")


class GoployError(Exception):
    """业务错误，消息直接呈现给 LLM/用户"""


class WSRejectedError(GoployError):
    """WebSocket 握手被服务端拒绝（非 101），常见原因为 token 失效"""


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise GoployError(f"缺少配置文件 {CONFIG_PATH}，需要字段 host/account/password")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    for key in ("host", "account", "password"):
        if not cfg.get(key):
            raise GoployError(f"配置文件缺少字段: {key}")
    return cfg


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"token": "", "namespaces": []}


def save_state(**kwargs) -> dict:
    st = load_state()
    st.update(kwargs)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE_PATH)
    return st


def _extract_token(headers) -> str:
    """从 Set-Cookie 头里解析 goploy_token（滑动续期的新 token）"""
    for value in headers.get_all("Set-Cookie") or []:
        m = re.search(r"(?:^|;\s*)goploy_token=([^;]+)", value)
        if m:
            return m.group(1).strip()
    return ""


def _http(path: str, method: str = "GET", body: dict | None = None,
          token: str = "", namespace_id: int = 0) -> tuple[int, str, object, str]:
    """返回 (code, message, data, set_cookie_token)"""
    cfg = load_config()
    url = cfg["host"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("G-N-ID", str(namespace_id))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Cookie", f"goploy_token={token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            new_token = _extract_token(resp.headers)
    except urllib.error.HTTPError as e:
        raise GoployError(f"HTTP {e.code}: {path} {e.reason}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GoployError(f"请求 goploy 失败: {e}（检查 config.host 与网络）")
    except json.JSONDecodeError:
        raise GoployError(f"响应不是 JSON: {path}")
    return int(payload.get("code", -1)), payload.get("message", ""), payload.get("data"), new_token


def login() -> dict:
    """账号密码登录；成功返回最新 state（含 token 与分组列表）。失败不重试（防锁号）。"""
    cfg = load_config()
    code, msg, data, cookie_token = _http(
        "/user/login", "POST",
        {"account": cfg["account"], "password": cfg["password"]},
    )
    if code != 0:
        raise GoployError(
            f"goploy 登录失败: {msg}（连续失败会锁号 15 分钟，请检查 config.json 后再试）"
        )
    token = ""
    namespaces = []
    if isinstance(data, dict):
        token = data.get("token", "")
        # 兼容两种 schema：新版 {"namespaceId","namespaceName"} / 部署版 {"id","name","role_id"}
        raw = data.get("namespaceList") or []
        for n in raw:
            if not isinstance(n, dict):
                continue
            ns_id = n.get("namespaceId", n.get("id"))
            if ns_id is None:
                continue
            item = {"namespaceId": int(ns_id),
                    "namespaceName": n.get("namespaceName", n.get("name", ""))}
            if n.get("role_id") is not None:
                item["roleId"] = n["role_id"]
            namespaces.append(item)
    token = cookie_token or token
    if not token:
        raise GoployError("登录成功但未返回 token")
    return save_state(token=token, namespaces=namespaces)


def api_call(path: str, method: str = "GET", body: dict | None = None,
             namespace_id: int | None = None) -> object:
    """带自动登录/续期的 API 调用。LoginExpired 自动重登一次。"""
    st = load_state()
    if not st.get("token"):
        st = login()

    def once(token: str, ns: int):
        code, msg, data, new_token = _http(path, method, body, token, ns)
        if new_token:
            save_state(token=new_token)
        return code, msg, data

    ns = namespace_id if namespace_id is not None else 0
    code, msg, data = once(st["token"], ns)
    if code == LOGIN_EXPIRED:
        st = login()
        code, msg, data = once(st["token"], ns)
    if code == ACCOUNT_DISABLED:
        raise GoployError("账号已被禁用，请联系管理员")
    if code != 0:
        raise GoployError(f"goploy 接口错误 code={code}: {msg} ({path})")
    return data


def get_namespaces() -> list[dict]:
    """分组列表：由登录响应提供。"""
    st = load_state()
    if not st.get("token") or not st.get("namespaces"):
        st = login()
    if st.get("namespaces"):
        return st["namespaces"]
    raise GoployError("登录响应不含分组列表，当前 goploy 版本不受支持")


def resolve_namespace(namespace_id: int | None) -> tuple[int, list[dict]]:
    """解析目标分组：显式指定则校验；未指定时唯一分组自动选、多分组要求明确。"""
    nss = get_namespaces()
    if namespace_id is not None:
        known = {ns["namespaceId"] for ns in nss}
        if nss and namespace_id not in known:
            raise GoployError(
                f"namespace_id={namespace_id} 不在可用分组中: "
                + json.dumps(nss, ensure_ascii=False))
        return namespace_id, nss
    if len(nss) == 1:
        return nss[0]["namespaceId"], nss
    raise GoployError("存在多个分组，请先调用 namespaces 查看并指定 namespace_id: "
                      + json.dumps(nss, ensure_ascii=False))


def resolve_server(namespace_id: int | None, server_id: int | None) -> tuple[int, int]:
    """解析分组与服务器：server_id 未指定时，分组内唯一服务器自动选。"""
    ns_id, _ = resolve_namespace(namespace_id)
    if server_id is not None:
        return ns_id, server_id
    data = api_call("/server/getOption", namespace_id=ns_id)
    servers = (data or {}).get("list") or []
    if len(servers) == 1:
        return ns_id, int(servers[0]["id"])
    brief = [{"id": s.get("id"), "name": s.get("name"), "ip": s.get("ip")} for s in servers]
    raise GoployError("该分组存在多台服务器，请指定 server_id: "
                      + json.dumps(brief, ensure_ascii=False))


# ---------------- WebSocket 一次性终端执行 ----------------

class _WS:
    """极简 RFC6455 客户端：握手 + 收发文本帧（仅服务 goploy /ws/xterm）"""

    def __init__(self, sock: socket.socket):
        self.s = sock
        self.buf = b""
        self.handshake_head = b""

    @classmethod
    def connect(cls, host: str, path: str, cookie: str) -> "_WS":
        parts = urlsplit(host)
        hostname, port = parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
        sock = socket.create_connection((hostname, port), timeout=15)
        try:
            if parts.scheme == "https":
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=hostname)
            key = base64.b64encode(os.urandom(16)).decode()
            req = (f"GET {path} HTTP/1.1\r\nHost: {hostname}:{port}\r\n"
                   f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                   f"Origin: {parts.scheme}://{hostname}:{port}\r\n"
                   f"Cookie: {cookie}\r\nUser-Agent: goploy-mcp\r\n\r\n")
            sock.sendall(req.encode())
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = sock.recv(4096)
                if not chunk:
                    raise GoployError("WebSocket 握手失败: 连接被关闭")
                head += chunk
            status = head.split(b"\r\n", 1)[0].decode(errors="replace")
            if " 101 " not in status + " ":
                raise WSRejectedError(f"WebSocket 握手被拒绝: {status}（token 可能已失效）")
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise
        ws = cls(sock)
        ws.buf = head.split(b"\r\n\r\n", 1)[1]
        ws.handshake_head = head  # 供上层解析 Set-Cookie
        return ws

    def _send_frame(self, first_byte: int, payload: bytes):
        """发送客户端掩码帧（text/pong/close 共用）"""
        header = bytearray([first_byte])
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += n.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += n.to_bytes(8, "big")
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.s.sendall(bytes(header) + mask + masked)

    def send_text(self, payload: bytes):
        self._send_frame(0x81, payload)

    def _next_frame(self):
        """解析 buf 中的下一帧；数据不足返回 None"""
        if len(self.buf) < 2:
            return None
        b1, b2 = self.buf[0], self.buf[1]
        ln = b2 & 0x7F
        off = 2
        if ln == 126:
            if len(self.buf) < 4:
                return None
            ln = int.from_bytes(self.buf[2:4], "big")
            off = 4
        elif ln == 127:
            if len(self.buf) < 10:
                return None
            ln = int.from_bytes(self.buf[2:10], "big")
            off = 10
        if b2 & 0x80:
            off += 4  # 服务端帧按规范不带掩码，防御性跳过
        if len(self.buf) < off + ln:
            return None
        opcode = b1 & 0x0F
        payload = bytes(self.buf[off:off + ln])
        self.buf = self.buf[off + ln:]
        return opcode, payload

    def _pong(self, payload: bytes):
        self._send_frame(0x8A, payload)

    def recv_text(self, timeout: float) -> str:
        """读一段时间内的所有文本帧，拼接返回"""
        deadline = time.time() + timeout
        out = bytearray()
        while time.time() < deadline:
            frame = self._next_frame()
            if frame is None:
                self.s.settimeout(max(0.05, deadline - time.time()))
                try:
                    chunk = self.s.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                self.buf += chunk
                continue
            opcode, payload = frame
            if opcode in (1, 2, 0):
                out += payload
            elif opcode == 9:  # ping -> pong
                try:
                    self._pong(payload)
                except OSError:
                    break
            elif opcode == 8:  # close
                break
        return out.decode("utf-8", "replace")

    def close(self):
        try:
            self._send_frame(0x88, b"")
        except OSError:
            pass
        try:
            self.s.close()
        except OSError:
            pass


def ws_exec(command: str, server_id: int, namespace_id: int, timeout_s: int) -> str:
    """一次性连接 /ws/xterm：等提示符 -> 发命令 -> 哨兵判完成 -> 清洗输出"""
    st = load_state()
    if not st.get("token"):
        st = login()
    cfg = load_config()
    ws_path = f"/ws/xterm?G-N-ID={namespace_id}&serverId={server_id}&rows=50&cols=200"
    try:
        ws = _WS.connect(cfg["host"], ws_path, f"goploy_token={st['token']}")
    except WSRejectedError:
        # 与 api_call 一致：token 失效自动重登一次后重试（登录失败不会重试）
        st = login()
        ws = _WS.connect(cfg["host"], ws_path, f"goploy_token={st['token']}")
    try:
        new_token = _extract_token_from_raw(ws.handshake_head.decode("latin-1", "replace"))
        if new_token:
            save_state(token=new_token)
        # 阶段1：等 shell 提示符（登录横幅之后），最多 5s
        banner = ""
        deadline = time.time() + 5
        while time.time() < deadline:
            banner += ws.recv_text(0.4)
            if PROMPT_RE.search(ANSI_RE.sub("", banner)):
                break
        # 阶段2：发命令 + 哨兵，读到哨兵或超时
        # 注意：回显行里哨兵带引号（__MCP_"DONE_xx"__），只有真实输出行才是完整哨兵，
        # 避免 find(sentinel) 命中回显导致提前截断
        mark = os.urandom(3).hex()
        sentinel = f"__MCP_DONE_{mark}__"
        ws.send_text(f'{command}; echo __MCP_"DONE_{mark}"__\n'.encode())
        raw = ""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            raw += ws.recv_text(0.5)
            if sentinel in ANSI_RE.sub("", raw):
                break
    finally:
        ws.close()

    clean = ANSI_RE.sub("", banner + raw).replace("\r", "")
    # 截掉哨兵及其后内容（新提示符）
    pos = clean.find(sentinel)
    if pos >= 0:
        clean = clean[:pos]
    # 截掉首个提示符之前的内容（登录横幅 + 提示符 + 回显命令头）
    m = PROMPT_RE.search(clean)
    if m:
        clean = clean[m.end():]
    # 去掉回显的哨兵命令行与空行噪音
    lines = [ln for ln in clean.split("\n") if "echo __MCP_" not in ln]
    return "\n".join(lines).strip("\n")


def _extract_token_from_raw(head: str) -> str:
    m = re.search(r"set-cookie:\s*goploy_token=([^;\r\n]+)", head, re.IGNORECASE)
    return m.group(1).strip() if m else ""


# ---------------- MCP tools ----------------

mcp = FastMCP("goploy-mcp")


@mcp.tool()
def user_info() -> str:
    """获取当前 goploy 登录用户信息（用户名/角色/权限）。"""
    nss = get_namespaces()
    ns_id = nss[0]["namespaceId"] if nss else 0
    data = api_call("/user/info", namespace_id=ns_id)
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def namespaces() -> str:
    """列出当前账号可用的所有分组(namespace)。"""
    return json.dumps(get_namespaces(), ensure_ascii=False, indent=2)


@mcp.tool()
def servers(namespace_id: int | None = None) -> str:
    """列出分组下的服务器。namespace_id 不传时：唯一分组自动选择，多分组会返回分组列表提示选择。"""
    ns_id, _ = resolve_namespace(namespace_id)
    data = api_call("/server/getOption", namespace_id=ns_id)
    servers_ = (data or {}).get("list") or []
    brief = [{"id": s.get("id"), "name": s.get("name"), "ip": s.get("ip"),
              "owner": s.get("owner"), "description": s.get("description")} for s in servers_]
    return json.dumps(brief, ensure_ascii=False, indent=2)


@mcp.tool()
def exec(command: str, server_id: int | None = None,
         namespace_id: int | None = None, timeout_s: int = 8) -> str:
    """在 goploy 管理的服务器上一次性执行 shell 命令，返回终端输出（已清理 ANSI）。
    适合 ps/df/cat/脚本等一次性命令；交互式命令(如 top)会在 timeout_s 秒后被切断。
    server_id/namespace_id 不传时自动解析（唯一则自动选，多个会返回列表提示选择）。"""
    timeout_s = max(1, min(int(timeout_s), 120))
    ns_id, sid = resolve_server(namespace_id, server_id)
    return ws_exec(command, sid, ns_id, timeout_s) or "(无输出)"


if __name__ == "__main__":
    mcp.run()
