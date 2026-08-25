"""GUI 后端：本地 HTTP 服务 + 检测线程管理（复用 detector 内核）。

启动: python3 gui.py [--port 8899] [--no-open]
前端: 单文件 detector/gui_static/index.html（深色现代 UI，轮询 API）
API:
  GET  /               前端页面
  GET  /api/status     Clash 连接状态 / 节点列表 / 检测是否运行中
  POST /api/scan       开始检测 {nodes?: [...]}（空=全部）
  GET  /api/progress   检测进度与结果
  POST /api/stop       停止检测（停止后自动恢复 Clash 环境）
  GET  /api/rules      当前干净节点生成的规则片段
  POST /api/apply      应用规则 {verge: bool}（verge=true 写 Clash Verge 扩展）
"""
import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from clash_api import ClashAPI
from cdp_tester import HeadlessChrome, test_node, Verdict
from rules import generate_rules_yaml, write_verge_extensions
from scan import scan_nodes

STATIC_DIR = Path(__file__).parent / "gui_static"


class ScanState:
    """检测线程的共享状态。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.running = False
            self.stop_requested = False
            self.current = None
            self.total = 0
            self.done = 0
            self.results = []
            self.error = None
            self.finished_at = None

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running,
                "current": self.current,
                "total": self.total,
                "done": self.done,
                "results": list(self.results),
                "error": self.error,
                "finished_at": self.finished_at,
            }


STATE = ScanState()
API_CACHE = {"clash": None}


def get_clash():
    """带缓存的 ClashAPI 实例（探测失败返回 None）。"""
    if API_CACHE["clash"] is None:
        try:
            API_CACHE["clash"] = ClashAPI()
        except Exception:
            return None
    return API_CACHE["clash"]


def _snapshot_node(node):
    """从运行结果中取某个节点的判定（未测返回 None）。"""
    for r in STATE.results:
        if r["node"] == node:
            return r
    return None


def run_scan(nodes=None, sleep_sec=3.0, proxy_port=7897, chrome=None, parallel=1):
    """后台检测线程。parallel=1 串行（主实例）；parallel>=2 并行（临时实例池，不碰主实例）。"""
    parallel = max(1, int(parallel))
    STATE.running = True
    STATE.error = None
    try:
        if parallel > 1:
            def on_result(node, r, worker_id, seq):
                STATE.current = node
                STATE.done = seq
                STATE.results.append(r)
            scan_nodes(None, proxy_port, nodes, parallel=parallel,
                       chrome_path=chrome, sleep_sec=sleep_sec,
                       on_result=on_result, stop_flag=lambda: STATE.stop_requested)
        else:
            api = get_clash()
            if api is None:
                STATE.error = "未找到可用的 Clash/mihomo 控制端"
                return
            mode_before = api.get_mode()
            global_before = api.get_global_now()
            api.set_mode("global")
            with HeadlessChrome(proxy_port=proxy_port, chrome_path=chrome) as chrome:
                for i, node in enumerate(nodes):
                    if STATE.stop_requested:
                        break
                    STATE.current = node
                    STATE.done = i
                    ok = api.switch_global(node)
                    if not ok:
                        STATE.results.append({"node": node, "verdict": Verdict.ERROR, "reply": "节点切换失败"})
                        continue
                    time.sleep(sleep_sec)
                    r = test_node(chrome, node)
                    STATE.results.append(r)
                STATE.done = len(nodes)
    except Exception as e:
        STATE.error = f"{e}\n{traceback.format_exc(limit=3)}"
    finally:
        if parallel == 1:
            try:
                api.set_mode(mode_before)
                if global_before and global_before != "DIRECT":
                    api.switch_global(global_before)
            except Exception:
                pass
        STATE.running = False
        STATE.current = None
        STATE.finished_at = time.time()


class Handler(BaseHTTPRequestHandler):
    server_version = "CDD-GUI/1.0"

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):
        pass  # 静默

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    # ---------- 路由 ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/" or path == "/index.html":
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            self._send(200, html, "text/html; charset=utf-8")
        elif path == "/api/status":
            self._status()
        elif path == "/api/progress":
            self._progress()
        elif path == "/api/rules":
            self._rules()
        elif path == "/api/plugin.zip":
            self._plugin_zip()
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/scan":
            self._scan()
        elif path == "/api/stop":
            self._stop()
        elif path == "/api/apply":
            self._apply()
        else:
            self._send(404, {"error": "not found"})

    # ---------- API 实现 ----------
    def _status(self):
        api = get_clash()
        if api is None:
            self._send(200, {"clash": False, "error": "未找到 Clash/mihomo 控制端", "busy": STATE.running})
            return
        try:
            mode = api.get_mode()
            global_now = api.get_global_now()
            nodes = api.list_real_nodes()
            clean = sum(1 for r in STATE.results if r["verdict"] == Verdict.LUNA)
            mini = sum(1 for r in STATE.results if r["verdict"] == Verdict.MINI)
            wall = sum(1 for r in STATE.results if r["verdict"] == Verdict.LOGIN_WALL)
            self._send(200, {
                "clash": True,
                "endpoint": str(api._endpoint),
                "mode": mode,
                "global_now": global_now,
                "nodes": nodes,
                "node_count": len(nodes),
                "busy": STATE.running,
                "counts": {"clean": clean, "mini": mini, "wall": wall},
            })
        except Exception as e:
            self._send(200, {"clash": False, "error": str(e), "busy": STATE.running})

    def _scan(self):
        if STATE.running:
            self._send(409, {"error": "检测正在进行中"})
            return
        data = self._read_json()
        parallel = max(1, int(data.get("parallel", 1) or 1))
        nodes = data.get("nodes")
        if not nodes:
            if parallel > 1:
                # 并行模式：节点列表从临时实例取（不依赖主实例控制端）
                from mihomo_pool import MihomoPool
                try:
                    with MihomoPool(1) as pool:
                        api = ClashAPI(host="127.0.0.1", port=pool[0].ctl_port)
                        nodes = api.list_real_nodes()
                except Exception as e:
                    self._send(400, {"error": f"并行实例启动失败: {e}"})
                    return
            else:
                api = get_clash()
                if api is None:
                    self._send(400, {"error": "未找到 Clash 控制端"})
                    return
                nodes = api.list_real_nodes()
        STATE.reset()
        STATE.total = len(nodes)
        t = threading.Thread(target=run_scan, kwargs={
            "nodes": nodes,
            "sleep_sec": data.get("sleep", 3.0),
            "proxy_port": data.get("proxy_port", 7897),
            "parallel": parallel,
        }, daemon=True)
        t.start()
        self._send(200, {"ok": True, "total": len(nodes), "parallel": parallel})

    def _progress(self):
        snap = STATE.snapshot()
        self._send(200, snap)

    def _stop(self):
        STATE.stop_requested = True
        self._send(200, {"ok": True})

    def _rules(self):
        clean = [r["node"] for r in STATE.results if r["verdict"] == Verdict.LUNA]
        if not clean:
            self._send(200, {"clean": [], "rules": "", "hint": "还没有检测出干净节点"})
            return
        self._send(200, {"clean": clean, "rules": generate_rules_yaml(clean)})

    def _plugin_zip(self):
        """浏览器插件下载：优先返回仓库内置 zip，缺失则现场打包 extension/ 目录。"""
        import io
        import shutil
        import zipfile
        root = Path(__file__).parent.parent  # 项目根
        ext_dir = root / "extension"
        zips = sorted(ext_dir.glob("*.zip")) if ext_dir.is_dir() else []
        if zips:
            data = zips[0].read_bytes()
            fname = zips[0].name
        elif ext_dir.is_dir():
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in ext_dir.iterdir():
                    if f.is_file() and f.suffix != ".zip":
                        zf.write(f, f.name)
            data = buf.getvalue()
            fname = "chatgpt-fingerprint-extension.zip"
        else:
            self._send(404, {"error": "插件目录不存在"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.end_headers()
        self.wfile.write(data)

    def _apply(self):
        data = self._read_json()
        clean = [r["node"] for r in STATE.results if r["verdict"] == Verdict.LUNA]
        if not clean:
            self._send(400, {"error": "没有可应用的干净节点，请先完成检测"})
            return
        out = {"clean": clean, "rules": generate_rules_yaml(clean)}
        if data.get("verge"):
            try:
                info = write_verge_extensions(clean)
                out["verge"] = info
            except Exception as e:
                self._send(400, {"error": f"Clash Verge 扩展写入失败: {e}"})
                return
        self._send(200, out)


def start_server(port: int = 8899):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return srv
