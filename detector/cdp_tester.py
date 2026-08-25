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


def _dismiss_cookie_banner(cdp) -> str:
    """处理 ChatGPT 首次访问的 Cookie 弹窗。

    新版界面（wm-composer）未处理 Cookie 弹窗时，发送按钮虽显示可用但点击无效，
    消息发不出去 → 必须先把弹窗点掉（模拟真实用户首次访问的行为）。
    多语言兜底匹配（中文界面/英文界面）。
    """
    try:
        return cdp.ev("""(() => {
            const btns = [...document.querySelectorAll('button')];
            const ACCEPT = ['全部接受', '接受全部', 'Accept all', 'Agree', 'I agree'];
            const REJECT = ['拒绝非必需', '拒绝', 'Reject', 'Decline'];
            const pick = (list) => btns.find(x => {
                const a = x.getAttribute('aria-label') || '';
                const t = (x.textContent || '').trim();
                return list.some(l => a.includes(l) || t === l || t.includes(l));
            });
            const b = pick(ACCEPT) || pick(REJECT);
            if (!b) return 'NONE';
            b.click();
            return 'CLICKED';
        })()""")
    except Exception:
        return "EVAL_ERR"


def _type_and_send(cdp) -> str:
    """设值 → 等发送按钮可用 → 点击 → 确认消息已进入对话。返回状态字符串。"""
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
    except Exception as e:
        return f"SET_ERR:{str(e)[:60]}"

    # 等发送按钮可用（React 同步需要时间，页面卡时更久）
    st = "TIMEOUT"
    for _ in range(15):
        time.sleep(1)
        try:
            st = cdp.ev(
                "(() => { const b = [...document.querySelectorAll('button')]"
                ".find(b => (b.getAttribute('aria-label') || '').includes('发送')"
                " || (b.getAttribute('aria-label') || '').includes('Send'));"
                " return b ? (b.disabled ? 'DISABLED' : 'READY') : 'NOBTN'; })()")
            if st == "READY":
                break
        except Exception:
            pass
    if st != "READY":
        return f"BTN:{st}"

    # 点击发送
    try:
        cdp.ev(
            "(() => { const b = [...document.querySelectorAll('button')]"
            ".find(b => (b.getAttribute('aria-label') || '').includes('发送')"
            " || (b.getAttribute('aria-label') || '').includes('Send'));"
            " if (!b) return 'NOBTN'; b.click(); return 'SENT'; })()")
    except Exception as e:
        return f"CLICK_ERR:{str(e)[:60]}"

    # 确认已发送：消息进入对话（出现「你说/You said」）或输入框已清空
    for _ in range(10):
        time.sleep(1)
        try:
            ok = cdp.ev(
                "(() => { const t = document.body.innerText || '';"
                " const ta = document.querySelector('textarea');"
                " return (t.includes('你说') || t.includes('You said')"
                " || !ta || !ta.value) ? 'OK' : 'PENDING'; })()")
            if ok == "OK":
                return "SENT_OK"
        except Exception:
            pass
    return "SENT_UNCONFIRMED"


def _wait_reply(cdp, timeout_reply: float) -> str | None:
    """等模型回复，返回判定（LUNA/MINI/LOGIN_WALL）或 None（超时）。

    关键：页面/回复慢时绝不提前下结论——
      - 检测到「正在生成」（停止按钮 / 文本在增长）就把等待窗口顺延，
        直到生成结束或拿到明确判定；
      - 总上限 = timeout_reply + 120s，防异常节点无限拖。
    """
    base_deadline = time.time() + timeout_reply
    hard_deadline = base_deadline + 120
    active_until = base_deadline
    last_len = 0
    while time.time() < active_until and time.time() < hard_deadline:
        time.sleep(2)
        try:
            r = cdp.ev(
                "(() => { const t = document.body.innerText || '';"
                " return JSON.stringify({len: t.length,"
                " stop: !!document.querySelector('[data-testid=\"stop-button\"]')"
                " || t.includes('停止生成') || t.includes('Stop generating'),"
                " tail: t.slice(-900)}); })()")
            d = json.loads(r)
        except Exception:
            continue
        t = d["tail"]
        if "Sign in is required" in t:
            return Verdict.LOGIN_WALL
        if "GPT-5.6" in t or "Luna" in t:
            return Verdict.LUNA
        if "GPT-5.5" in t or "mini" in t:
            return Verdict.MINI
        # 活性检测：正在生成或文本在增长 → 顺延等待窗口
        if d["stop"] or d["len"] > last_len:
            active_until = max(active_until, time.time() + 15)
        last_len = d["len"]
    return None


def test_node(chrome, node_name: str, timeout_page=90, timeout_reply=120) -> dict:
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

        # 3) 等页面加载（页面慢时最多等 timeout_page 秒）
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

        # 3.5) 处理 Cookie 弹窗（新版界面不点掉则发送无效）
        _dismiss_cookie_banner(cdp)
        time.sleep(1)

        # 4) 发送问题：设值 → 等按钮可用 → 点击 → 确认发送成功
        send_res = _type_and_send(cdp)
        if send_res != "SENT_OK":
            # 发送失败：先怀疑 Cookie 弹窗残留，重试一次
            _dismiss_cookie_banner(cdp)
            time.sleep(1)
            send_res = _type_and_send(cdp)
        if send_res != "SENT_OK":
            return {"node": node_name, "verdict": Verdict.ERROR,
                    "reply": f"发送失败 {send_res}"}

        # 5) 等回复：活性顺延 + 明确判定才分类，超时如实报 ERROR
        v = _wait_reply(cdp, timeout_reply)
        if v is not None:
            reply = {"node": node_name, "verdict": v,
                     "reply": {"LUNA": "GPT-5.6 Luna", "MINI": "GPT-5.5-mini",
                               "LOGIN_WALL": "Sign in is required to continue."}[v]}
            return reply

        tail = ""
        try:
            tail = str(cdp.ev("document.body ? document.body.innerText.slice(-200).replace(/\\n/g,' ') : 'NOBODY'"))[:120]
        except Exception:
            pass
        return {"node": node_name, "verdict": Verdict.ERROR,
                "reply": f"无回复 tail={tail}"}
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
