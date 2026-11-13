"""官方登录：驱动本机浏览器完成微信扫码 / 手机号 / 企业微信登录，成功后读走令牌。

原理：workbuddy.cn 网页登录的令牌最终由该域的 SPA 写进浏览器存储，
服务端拿不到。所以这里用 Chrome/Edge 的调试协议（CDP）起一个真实窗口，
让用户在窗口里正常扫码/收验证码，登录成功后在同一页面上下文里把令牌读出来。

不逆向任何密钥，也不伪造设备身份——只是替用户把「登录」这一步自动化了。
仅桌面端可用；容器里没有浏览器。
"""

import base64
import collections
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.request

# 这个入口会跳到带完整参数的官方登录页（微信扫码 / 手机号 / 企业微信 / SSO / QQ / 邮箱）
LOGIN_URL = "https://www.workbuddy.cn/agents"
SESSION_TTL = 300
POLL_INTERVAL = 1.5
QR_INTERVAL = 2.5

JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")

STORAGE_JS = r"""
(() => {
  const grab = (st, tag) => {
    const o = {};
    try {
      for (let i = 0; i < st.length; i++) {
        const k = st.key(i);
        o[tag + ':' + k] = st.getItem(k);
      }
    } catch (e) {}
    return o;
  };
  return JSON.stringify(Object.assign(grab(localStorage, 'ls'), grab(sessionStorage, 'ss')));
})()
"""

# 微信二维码在 open.weixin.qq.com 的 iframe 里（跨域，只能取位置不能取内容，
# 但截图截的是渲染结果，不受同源限制）
QR_RECT_JS = r"""
(() => {
  const f = document.querySelector('iframe[src*="open.weixin.qq.com"]');
  if (!f) return '';
  const r = f.getBoundingClientRect();
  if (!r.width || !r.height) return '';
  // 截图坐标是页面坐标，getBoundingClientRect 是视口坐标，要补上滚动量
  return JSON.stringify({
    x: r.x + window.scrollX, y: r.y + window.scrollY,
    w: r.width, h: r.height
  });
})()
"""

COOKIE_JWT_JS = r"""
(async () => {
  try {
    const r = await fetch('/v2/billing/meter/checkin-activity-status', {
      method: 'POST', credentials: 'include',
      headers: {'Content-Type': 'application/json'}, body: '{}'
    });
    return JSON.stringify({status: r.status});
  } catch (e) { return JSON.stringify({status: 0}); }
})()
"""

# 页面上显示的昵称：左下角用户菜单里的 user-menu-trigger-name 最可靠，
# 不要用「按文字猜」的办法，容易抓到「引用来源」这种无关文案
NICK_JS = r"""
(() => {
  const pick = (el) => {
    if (!el) return '';
    const t = (el.textContent || '').trim();
    return (t && t.length <= 24) ? t : '';
  };
  return pick(document.querySelector('[class*="user-menu-trigger-name"]'))
      || pick(document.querySelector('[class*="user-menu-trigger"] [class*="name"]'))
      || pick(document.querySelector('[class*="nickname"]'));
})()
"""


def find_browser():
    cands = []
    envs = ["ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"]
    for env in envs:
        base = os.environ.get(env)
        if not base:
            continue
        cands.append(os.path.join(base, r"Microsoft\Edge\Application\msedge.exe"))
        cands.append(os.path.join(base, r"Google\Chrome\Application\chrome.exe"))
    cands += [
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for p in cands:
        if p and os.path.isfile(p):
            brand = "Edge" if "edge" in p.lower() else "Chrome"
            return p, brand
    for name in ("google-chrome", "chromium", "microsoft-edge"):
        from shutil import which
        hit = which(name)
        if hit:
            return hit, "Chrome"
    return None, None


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------- 极简 WebSocket

class WS:
    def __init__(self, host, port, path, timeout=25):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key)
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise IOError("握手时连接被关闭")
            buf += chunk
        head, self._buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise IOError("WebSocket 握手失败")

    def _read_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise IOError("连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send(self, text):
        data = text.encode("utf-8")
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _pong(self, payload):
        header = bytearray([0x8A])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        else:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self):
        payload = b""
        while True:
            b1, b2 = self._read_exact(2)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            chunk = self._read_exact(n) if n else b""
            if mask:
                chunk = bytes(b ^ mask[i % 4] for i, b in enumerate(chunk))
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self._pong(chunk)
                continue
            if opcode == 0xA:
                continue
            payload += chunk
            if fin:
                return payload.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class CDP:
    """单线程使用（只有工作线程碰它），所以不加锁。"""

    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self.events = collections.deque(maxlen=400)

    def call(self, method, params=None, timeout=20):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self.ws.recv()
            if msg is None:
                raise IOError("调试连接已断开")
            try:
                data = json.loads(msg)
            except Exception:
                continue
            if data.get("id") != mid:
                if "method" in data:            # 事件：攒起来给抓包用
                    self.events.append(data)
                continue
            if "error" in data:
                raise RuntimeError(str(data["error"]))
            return data.get("result", {})
        raise TimeoutError("CDP 超时：%s" % method)

    def pump(self):
        """把管道里积压的事件读空（非阻塞地读一小会儿）。"""
        try:
            self.ws.sock.settimeout(0.05)
            while True:
                msg = self.ws.recv()
                if msg is None:
                    return
                try:
                    data = json.loads(msg)
                except Exception:
                    continue
                if "method" in data:
                    self.events.append(data)
        except socket.timeout:
            pass
        except Exception:
            pass
        finally:
            try:
                self.ws.sock.settimeout(25)
            except Exception:
                pass

    def drain(self):
        out = list(self.events)
        self.events.clear()
        return out

    def eval(self, expr, await_promise=False, timeout=20):
        r = self.call("Runtime.evaluate", {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": await_promise,
        }, timeout=timeout)
        res = r.get("result") or {}
        if res.get("subtype") == "error":
            raise RuntimeError(res.get("description") or "页面脚本执行失败")
        return res.get("value")

    def screenshot_clip(self, x, y, w, h):
        shot = self.call("Page.captureScreenshot", {
            "format": "png",
            "clip": {"x": max(0, x), "y": max(0, y), "width": w, "height": h, "scale": 1},
        })
        return base64.b64decode(shot.get("data") or "")


# ---------------------------------------------------------------- 会话

class Session:
    def __init__(self, sid, hint=""):
        self.sid = sid
        self.hint = hint
        self.state = "starting"
        self.message = "正在启动浏览器…"
        self.account = None
        self.qr = None
        self.qr_at = 0.0
        self.started_at = time.time()
        self.proc = None
        self.port = None
        self.profile = None
        self.brand = ""
        self.targets = []
        self.headless = True


class BrowserLogin:
    def __init__(self, data_dir, on_token, log=print):
        self.data_dir = data_dir
        self.on_token = on_token
        self.log = log
        self.sessions = {}
        self.lock = threading.Lock()

    # ---- 对外

    def available(self):
        return find_browser()[0] is not None

    def start(self, hint="", headless=True):
        exe, brand = find_browser()
        if not exe:
            return {"ok": False, "error": "本机没找到 Chrome 或 Edge；容器部署请用令牌方式添加"}
        sid = base64.urlsafe_b64encode(os.urandom(9)).decode().rstrip("=")
        s = Session(sid, hint)
        s.brand = brand
        s.headless = bool(headless)
        with self.lock:
            self.sessions[sid] = s
        threading.Thread(target=self._run, args=(s, exe), daemon=True).start()
        return {"ok": True, "sid": sid, "brand": brand, "headless": s.headless}

    def status(self, sid):
        with self.lock:
            s = self.sessions.get(sid)
        if not s:
            return {"ok": False, "state": "expired", "message": "会话不存在或已结束"}
        return {
            "ok": True,
            "state": s.state,
            "message": s.message,
            "account": s.account,
            "has_qr": bool(s.qr),
            "headless": s.headless,
            "targets": s.targets,
            "elapsed": int(time.time() - s.started_at),
        }

    def qr(self, sid):
        with self.lock:
            s = self.sessions.get(sid)
        return s.qr if s else None

    def cancel(self, sid):
        with self.lock:
            s = self.sessions.get(sid)
        if not s:
            return {"ok": True}
        s.state = "cancelled"
        s.message = "已取消"
        self._kill(s)
        return {"ok": True}

    def cleanup(self):
        now = time.time()
        with self.lock:
            items = list(self.sessions.items())
        for sid, s in items:
            if s.state in ("waiting", "starting") and now - s.started_at > SESSION_TTL:
                s.state = "error"
                s.message = "登录超时（5 分钟），已关闭浏览器"
                self._kill(s)
            if s.state in ("ok", "error", "cancelled") and now - s.started_at > 120:
                with self.lock:
                    self.sessions.pop(sid, None)

    # ---- 工作线程

    def _run(self, s, exe):
        profile = os.path.join(self.data_dir, "browser", s.sid)
        port = free_port()
        s.port, s.profile = port, profile
        try:
            os.makedirs(profile, exist_ok=True)
            args = [
                exe,
                "--remote-debugging-port=%d" % port,
                "--remote-allow-origins=*",
                "--user-data-dir=%s" % profile,
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-sync",
                "--disable-background-networking",
                "--window-size=1120,880",
            ]
            if s.headless:
                # 无头模式：不弹窗，二维码由后台截图送到控制台
                args += ["--headless=new", "--disable-gpu", "--hide-scrollbars", "--mute-audio"]
            else:
                args += ["--window-position=120,60"]
            args.append(LOGIN_URL)
            flags = 0x08000000 if os.name == "nt" else 0
            s.proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            ws_url = self._wait_target(port, s)
            if not ws_url:
                if s.state != "cancelled":
                    s.state = "error"
                    s.message = "浏览器启动失败，或调试端口没起来"
                return

            rest = ws_url.split("://", 1)[1]
            ws = WS("127.0.0.1", port, rest[rest.find("/"):])
            cdp = CDP(ws)
            cdp.call("Runtime.enable")
            try:
                cdp.call("Page.enable")
            except Exception:
                pass

            s.state = "waiting"
            s.message = ("正在打开官方登录页…" if s.headless
                         else "请在弹出的窗口里用微信扫码，或点「手机号」用验证码登录")
            self._loop(s, cdp, ws_url)
        except Exception as e:
            if s.state not in ("ok", "cancelled"):
                s.state = "error"
                s.message = "出错：%s" % e
                self.log("[官方登录] 异常: %s" % e)
        finally:
            if s.state != "ok":
                self._kill(s)
            s.qr = None
            self._drop_profile(s)

    def _wait_target(self, port, s):
        deadline = time.time() + 30
        while time.time() < deadline:
            if s.state == "cancelled":
                return None
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=2) as r:
                    targets = json.loads(r.read().decode("utf-8", "replace"))
                for t in targets:
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        return t["webSocketDebuggerUrl"]
            except Exception:
                pass
            time.sleep(0.4)
        return None

    def _targets(self, port):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=2) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return []

    def _attach(self, port, ws_url):
        rest = ws_url.split("://", 1)[1]
        cdp = CDP(WS("127.0.0.1", port, rest[rest.find("/"):]))
        cdp.call("Runtime.enable", timeout=8)
        try:
            cdp.call("Page.enable", timeout=5)
        except Exception:
            pass
        try:
            # 打开网络监听，等下从真实请求里截 Authorization
            cdp.call("Network.enable", {}, timeout=8)
        except Exception:
            pass
        return cdp

    def _loop(self, s, primary, primary_url=""):
        """同时盯住所有页面目标——扫完码登录可能发生在另一个标签/窗口里。"""
        tried = set()
        pages = {}                       # targetId -> CDP
        last_msg = ""
        s.targets = []
        if primary_url:
            tid0 = primary_url.rsplit("/", 1)[-1]
            pages[tid0] = primary
            try:
                primary.call("Network.enable", {}, timeout=6)
            except Exception:
                pass
        uid = ""
        nick = ""
        while time.time() - s.started_at < SESSION_TTL:
            if s.state == "cancelled":
                return
            if s.proc and s.proc.poll() is not None:
                s.state = "error"
                s.message = "浏览器窗口被关闭了，登录没完成"
                return

            targets = [t for t in self._targets(s.port)
                       if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            alive = {t["id"] for t in targets}
            for tid in list(pages):
                if tid not in alive:      # 标签关了，顺手断开
                    try:
                        pages[tid].ws.close()
                    except Exception:
                        pass
                    pages.pop(tid, None)

            s.targets = [t.get("url", "") for t in targets][:6]
            on_login = False
            candidates = []

            for t in targets:
                url = t.get("url") or ""
                if "workbuddy.cn" not in url:
                    continue
                if "/login" in url:
                    on_login = True
                tid = t["id"]
                cdp = pages.get(tid)
                if cdp is None:
                    try:
                        cdp = self._attach(s.port, t["webSocketDebuggerUrl"])
                        pages[tid] = cdp
                    except Exception:
                        continue

                # ① 抓包：从真实请求头里拿 Bearer，最可靠
                cdp.pump()
                for ev in cdp.drain():
                    if ev.get("method") != "Network.requestWillBeSent":
                        continue
                    req = (ev.get("params") or {}).get("request") or {}
                    auth = ""
                    for k, v in (req.get("headers") or {}).items():
                        if k.lower() == "authorization":
                            auth = v
                            break
                    if auth:
                        tok = auth[7:].strip() if auth.lower().startswith("bearer ") else auth.strip()
                        if len(tok) > 20 and tok not in candidates:
                            candidates.append(tok)

                # ② 兜底：翻页面存储（Bearer 部署下令牌会在这里）
                try:
                    store = json.loads(cdp.eval(STORAGE_JS, timeout=6) or "{}")
                except Exception:
                    store = {}
                for val in store.values():
                    if isinstance(val, str):
                        for m in JWT_RE.findall(val):
                            if m not in candidates:
                                candidates.append(m)
                uid = self._guess_uid(store)

                # ③ 网页版真正用的是 httpOnly Cookie（Bearer 根本没有），
                #    而且会话跟 UA 绑定，必须连 UA 一起带出去。
                #    登录前也存在一个短会话 Cookie，用它区分：真会话光是
                #    session 就有 4KB，或者页面上已经有 lamp_last_login 标记。
                if "/login" not in url:
                    try:
                        ck = cdp.call("Network.getCookies", {"urls": [
                            "https://www.workbuddy.cn", "https://workbuddy.cn"]}, timeout=6)
                        cookies = ck.get("cookies") or []
                        names = {c["name"] for c in cookies}
                        jar = "; ".join("%s=%s" % (c["name"], c["value"]) for c in cookies)
                        # 只认「登录后」的会话：登录动作会在页面写入 lamp_last_login，
                        # 登录前 /agents 也会下发一个未认证的 session，靠这个标记排掉。
                        # 注意 STORAGE_JS 里的键带 ls:/ss: 前缀
                        logged_in = any(k.endswith("lamp_last_login") for k in store)
                        if (names & {"session", "session_2"}) and logged_in:
                            ua = cdp.eval("navigator.userAgent", timeout=4) or ""
                            cred = "CK1:" + json.dumps(
                                {"ua": ua, "cookie": jar,
                                 "referer": "https://www.workbuddy.cn/agents"},
                                ensure_ascii=False)
                            if cred not in candidates:
                                candidates.append(cred)
                            if not nick:
                                try:
                                    nick = (cdp.eval(NICK_JS, timeout=4) or "").strip()[:24]
                                except Exception:
                                    pass
                    except Exception:
                        pass

                # ④ 二维码只在登录页那个标签上截
                if "/login" in url and time.time() - s.qr_at > QR_INTERVAL:
                    try:
                        rect = cdp.eval(QR_RECT_JS, timeout=6)
                        if rect:
                            d = json.loads(rect)
                            side = min(246, max(180, d["w"] - 10))
                            x = d["x"] + (d["w"] - side) / 2
                            y = d["y"] + 3
                            png = cdp.screenshot_clip(x, y, side, min(side - 22, d["h"] - 22))
                            if png and len(png) > 4000:
                                s.qr = png
                                s.qr_at = time.time()
                    except Exception:
                        pass

            fresh = [c for c in candidates if c not in tried]
            if fresh:
                s.message = "已登录，正在校验令牌…"
                err = ""
                for tok in fresh:
                    tried.add(tok)
                    res = self.on_token(tok, uid, s.hint or nick, s.brand)
                    if res and res.get("ok"):
                        s.state = "ok"
                        s.account = res
                        s.message = "登录成功，账号已加入列表"
                        s.qr = None
                        for c in pages.values():
                            try:
                                c.ws.close()
                            except Exception:
                                pass
                        self._kill(s)
                        return
                    err = (res or {}).get("error") or ""
                s.message = "抓到了令牌但校验没过：%s" % (err or "上游拒绝")
            else:
                if on_login:
                    if not s.qr:
                        msg = "正在加载二维码…"
                    elif s.headless:
                        msg = "请用微信扫描二维码"
                    else:
                        msg = "请在弹出的窗口里扫码，或点「手机号」登录"
                else:
                    msg = "登录中，等待页面跳转…"
                if msg != last_msg:
                    s.message = msg
                    last_msg = msg
            time.sleep(POLL_INTERVAL)

        if s.state == "waiting":
            s.state = "error"
            s.message = "登录超时（5 分钟），已关闭浏览器"

    @staticmethod
    def _guess_uid(store):
        for k, v in store.items():
            if isinstance(v, str) and len(v) < 400 and re.search(r"(uid|uin|user_?id)$", k, re.I):
                if re.fullmatch(r"\d{5,12}", v.strip()):
                    return v.strip()
        for v in store.values():
            if isinstance(v, str) and v.startswith("{"):
                try:
                    d = json.loads(v)
                except Exception:
                    continue
                if isinstance(d, dict):
                    for kk in ("uid", "uin", "userId", "user_id"):
                        val = d.get(kk)
                        if isinstance(val, (str, int)) and re.fullmatch(r"\d{5,12}", str(val)):
                            return str(val)
        return ""

    @staticmethod
    def _kill(s):
        if not s.proc:
            return
        try:
            if s.proc.poll() is None:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(s.proc.pid)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=0x08000000,
                    )
                else:
                    s.proc.terminate()
        except Exception:
            pass

    @staticmethod
    def _drop_profile(s):
        if not s.profile:
            return

        def gone():
            for _ in range(8):
                shutil.rmtree(s.profile, ignore_errors=True)
                if not os.path.exists(s.profile):
                    return
                time.sleep(0.7)

        threading.Thread(target=gone, daemon=True).start()
