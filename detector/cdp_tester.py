"""Headless Chrome CDP 三判定检测器。

原理（2026-08 实战验证）：
  OpenAI 对降智名单内的 IP，未登录访问 chatgpt.com 会强制登录（Sign in is required），
  或仅允许 mini 模型；干净 IP 允许匿名使用 GPT-5.6 Luna。
  判定：发送「你是什么模型？」→ 回复 GPT-5.6 Luna = 干净(LUNA)
                               回复 GPT-5.5-mini = 半干净(MINI)
                               Sign in is required = 降智名单(LOGIN_WALL)

关键坑（全部实战踩过并修复）：
  1. Chrome 151+ 的 /json/new 必须用 PUT（GET 被拒绝）
  2. headless 默认 UA 带 HeadlessChrome 标记，被 Cloudflare 拦截 → 必须伪装 UA
  3. 匿名 ChatGPT 会话存 localStorage 跨 tab 共享 → 后测节点会读到首节点残留回复造成假 LUNA
     → 每个节点测试前必须 clearBrowserCookies + localStorage.clear 再重新导航
  4. React 受控 textarea 需要 setter + input 事件 + 等待状态同步后才能点发送按钮
  5. macOS 没有 GNU timeout 命令 → 脚本内部自管超时
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.request

import websocket  # pip install websocket-client

QUESTION = "你是什么模型？请直接回答模型名称"
CHROME_WINDOW = "--window-size=1280,900"


def _free_port() -> int:
    """找一个空闲端口给 CDP。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HeadlessChrome:
    """管理一个 headless Chrome 实例（独立 user-data-dir，零窗口）。"""

    def __init__(self, proxy_port: int = 7897, chrome_path: str = None):
        self.proxy_port = proxy_port
        self.chrome_path = chrome_path or _find_chrome()
        self.port = _free_port()
        self.user_data_dir = tempfile.mkdtemp(prefix="cdd-chrome-")
        self.proc = None

    def start(self):
        cmd = [
            self.chrome_path,
            "--headless=new",
            f"--user-data-dir={self.user_data_dir}",
            f"--remote-debugging-port={self.port}",
            "--remote-allow-origins=*",
            f"--proxy-server=http://127.0.0.1:{self.proxy_port}",
            # 伪装 UA：去掉 HeadlessChrome 标记，绕过 Cloudflare 对 headless 的拦截
            "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            CHROME_WINDOW,
            "about:blank",
        ]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        # 等待 CDP 就绪
        for _ in range(30):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=1)
                return
            except Exception:
                time.sleep(0.5)
        raise RuntimeError("headless Chrome 启动失败")

    def stop(self):
        if self.proc:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        shutil.rmtree(self.user_data_dir, ignore_errors=True)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()


class CDP:
    """极简 CDP 客户端（websocket-client）。"""

    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, origin="http://localhost", timeout=45)
        self._id = 0

    def send(self, method, params=None):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"]["message"])
                return msg.get("result", {})

    def ev(self, expr):
        r = self.send("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True,
        })
        if "exceptionDetails" in r:
            raise RuntimeError("eval exception")
        return (r.get("result") or {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def _find_chrome() -> str:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",   # macOS
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",                                        # Linux
        "/usr/bin/chromium",
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",    # Windows
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    which = shutil.which("chrome") or shutil.which("chromium") or shutil.which("google-chrome")
    if which:
        return which
    raise RuntimeError("未找到 Chrome，请用 --chrome 指定路径")


class Verdict:
    LUNA = "LUNA"            # GPT-5.6 Luna：IP 干净
    MINI = "MINI"            # GPT-5.5-mini：半干净
    LOGIN_WALL = "LOGIN_WALL"  # Sign in is required：降智名单
    ERROR = "ERROR"          # 页面异常/无回复


def test_node(chrome, node_name: str, timeout_page=70, timeout_reply=70) -> dict:
    """在指定 Chrome 实例中测试当前出口（需已切好节点）的降智判定。"""
    # 1) 新建 tab（新版 Chrome 要求 PUT）
    req = urllib.request.Request(
        f"http://127.0.0.1:{chrome.port}/json/new?https://chatgpt.com/", method="PUT")
    target = json.load(urllib.request.urlopen(req, timeout=15))
    cdp = CDP(target["webSocketDebuggerUrl"])
    try:
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("Network.enable")

        # 2) 清会话（关键防污染）：先加载 chatgpt origin → 清 localStorage/cookie → 重导航
        try:
            cdp.send("Page.navigate", {"url": "https://chatgpt.com/"})
            time.sleep(6)
            cdp.ev("localStorage.clear(); sessionStorage.clear(); 'ok'")
            cdp.send("Network.clearBrowserCookies")
            cdp.send("Page.navigate", {"url": "https://chatgpt.com/"})
        except Exception:
            pass

        # 3) 等页面加载
        page = None
        for _ in range(timeout_page // 2):
            time.sleep(2)
            try:
                page = cdp.ev(
                    "(() => { const t = document.body ? document.body.innerText : '';"
                    " return {ta: !!document.querySelector('textarea'),"
                    " login: t.includes('Sign in is required')}; })()")
            except Exception:
                page = None
            if page and (page.get("ta") or page.get("login")):
                break

        if not page:
            return {"node": node_name, "verdict": Verdict.ERROR, "reply": "页面无法加载"}

        if page.get("login"):
            return {"node": node_name, "verdict": Verdict.LOGIN_WALL,
                    "reply": "Sign in is required to continue."}

        if not page.get("ta"):
            return {"node": node_name, "verdict": Verdict.ERROR,
                    "reply": "页面无输入框（未登录界面异常）"}

        # 4) 发送问题：设值 → 等 React 同步 → 点发送按钮
        send_res = None
        try:
            cdp.ev(
                "(() => { const ta = document.querySelector('textarea');"
                " if (!ta) return 'NOTA';"
                " const setter = Object.getOwnPropertyDescriptor("
                "   window.HTMLTextAreaElement.prototype, 'value').set;"
                f" setter.call(ta, {json.dumps(QUESTION)});"
                " ta.dispatchEvent(new Event('input', {bubbles:true}));"
                " ta.dispatchEvent(new Event('change', {bubbles:true}));"
                " ta.focus(); return 'SET'; })()")
            time.sleep(2.5)
            send_res = cdp.ev(
                "(() => { const btn = document.querySelector("
                "   'button[data-testid=\"send-button\"]') ||"
                " [...document.querySelectorAll('button')].find(b =>"
                "   b.getAttribute('aria-label') === '发送消息' ||"
                "   b.getAttribute('aria-label') === 'Send message');"
                " if (!btn) return 'NOSENDBTN'; if (btn.disabled) return 'DISABLED';"
                " btn.click(); return 'SENT'; })()")
        except Exception as e:
            send_res = f"EVAL_ERR:{str(e)[:60]}"

        # 5) 等回复
        for _ in range(timeout_reply // 2):
            time.sleep(2)
            try:
                r = cdp.ev(
                    "(() => { const t = document.body.innerText;"
                    " return {t: t.slice(-500),"
                    " m: t.match(/GPT-5\\.\\d|5\\.6|5\\.5|luna|Luna|mini|Sign in is required[^\\n]*/g)}; })()")
                t = (r or {}).get("t") or ""
                if "Sign in is required" in t:
                    return {"node": node_name, "verdict": Verdict.LOGIN_WALL,
                            "reply": "Sign in is required to continue."}
                if "GPT-5.6" in t or "luna" in t.lower():
                    return {"node": node_name, "verdict": Verdict.LUNA, "reply": "GPT-5.6 Luna"}
                if "GPT-5.5" in t or "mini" in t.lower():
                    return {"node": node_name, "verdict": Verdict.MINI, "reply": "GPT-5.5-mini"}
            except Exception:
                pass

        tail = ""
        try:
            tail = str(cdp.ev("document.body ? document.body.innerText.slice(-200).replace(/\\n/g,' ') : 'NOBODY'"))[:120]
        except Exception:
            pass
        return {"node": node_name, "verdict": Verdict.ERROR,
                "reply": f"无回复 sendRes={send_res} tail={tail}"}
    finally:
        # 关闭本 tab，杜绝会话残留污染下一个节点
        try:
            cdp.send("Target.closeTarget", {"targetId": target["id"]})
        except Exception:
            pass
        cdp.close()


if __name__ == "__main__":
    # 自测：python3 cdp_tester.py <节点名> [代理端口]
    import sys
    name = sys.argv[1] if len(sys.argv) > 1 else None
    proxy = int(sys.argv[2]) if len(sys.argv) > 2 else 7897
    with HeadlessChrome(proxy_port=proxy) as chrome:
        print("headless 就绪，CDP 端口", chrome.port)
        if name:
            print(json.dumps(test_node(chrome, name), ensure_ascii=False))
