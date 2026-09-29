# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2.0,<2"]
# ///
"""goploy_mcp 离线单元测试（全部 mock，不连真实 goploy 实例，不会触发锁号）。

运行： uv run test_goploy_mcp.py
"""

import contextlib
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import goploy_mcp as g


# ---------------- 测试替身 ----------------

class FakeHeaders:
    def __init__(self, cookies):
        self.cookies = cookies

    def get_all(self, name):
        return self.cookies if name == "Set-Cookie" else []


class FakeSock:
    """替身 socket：预置接收数据，记录发送内容"""

    def __init__(self, rx=b"", empty_returns=None):
        self.rx = bytearray(rx)
        self.sent = bytearray()
        self.closed = False
        self.timeout = None
        self.empty_returns = empty_returns  # rx 耗尽时：None=抛 TimeoutError，否则返回该值

    def sendall(self, b):
        self.sent += b

    def settimeout(self, t):
        self.timeout = t

    def recv(self, n):
        if self.rx:
            out = bytes(self.rx[:n])
            del self.rx[:n]
            return out
        if self.empty_returns is not None:
            return self.empty_returns
        raise TimeoutError  # 3.10+ socket.timeout 即 TimeoutError

    def close(self):
        self.closed = True


class FakeWS:
    """替身 _WS：按脚本回放 recv_text，从发送的命令推导真实哨兵"""

    def __init__(self, script):
        self.script = list(script)
        self.i = 0
        self.sent = []
        self.handshake_head = b""

    def send_text(self, payload):
        self.sent.append(payload)

    def recv_text(self, timeout):
        if self.i < len(self.script):
            out = self.script[self.i]
            self.i += 1
            if callable(out):
                return out(self.sent[-1].decode("utf-8", "replace"))
            return out
        time.sleep(0.02)
        return ""

    def close(self):
        pass


def srv_frame(opcode: int, payload: bytes) -> bytes:
    """构造服务端未掩码帧"""
    n = len(payload)
    if n < 126:
        return bytes([0x80 | opcode, n]) + payload
    if n < 65536:
        return bytes([0x80 | opcode, 126]) + n.to_bytes(2, "big") + payload
    return bytes([0x80 | opcode, 127]) + n.to_bytes(8, "big") + payload


def parse_masked_frame(data: bytes, offset: int = 0):
    """解析客户端掩码帧，返回 (opcode, payload, 帧结束偏移)"""
    b1, b2 = data[offset], data[offset + 1]
    ln, off = b2 & 0x7F, offset + 2
    if ln == 126:
        ln, off = int.from_bytes(data[off:off + 2], "big"), off + 2
    elif ln == 127:
        ln, off = int.from_bytes(data[off:off + 8], "big"), off + 8
    assert b2 & 0x80, "客户端帧必须置掩码位"
    mask, off = data[off:off + 4], off + 4
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(data[off:off + ln]))
    return b1 & 0x0F, payload, off + ln


# ---------------- token 解析 ----------------

class TestExtractToken(unittest.TestCase):
    def test_from_set_cookie(self):
        self.assertEqual(g._extract_token(FakeHeaders(["goploy_token=abc; Path=/"])), "abc")
        self.assertEqual(g._extract_token(FakeHeaders(["a=1; Path=/", "goploy_token=xyz; HttpOnly"])), "xyz")
        self.assertEqual(g._extract_token(FakeHeaders(["other=1"])), "")
        self.assertEqual(g._extract_token(FakeHeaders([])), "")

    def test_from_ws_handshake_head(self):
        head = "HTTP/1.1 101 Switching Protocols\r\nset-cookie: goploy_token=t2; Path=/\r\n\r\n"
        self.assertEqual(g._extract_token_from_raw(head), "t2")
        self.assertEqual(g._extract_token_from_raw("HTTP/1.1 101\r\n\r\n"), "")


# ---------------- WebSocket 帧编解码 ----------------

class TestFrames(unittest.TestCase):
    def test_send_text_length_classes(self):
        for size in (5, 200, 70000):
            sock = FakeSock()
            g._WS(sock).send_text(b"x" * size)
            opcode, payload, _ = parse_masked_frame(bytes(sock.sent))
            self.assertEqual(opcode, 1)
            self.assertEqual(len(payload), size)

    def test_pong_short_payload(self):
        sock = FakeSock()
        g._WS(sock)._pong(b"ping")
        opcode, payload, _ = parse_masked_frame(bytes(sock.sent))
        self.assertEqual(opcode, 0xA)
        self.assertEqual(payload, b"ping")

    def test_pong_extended_length(self):
        """回归：>=126 字节 ping 载荷必须用扩展长度，不能塞进 7 位长度"""
        sock = FakeSock()
        g._WS(sock)._pong(b"p" * 130)
        self.assertEqual(sock.sent[1] & 0x7F, 126)  # 扩展长度标记
        opcode, payload, _ = parse_masked_frame(bytes(sock.sent))
        self.assertEqual(opcode, 0xA)
        self.assertEqual(len(payload), 130)

    def test_next_frame_length_classes(self):
        for size in (5, 300, 70000):
            ws = g._WS(FakeSock())
            ws.buf = srv_frame(1, b"y" * size)
            opcode, payload = ws._next_frame()
            self.assertEqual(opcode, 1)
            self.assertEqual(len(payload), size)

    def test_next_frame_fragmented(self):
        ws = g._WS(FakeSock())
        frame = srv_frame(1, b"hello world, fragmented")
        ws.buf = frame[:5]
        self.assertIsNone(ws._next_frame())
        ws.buf += frame[5:]
        opcode, payload = ws._next_frame()
        self.assertEqual(payload, b"hello world, fragmented")

    def test_recv_text_ping_pong_and_concat(self):
        sock = FakeSock(srv_frame(1, b"hello") + srv_frame(9, b"ping") + srv_frame(1, b"world"))
        ws = g._WS(sock)
        out = ws.recv_text(0.3)
        self.assertEqual(out, "helloworld")
        opcode, payload, _ = parse_masked_frame(bytes(sock.sent))
        self.assertEqual(opcode, 0xA)
        self.assertEqual(payload, b"ping")

    def test_recv_text_close_frame_stops(self):
        ws = g._WS(FakeSock(srv_frame(8, b"") + srv_frame(1, b"after")))
        self.assertEqual(ws.recv_text(0.3), "")


# ---------------- _WS.connect 握手 ----------------

@mock.patch("goploy_mcp.socket.create_connection")
class TestConnect(unittest.TestCase):
    def test_handshake_ok_and_headers(self, create_conn):
        sock = FakeSock(b"HTTP/1.1 101 Switching Protocols\r\n"
                        b"set-cookie: goploy_token=t2; Path=/\r\n\r\n")
        create_conn.return_value = sock
        ws = g._WS.connect("http://goploy.test", "/ws/xterm", "goploy_token=t1")
        req = sock.sent.decode()
        self.assertIn("Upgrade: websocket", req)
        self.assertIn("Cookie: goploy_token=t1", req)
        self.assertIn("/ws/xterm", req)
        self.assertEqual(g._extract_token_from_raw(ws.handshake_head.decode("latin-1")), "t2")

    def test_rejected_raises_and_closes_socket(self, create_conn):
        """回归：握手被拒必须关闭 socket，不能泄漏 fd"""
        sock = FakeSock(b"HTTP/1.1 401 Unauthorized\r\n\r\n")
        create_conn.return_value = sock
        with self.assertRaises(g.WSRejectedError):
            g._WS.connect("http://goploy.test", "/ws/xterm", "goploy_token=t1")
        self.assertTrue(sock.closed)

    def test_closed_mid_handshake_closes_socket(self, create_conn):
        sock = FakeSock(b"", empty_returns=b"")
        create_conn.return_value = sock
        with self.assertRaises(g.GoployError):
            g._WS.connect("http://goploy.test", "/ws/xterm", "goploy_token=t1")
        self.assertTrue(sock.closed)

    def test_https_wraps_tls(self, create_conn):
        """回归：https 主机必须做 TLS 包装（Origin/Host 不受影响）"""
        raw = FakeSock()
        create_conn.return_value = raw
        wrapped = FakeSock(b"HTTP/1.1 101 Switching Protocols\r\n\r\n")
        ctx = mock.MagicMock()
        ctx.wrap_socket.return_value = wrapped
        with mock.patch.object(g.ssl, "create_default_context", return_value=ctx):
            g._WS.connect("https://goploy.test", "/ws/xterm", "goploy_token=t1")
        ctx.wrap_socket.assert_called_once_with(raw, server_hostname="goploy.test")

    def test_http_no_tls(self, create_conn):
        create_conn.return_value = FakeSock(b"HTTP/1.1 101 Switching Protocols\r\n\r\n")
        ctx = mock.MagicMock()
        with mock.patch.object(g.ssl, "create_default_context", return_value=ctx):
            g._WS.connect("http://goploy.test", "/ws/xterm", "goploy_token=t1")
        ctx.wrap_socket.assert_not_called()


# ---------------- login / state ----------------

class TestLogin(unittest.TestCase):
    def _run(self, code, data, cookie_token=""):
        captured = {}

        def fake_save(**kwargs):
            captured.update(kwargs)
            return dict(kwargs)

        with mock.patch.object(g, "_http", return_value=(code, "msg", data, cookie_token)), \
                mock.patch.object(g, "save_state", side_effect=fake_save), \
                mock.patch.object(g, "load_config",
                                  return_value={"host": "http://h", "account": "a", "password": "p"}):
            g.login()
        return captured

    def test_new_schema_and_cookie_token_preferred(self):
        data = {"token": "body-token",
                "namespaceList": [{"namespaceId": 1, "namespaceName": "A"},
                                  {"namespaceId": 2, "namespaceName": "B"}]}
        st = self._run(0, data, cookie_token="cookie-token")
        self.assertEqual(st["token"], "cookie-token")
        self.assertEqual(st["namespaces"],
                         [{"namespaceId": 1, "namespaceName": "A"},
                          {"namespaceId": 2, "namespaceName": "B"}])

    def test_legacy_schema_with_role(self):
        data = {"token": "t", "namespaceList": [{"id": 5, "name": "D", "role_id": 3}]}
        st = self._run(0, data)
        self.assertEqual(st["namespaces"], [{"namespaceId": 5, "namespaceName": "D", "roleId": 3}])

    def test_junk_entries_skipped(self):
        data = {"token": "t", "namespaceList": ["x", {"no_id": 1}, None, {"id": 9, "name": "ok"}]}
        st = self._run(0, data)
        self.assertEqual(st["namespaces"], [{"namespaceId": 9, "namespaceName": "ok"}])

    def test_login_failure_no_retry(self):
        with mock.patch.object(g, "_http", return_value=(1, "密码错误", None, "")), \
                mock.patch.object(g, "load_config",
                                  return_value={"host": "http://h", "account": "a", "password": "p"}):
            with self.assertRaises(g.GoployError) as cm:
                g.login()
        self.assertIn("登录失败", str(cm.exception))


class TestState(unittest.TestCase):
    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            with mock.patch.object(g, "STATE_PATH", path), mock.patch.object(g, "STATE_DIR", Path(td)):
                g.save_state(token="tok", namespaces=[{"namespaceId": 1}])
                st = g.load_state()
        self.assertEqual(st["token"], "tok")
        self.assertEqual(st["namespaces"], [{"namespaceId": 1}])

    def test_load_state_corrupt_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            path.write_text("{broken", encoding="utf-8")
            with mock.patch.object(g, "STATE_PATH", path):
                st = g.load_state()
        self.assertEqual(st, {"token": "", "namespaces": []})


# ---------------- resolve_server ----------------

class TestResolveServer(unittest.TestCase):
    def test_explicit_server_skips_api(self):
        with mock.patch.object(g, "resolve_namespace", return_value=(7, [])), \
                mock.patch.object(g, "api_call") as api:
            self.assertEqual(g.resolve_server(7, 3), (7, 3))
        api.assert_not_called()

    def test_unique_server_autopick(self):
        data = {"list": [{"id": 9, "name": "s1", "ip": "1.2.3.4"}]}
        with mock.patch.object(g, "resolve_namespace", return_value=(7, [])), \
                mock.patch.object(g, "api_call", return_value=data):
            self.assertEqual(g.resolve_server(7, None), (7, 9))

    def test_multiple_servers_require_id(self):
        data = {"list": [{"id": 9, "name": "s1", "ip": "1.1.1.1"},
                         {"id": 10, "name": "s2", "ip": "2.2.2.2"}]}
        with mock.patch.object(g, "resolve_namespace", return_value=(7, [])), \
                mock.patch.object(g, "api_call", return_value=data):
            with self.assertRaises(g.GoployError) as cm:
                g.resolve_server(7, None)
        self.assertIn("请指定 server_id", str(cm.exception))


# ---------------- deploy tools ----------------

class TestDeployTools(unittest.TestCase):
    def test_deploy_list_brief_and_state_desc(self):
        data = {"list": [{"id": 106, "name": "svc", "repoType": "git", "branch": "master",
                          "deployState": 2, "lastPublishToken": "tk",
                          "script": {"afterDeploy": {"content": "secret"}}}]}
        with mock.patch.object(g, "resolve_namespace", return_value=(11, [])), \
                mock.patch.object(g, "api_call", return_value=data) as api:
            out = g.deploy_list(None)
        api.assert_called_once_with("/deploy/getList", namespace_id=11)
        parsed = json.loads(out)
        self.assertEqual(parsed[0]["id"], 106)
        self.assertEqual(parsed[0]["deployStateDesc"], "部署成功")
        self.assertNotIn("script", parsed[0])  # 不泄露脚本内容

    def test_deploy_publish_body_and_token(self):
        captured = {}

        def fake_api(path, method="GET", body=None, namespace_id=None):
            captured.update(path=path, method=method, body=body, ns=namespace_id)
            return {"token": "tok-1"}

        with mock.patch.object(g, "resolve_namespace", return_value=(11, [])), \
                mock.patch.object(g, "api_call", side_effect=fake_api):
            out = g.deploy_publish(106, commit="abc123", branch="dev", server_ids=[3, 5])
        self.assertEqual(captured, {"path": "/deploy/publish", "method": "POST",
                                    "body": {"projectId": 106, "commit": "abc123",
                                             "branch": "dev", "serverIds": [3, 5]},
                                    "ns": 11})
        self.assertIn("tok-1", out)

    def test_deploy_publish_minimal_body(self):
        captured = {}

        def fake_api(path, method="GET", body=None, namespace_id=None):
            captured.update(body=body)
            return {"token": "t"}

        with mock.patch.object(g, "resolve_namespace", return_value=(11, [])), \
                mock.patch.object(g, "api_call", side_effect=fake_api):
            g.deploy_publish(106)
        self.assertEqual(captured["body"], {"projectId": 106, "commit": "", "branch": ""})

    def test_deploy_progress_path_and_state_desc(self):
        captured = {}

        def fake_api(path, method="GET", body=None, namespace_id=None):
            captured.update(path=path, ns=namespace_id)
            return {"state": 1, "stage": "Pull", "message": ""}

        with mock.patch.object(g, "resolve_namespace", return_value=(11, [])), \
                mock.patch.object(g, "api_call", side_effect=fake_api):
            out = g.deploy_progress("uuid-token-9")
        self.assertEqual(captured["path"],
                         "/deploy/getPublishProgress?lastPublishToken=uuid-token-9")
        self.assertEqual(captured["ns"], 11)
        parsed = json.loads(out)
        self.assertEqual(parsed["stateDesc"], "进行中")
        self.assertEqual(parsed["stage"], "Pull")


# ---------------- ws_exec ----------------

def _fake_terminal(_unused=""):
    """回放脚本：横幅+提示符 -> 回显命令+输出+真实哨兵+新提示符（提示符须含 ]# 以命中 PROMPT_RE）"""
    def phase2(sent_cmd: str) -> str:
        m = re.search(r'__MCP_"DONE_([0-9a-f]+)"__', sent_cmd)
        sentinel = f"__MCP_DONE_{m.group(1)}__"
        return f'{sent_cmd.rstrip(chr(10))}\r\nLINE1\r\nLINE2\r\n{sentinel}\r\n[root@host ~]# '
    return FakeWS(["Welcome!\r\n[root@host ~]# ", phase2])


class TestWsExec(unittest.TestCase):
    @contextlib.contextmanager
    def _patch_env(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(g, "load_state", return_value={"token": "old"}))
            stack.enter_context(mock.patch.object(g, "load_config", return_value={"host": "http://goploy.test"}))
            yield

    def test_output_cleaning(self):
        ws = _fake_terminal("cat f")
        with mock.patch.object(g, "_WS") as Ws:
            Ws.connect.return_value = ws
            with self._patch_env():
                out = g.ws_exec("cat f", 1, 1, 2)
        self.assertEqual(out, "LINE1\nLINE2")
        cmd = ws.sent[0].decode()
        self.assertTrue(cmd.startswith("cat f; echo __MCP_"))
        self.assertIn('"DONE_', cmd)  # 哨兵引号拆分技巧仍在

    def test_relogin_once_on_rejection(self):
        """回归：token 失效被拒 -> 重登一次 -> 用新 token 重连"""
        cookies = []

        def fake_connect(host, path, cookie):
            cookies.append(cookie)
            if len(cookies) == 1:
                raise g.WSRejectedError("WebSocket 握手被拒绝: 401")
            return _fake_terminal("cat f")

        with mock.patch.object(g._WS, "connect", side_effect=fake_connect), \
                mock.patch.object(g, "login", return_value={"token": "new"}) as login:
            with self._patch_env():
                out = g.ws_exec("cat f", 1, 1, 2)
        self.assertEqual(out, "LINE1\nLINE2")
        login.assert_called_once()
        self.assertEqual(cookies, ["goploy_token=old", "goploy_token=new"])

    def test_second_rejection_propagates(self):
        with mock.patch.object(g._WS, "connect",
                               side_effect=g.WSRejectedError("WebSocket 握手被拒绝: 403")), \
                mock.patch.object(g, "login", return_value={"token": "new"}) as login:
            with self._patch_env():
                with self.assertRaises(g.WSRejectedError):
                    g.ws_exec("cat f", 1, 1, 2)
        login.assert_called_once()  # 只重登一次，绝不更多

    def test_no_relogin_on_other_errors(self):
        with mock.patch.object(g._WS, "connect",
                               side_effect=g.GoployError("WebSocket 握手失败: 连接被关闭")), \
                mock.patch.object(g, "login") as login:
            with self._patch_env():
                with self.assertRaises(g.GoployError):
                    g.ws_exec("cat f", 1, 1, 2)
        login.assert_not_called()

    def test_no_token_logs_in_first(self):
        ws = _fake_terminal("cat f")
        with mock.patch.object(g, "_WS") as Ws:
            Ws.connect.return_value = ws
            with mock.patch.object(g, "load_state", return_value={"token": ""}), \
                    mock.patch.object(g, "load_config", return_value={"host": "http://h"}), \
                    mock.patch.object(g, "login", return_value={"token": "fresh"}):
                g.ws_exec("cat f", 1, 1, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
