"""Clash / mihomo REST API 封装（零第三方依赖，走 curl 子进程）。

支持 Clash Verge Rev 的 unix socket（/tmp/verge/verge-mihomo.sock）
与标准 TCP external-controller（127.0.0.1:9090 等），自动探测。

实战验证过的接口：
- GET  /configs                当前模式
- PATCH /configs {"mode": ...} 切换模式（rule/global）
- GET  /proxies                全部代理与组
- GET  /proxies/{name}         单个代理/组详情
- PUT  /proxies/{name}         切换组内节点（成功返回 204 空 body）
"""
import json
import shutil
import subprocess

DEFAULT_SOCKETS = [
    "/tmp/verge/verge-mihomo.sock",   # Clash Verge Rev
    "/run/clash.sock",                 # Linux systemd / openwrt 常见
]
DEFAULT_TCP = [("127.0.0.1", 9090), ("127.0.0.1", 7890)]

# 组类型（不是真实节点，测试时跳过）
GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance", "Direct", "Reject"}
# 保留字（不可切）
RESERVED = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE", "PASS-RULE", "PASS-REJECT"}


class ClashAPI:
    def __init__(self, socket_path=None, host=None, port=None):
        self.socket_path = socket_path
        self.host = host
        self.port = port
        self._endpoint = self._probe() if not (host and port) and not socket_path else (socket_path or (host, port))

    def _probe(self):
        """自动探测可用的控制端。"""
        if shutil.which("curl") is None:
            raise RuntimeError("需要 curl（macOS/Linux 自带）")
        # 先探测 unix socket
        for s in DEFAULT_SOCKETS:
            try:
                code, _ = self._raw("GET", "/version", sock=s)
                if code == 200:
                    return s
            except Exception:
                continue
        # 再探测 TCP
        for host, port in DEFAULT_TCP:
            try:
                code, _ = self._raw("GET", "/version", sock=None, host=host, port=port)
                if code == 200:
                    return (host, port)
            except Exception:
                continue
        raise RuntimeError("未找到可用的 Clash/mihomo 控制端，请配置 clash-api 段")

    def _raw(self, method, path, data=None, sock=None, host=None, port=None):
        ep = getattr(self, "_endpoint", None)
        sock = sock if sock is not None else (ep if isinstance(ep, str) else None)
        if isinstance(ep, tuple) and host is None and port is None:
            host, port = ep
        url = f"http://localhost{path}"
        cmd = ["curl", "-s", "-m", "10"]
        if sock:
            cmd += ["--unix-socket", sock]
        cmd += ["-X", method, "-w", "\n%{http_code}", url]
        if data is not None:
            cmd += ["-d", json.dumps(data, ensure_ascii=False)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"curl 失败: {r.stderr.strip()[:200]}")
        body, _, code = r.stdout.rpartition("\n")
        return int(code), body

    def api(self, method, path, data=None):
        code, body = self._raw(method, path, data)
        return code, (json.loads(body) if body.strip() else None)

    # ---- 常用接口 ----

    def get_mode(self) -> str:
        _, d = self.api("GET", "/configs")
        return (d or {}).get("mode", "rule")

    def set_mode(self, mode: str):
        self.api("PATCH", "/configs", {"mode": mode})

    def list_real_nodes(self, with_type=False):
        """返回全部真实代理节点（排除组/保留字），可选附带协议类型。"""
        _, d = self.api("GET", "/proxies")
        if not d:
            return [] if not with_type else []
        proxies = d.get("proxies", {})
        out = []
        for name, info in proxies.items():
            if name in RESERVED:
                continue
            t = (info or {}).get("type")
            if t in GROUP_TYPES:
                continue
            if not t:
                continue  # 信息型节点（流量/提示）无 type
            out.append((name, t) if with_type else name)
        return out

    def get_global_now(self) -> str:
        _, d = self.api("GET", "/proxies/GLOBAL")
        return (d or {}).get("now", "")

    def switch_global(self, name: str) -> bool:
        code, _ = self.api("PUT", "/proxies/GLOBAL", {"name": name})
        return code == 204

    def delay(self, name: str, url: str = "https://www.gstatic.com/generate_204", timeout: int = 5000):
        """测试节点延迟（毫秒），失败返回 None。"""
        import urllib.parse
        q = urllib.parse.urlencode({"url": url, "timeout": timeout})
        code, d = self.api("GET", f"/proxies/{name}/delay?{q}")
        if code != 200:
            return None
        return (d or {}).get("delay")
