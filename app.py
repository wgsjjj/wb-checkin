#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 多账号签到助手

纯标准库实现（可选 cryptography 用于加密令牌），提供：
  - 多账号管理（令牌加密存储）
  - 单账号/批量签到，签到状态与积分查询
  - 定时自动签到（每天多个时间点，幂等重试）
  - 浏览器控制台

运行：
  python app.py
  WB_PORT=8787 WB_HOST=0.0.0.0 WB_PASSWORD=xxx python app.py
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------- 基础配置

# 打包成 exe 后：静态资源在 PyInstaller 的临时解压目录，数据要放 exe 旁边才便携
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RES_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RES_DIR = BASE_DIR

DATA_DIR = os.environ.get("WB_DATA_DIR") or os.path.join(BASE_DIR, "data")
STATIC_DIR = os.path.join(RES_DIR, "static")
DB_PATH = os.path.join(DATA_DIR, "wb.db")

HOST = os.environ.get("WB_HOST", "127.0.0.1")
PORT = int(os.environ.get("WB_PORT", "8787"))
PASSWORD = os.environ.get("WB_PASSWORD", "").strip()
SECRET = os.environ.get("WB_SECRET", "").strip()
NO_BROWSER = os.environ.get("WB_NO_BROWSER") == "1"

DEFAULT_DOMAIN = os.environ.get("WB_DOMAIN", "www.workbuddy.cn")
DEFAULT_TIMES = "08:30,12:00,15:00,18:00,21:30"

API_VERSION = "1.3.0"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) WorkBuddy/5.9.3 Chrome/136.0.0.0 Electron/36.0.0 Safari/537.36"
)

os.makedirs(DATA_DIR, exist_ok=True)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(*args):
    print(time.strftime("[%m-%d %H:%M:%S]"), *args, flush=True)


# ---------------------------------------------------------------- 加密存储

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    HAS_AES = True
except Exception:
    AESGCM = None
    HAS_AES = False


def _aes_key():
    if SECRET:
        return hashlib.sha256(SECRET.encode("utf-8")).digest()
    path = os.path.join(DATA_DIR, ".secret")
    if os.path.exists(path):
        with open(path, "rb") as f:
            key = f.read()
        if len(key) == 32:
            return key
    key = secrets.token_bytes(32)
    with open(path, "wb") as f:
        f.write(key)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return key


_AES_KEY = _aes_key() if HAS_AES else None


def _dpapi(data, protect=True):
    """Windows DPAPI，仅在缺少 cryptography 时使用。"""
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI 调用失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def seal(text):
    """把明文令牌封成可入库的字节串。"""
    data = text.encode("utf-8")
    if HAS_AES:
        nonce = secrets.token_bytes(12)
        return b"AES1:" + nonce + AESGCM(_AES_KEY).encrypt(nonce, data, None)
    if os.name == "nt":
        try:
            return b"DPAPI1:" + _dpapi(data, True)
        except Exception:
            pass
    return b"PLAIN1:" + base64.b64encode(data)


def unseal(raw):
    if raw is None:
        return ""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if raw.startswith(b"AES1:"):
        body = raw[5:]
        return AESGCM(_AES_KEY).decrypt(body[:12], body[12:], None).decode("utf-8")
    if raw.startswith(b"DPAPI1:"):
        return _dpapi(raw[7:], False).decode("utf-8")
    if raw.startswith(b"PLAIN1:"):
        return base64.b64decode(raw[7:]).decode("utf-8")
    return raw.decode("utf-8", "replace")


# ---------------------------------------------------------------- 数据层

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    domain TEXT NOT NULL,
    token BLOB NOT NULL,
    user_id TEXT DEFAULT '',
    note TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT,
    last_status_date TEXT DEFAULT '',
    last_checked_in INTEGER DEFAULT 0,
    last_streak INTEGER DEFAULT 0,
    last_credit REAL DEFAULT 0,          -- 账户真实积分余额（如 69.23）
    last_earned REAL DEFAULT 0,          -- 本次签到活动累计获得
    last_msg TEXT DEFAULT '',
    last_ts TEXT DEFAULT '',
    token_state TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    account_name TEXT,
    ts TEXT,
    action TEXT,
    state TEXT,
    ok INTEGER,
    credit INTEGER DEFAULT 0,
    streak INTEGER DEFAULT 0,
    message TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    day TEXT,
    slot TEXT,
    ts TEXT,
    summary TEXT,
    PRIMARY KEY (day, slot)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=20000")
    return conn


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)
        # 老库补列（SQLite 没有 IF NOT EXISTS 的 ADD COLUMN，只能查一下）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
        if "last_earned" not in cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN last_earned REAL DEFAULT 0")
        for k, v in (("auto", "1"), ("times", DEFAULT_TIMES), ("last_auto", "")):
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))


def get_setting(key, default=""):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def add_log(account, action, state, ok, credit=0, streak=0, message=""):
    with db() as conn:
        conn.execute(
            "INSERT INTO logs(account_id, account_name, ts, action, state, ok, credit, streak, message)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (
                account["id"] if account else 0,
                account["name"] if account else "-",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                action,
                state,
                1 if ok else 0,
                credit,
                streak,
                message[:500],
            ),
        )
        conn.execute(
            "DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 1200)"
        )


def update_account_status(acc_id, **fields):
    if not fields:
        return
    allowed = {
        "last_status_date",
        "last_checked_in",
        "last_streak",
        "last_credit",
        "last_earned",
        "last_msg",
        "last_ts",
        "token_state",
        "name",
        "note",
        "enabled",
        "user_id",
        "domain",
        "token",
        "updated_at",
    }
    keys = [k for k in fields if k in allowed]
    if not keys:
        return
    sql = "UPDATE accounts SET " + ", ".join(k + "=?" for k in keys) + " WHERE id=?"
    with db() as conn:
        conn.execute(sql, [fields[k] for k in keys] + [acc_id])


# ---------------------------------------------------------------- 上游接口

BASE_PATH = "/v2/billing/meter"


def safe_json(raw):
    try:
        return json.loads(raw)
    except Exception:
        return None


def brief(payload, raw, limit=160):
    if isinstance(payload, dict):
        msg = payload.get("msg") or payload.get("message") or payload.get("error")
        if msg:
            return str(msg)[:limit]
        return json.dumps(payload, ensure_ascii=False)[:limit]
    text = (raw or "").strip()
    low = text.lower()
    if low.startswith("<") or "<html" in low:
        # 网关返回的 HTML 错误页，只保留标题，避免把整页塞进日志
        import re

        m = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
        return (m.group(1).strip() if m else "网关返回了 HTML 错误页")[:limit]
    return text[:limit]


def pick_balance(data, depth=0):
    if not isinstance(data, dict) or depth > 3:
        return None
    for k in (
        "total_credits",
        "totalCredit",
        "total_credit",
        "balance",
        "credits",
        "available_credits",
        "remain_credits",
        "credit_balance",
    ):
        v = data.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str) and v.strip().lstrip("-").isdigit():
            return int(v.strip())
    for v in data.values():
        if isinstance(v, dict):
            r = pick_balance(v, depth + 1)
            if r is not None:
                return r
    return None


def wb_request(account, path, body=None, timeout=25):
    token = unseal(account["token"])
    domain = (account["domain"] or DEFAULT_DOMAIN).strip().rstrip("/")
    url = "https://" + domain + path
    data = json.dumps(body if body is not None else {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/plain, */*")
    req.add_header("Origin", "https://" + domain)
    req.add_header("Referer", "https://" + domain + "/")

    # 网页版登录态是 Cookie（httpOnly），不是 Bearer；UA 必须和浏览器一致，
    # 网关会拿 UA 校验会话，换成别的 UA 直接 401
    if token.startswith("CK1:"):
        try:
            cred = json.loads(token[4:])
        except Exception:
            cred = {}
        req.add_header("Cookie", cred.get("cookie") or "")
        req.add_header("User-Agent", cred.get("ua") or UA)
        if cred.get("referer"):
            req.add_header("Referer", cred["referer"])
    else:
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("User-Agent", UA)
        uid = (account["user_id"] or "").strip()
        if uid:
            req.add_header("X-User-Id", uid)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except Exception as e:
        return 0, json.dumps({"error": str(e)}, ensure_ascii=False)


# 真实积分余额在这个接口上（注意：它没有 /v2 前缀）
BALANCE_PATH = "/billing/meter/get-user-resource-summary"


def fetch_balance(account):
    """账户真实余额。签到接口里的 total_credits 是「本次活动累计获得」，不是余额。"""
    code, raw = wb_request(account, BALANCE_PATH, {})
    if code != 200:
        return None
    payload = safe_json(raw)
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return None
    d = payload.get("data") or {}
    if not isinstance(d, dict):
        return None
    try:
        return round(float(d.get("TotalCredits")), 2)
    except (TypeError, ValueError):
        return None


def fetch_status(account):
    code, raw = wb_request(account, BASE_PATH + "/checkin-activity-status", {})
    payload = safe_json(raw)
    if code == 200 and isinstance(payload, dict) and payload.get("code") == 0:
        d = payload.get("data") or {}
        if not isinstance(d, dict):
            d = {}
        try:
            earned = round(float(d.get("total_credits")), 2)
        except (TypeError, ValueError):
            earned = None
        return {
            "ok": True,
            "state": "status",
            "today": bool(d.get("today_checked_in")),
            "streak": int(d.get("streak_days") or 0),
            "balance": fetch_balance(account),      # 真实余额
            "earned": earned,                        # 活动累计获得
            "daily": d.get("daily_credit"),
            "message": "",
            "raw": raw[:600],
        }
    if code in (401, 403):
        return {
            "ok": False,
            "state": "invalid",
            "message": "令牌无效或已过期，请更新 token",
            "raw": raw[:400],
        }
    return {
        "ok": False,
        "state": "error",
        "message": "HTTP %s %s" % (code, brief(payload, raw)),
        "raw": raw[:400],
    }


def claim(account):
    code, raw = wb_request(account, BASE_PATH + "/daily-checkin", {})
    payload = safe_json(raw)
    text = raw or ""
    if code == 200 and isinstance(payload, dict) and payload.get("code") == 0:
        d = payload.get("data")
        if not isinstance(d, dict):
            d = payload if isinstance(payload, dict) else {}
        credit = d.get("credit") or d.get("daily_credit") or d.get("credits") or 0
        try:
            credit = int(credit)
        except Exception:
            credit = 0
        return {
            "ok": True,
            "state": "success",
            "credit": credit,
            "streak": int(d.get("streak_days") or 0),
            "message": "签到成功" + ("，+%d 积分" % credit if credit else ""),
        }
    if "已签到" in text or (isinstance(payload, dict) and payload.get("code") == 10001):
        return {"ok": True, "state": "already", "credit": 0, "streak": 0, "message": "今天已签到"}
    if code in (401, 403):
        return {
            "ok": False,
            "state": "invalid",
            "message": "令牌无效或已过期，请更新 token",
        }
    return {
        "ok": False,
        "state": "error",
        "message": "HTTP %s %s" % (code, brief(payload, raw)),
    }


def checkin_one(acc_id, trigger="manual"):
    """完整签到流程：查状态 -> 领取 -> 回写状态。"""
    with db() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
    if not row:
        return {"id": acc_id, "ok": False, "state": "error", "message": "账号不存在"}

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    ts = now.strftime("%Y-%m-%d %H:%M:%S")

    st = fetch_status(row)
    if not st.get("ok"):
        update_account_status(
            acc_id,
            last_msg=st.get("message", ""),
            last_ts=ts,
            token_state="invalid" if st.get("state") == "invalid" else "error",
        )
        add_log(row, trigger, st.get("state", "error"), False, 0, 0, st.get("message", ""))
        return {"id": acc_id, "name": row["name"], "ok": False, "state": st.get("state"), "message": st.get("message")}

    if st["today"]:
        update_account_status(
            acc_id,
            last_status_date=today,
            last_checked_in=1,
            last_streak=st["streak"],
            last_credit=st["balance"] if st["balance"] is not None else row["last_credit"],
            last_earned=st.get("earned") if st.get("earned") is not None else row["last_earned"],
            last_msg="今天已签到",
            last_ts=ts,
            token_state="ok",
        )
        add_log(row, trigger, "already", True, 0, st["streak"], "今天已签到")
        return {
            "id": acc_id,
            "name": row["name"],
            "ok": True,
            "state": "already",
            "streak": st["streak"],
            "balance": st["balance"],
            "message": "今天已签到",
        }

    res = claim(row)
    if res["ok"]:
        st2 = fetch_status(row)
        streak = st2.get("streak") if st2.get("ok") else res.get("streak", 0)
        balance = st2.get("balance") if st2.get("ok") else None
        update_account_status(
            acc_id,
            last_status_date=today,
            last_checked_in=1,
            last_streak=streak or 0,
            last_credit=balance if balance is not None else (row["last_credit"] or 0),
            last_earned=st2.get("earned") if st2.get("earned") is not None else (row["last_earned"] or 0),
            last_msg=res["message"],
            last_ts=ts,
            token_state="ok",
        )
        add_log(row, trigger, res["state"], True, res.get("credit", 0), streak or 0, res["message"])
        return {
            "id": acc_id,
            "name": row["name"],
            "ok": True,
            "state": res["state"],
            "credit": res.get("credit", 0),
            "streak": streak or 0,
            "balance": balance,
            "message": res["message"],
        }

    update_account_status(
        acc_id,
        last_msg=res["message"],
        last_ts=ts,
        token_state="invalid" if res.get("state") == "invalid" else "error",
    )
    add_log(row, trigger, res.get("state", "error"), False, 0, 0, res["message"])
    return {"id": acc_id, "name": row["name"], "ok": False, "state": res.get("state"), "message": res["message"]}


_run_lock = threading.Lock()


def run_batch(ids=None, trigger="manual"):
    with db() as conn:
        if ids:
            q = "SELECT id, name FROM accounts WHERE id IN (%s)" % ",".join("?" * len(ids))
            rows = conn.execute(q, list(ids)).fetchall()
        else:
            rows = conn.execute("SELECT id, name FROM accounts WHERE enabled=1 ORDER BY id").fetchall()

    if not rows:
        return {"ok": True, "results": [], "summary": "没有可执行的账号"}

    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "busy": True, "results": [], "summary": "上一轮还在执行，请稍候"}

    try:
        results = []
        with ThreadPoolExecutor(max_workers=min(5, len(rows))) as pool:
            futures = [pool.submit(checkin_one, r["id"], trigger) for r in rows]
            for f in futures:
                try:
                    results.append(f.result())
                except Exception as e:
                    results.append({"ok": False, "state": "error", "message": str(e)})
        success = sum(1 for r in results if r.get("ok"))
        gained = sum(r.get("credit", 0) or 0 for r in results)
        summary = "成功 %d / %d，本轮新增 %d 积分" % (success, len(results), gained)
        log("[%s] %s" % (trigger, summary))
        return {"ok": True, "results": results, "summary": summary}
    finally:
        _run_lock.release()


# ---------------------------------------------------------------- 定时调度


def parse_times(text):
    out = []
    for part in (text or "").replace("，", ",").split(","):
        part = part.strip()
        if ":" not in part:
            continue
        hh, mm = part.split(":", 1)
        hh, mm = hh.strip(), mm.strip()
        if not (hh.isdigit() and mm.isdigit()):
            continue
        h, m = int(hh), int(mm)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            continue
        out.append("%02d:%02d" % (h, m))
    return sorted(set(out))


def scheduler():
    last_minute = ""
    while True:
        try:
            now = datetime.now()
            if now.strftime("%H:%M") != last_minute:
                last_minute = now.strftime("%H:%M")
                if get_setting("auto", "1") == "1":
                    times = parse_times(get_setting("times", DEFAULT_TIMES))
                    slot = now.strftime("%H:%M")
                    if slot in times:
                        day = now.strftime("%Y-%m-%d")
                        with db() as conn:
                            done = conn.execute(
                                "SELECT 1 FROM runs WHERE day=? AND slot=?", (day, slot)
                            ).fetchone()
                        if not done:
                            with db() as conn:
                                conn.execute(
                                    "INSERT OR REPLACE INTO runs(day, slot, ts, summary) VALUES(?,?,?,?)",
                                    (day, slot, now.strftime("%Y-%m-%d %H:%M:%S"), ""),
                                )
                            set_setting("last_auto", now.strftime("%Y-%m-%d %H:%M:%S"))
                            log("定时任务触发 slot=%s" % slot)
                            res = run_batch(trigger="auto")
                            with db() as conn:
                                conn.execute(
                                    "UPDATE runs SET summary=? WHERE day=? AND slot=?",
                                    (res.get("summary", ""), day, slot),
                                )
        except Exception:
            log("调度异常:\n" + traceback.format_exc())
        time.sleep(10)


# ---------------------------------------------------------------- HTTP 服务


def mask_token(token):
    if not token:
        return ""
    if token.startswith("CK1:"):
        return "网页 Cookie 登录态"
    if len(token) <= 12:
        return token[:2] + "****"
    return token[:6] + "…" + token[-4:]


def account_public(row):
    today = datetime.now().strftime("%Y-%m-%d")
    fresh = row["last_status_date"] == today
    return {
        "id": row["id"],
        "name": row["name"],
        "domain": row["domain"],
        "user_id": row["user_id"],
        "note": row["note"],
        "enabled": bool(row["enabled"]),
        "today": bool(row["last_checked_in"]) if fresh else False,
        "streak": row["last_streak"] or 0,
        "credit": row["last_credit"] or 0,
        "earned": row["last_earned"] or 0,
        "last_msg": row["last_msg"] or "",
        "last_ts": row["last_ts"] or "",
        "token_state": row["token_state"] or "",
        "token_masked": "",
    }


def build_overview():
    with db() as conn:
        rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        logs = conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 60").fetchall()
        runs = conn.execute("SELECT * FROM runs ORDER BY day DESC, slot DESC LIMIT 10").fetchall()

    accounts = [account_public(r) for r in rows]
    today = datetime.now().strftime("%Y-%m-%d")
    stats = {
        "total": len(accounts),
        "enabled": sum(1 for a in accounts if a["enabled"]),
        "done": sum(1 for a in accounts if a["today"]),
        "credit": sum(a["credit"] for a in accounts),
        "streak": max([a["streak"] for a in accounts] + [0]),
        "invalid": sum(1 for a in accounts if a["token_state"] == "invalid"),
    }
    return {
        "accounts": accounts,
        "stats": stats,
        "logs": [
            {
                "id": l["id"],
                "name": l["account_name"],
                "ts": l["ts"],
                "action": l["action"],
                "state": l["state"],
                "ok": bool(l["ok"]),
                "credit": l["credit"],
                "streak": l["streak"],
                "message": l["message"],
            }
            for l in logs
        ],
        "settings": {
            "auto": get_setting("auto", "1") == "1",
            "times": get_setting("times", DEFAULT_TIMES),
            "last_auto": get_setting("last_auto", ""),
        },
        "runs": [
            {"day": r["day"], "slot": r["slot"], "summary": r["summary"] or ""} for r in runs
        ],
        "server": {
            "version": API_VERSION,
            "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "today": today,
            "tz": time.strftime("%Z"),
            "crypto": "AES-GCM" if HAS_AES else ("DPAPI" if os.name == "nt" else "PLAIN"),
        },
    }


def extract_account_from_json(text):
    """尽量从粘贴的 JSON 里提取 accessToken / uid / domain。"""
    obj = safe_json(text)
    if not isinstance(obj, dict):
        return None
    token, uid, domain = "", "", ""
    auth = obj.get("auth") if isinstance(obj.get("auth"), dict) else obj
    for key in ("accessToken", "access_token", "token"):
        v = auth.get(key)
        if isinstance(v, str) and v:
            token = v
            break
        if isinstance(v, dict) and isinstance(v.get("envelope"), str):
            # 加密信封，无法解开
            token = ""
    account = obj.get("account") if isinstance(obj.get("account"), dict) else {}
    uid = str(account.get("uid") or obj.get("uid") or obj.get("user_id") or "")
    domain = str(auth.get("domain") or obj.get("domain") or "")
    if not token:
        return None
    return {"token": token.strip(), "user_id": uid, "domain": domain or DEFAULT_DOMAIN}


def create_account(payload):
    """新建账号并立即探测一次状态。手机扫码提交与电脑表单共用。"""
    name = str(payload.get("name") or "").strip()
    token = str(payload.get("token") or "").strip()
    domain = str(payload.get("domain") or DEFAULT_DOMAIN).strip() or DEFAULT_DOMAIN
    uid = str(payload.get("user_id") or "").strip()
    note = str(payload.get("note") or "").strip()

    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if token.startswith("{"):
        parsed = extract_account_from_json(token)
        if parsed:
            token = parsed["token"]
            uid = uid or parsed["user_id"]
            domain = domain if payload.get("domain") else parsed["domain"]
    if not name:
        name = uid or ("账号 " + datetime.now().strftime("%m%d%H%M"))
    if len(token) < 16:
        return {"ok": False, "error": "请填写有效的 token"}

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO accounts(name, domain, token, user_id, note, enabled, created_at, updated_at)"
            " VALUES(?,?,?,?,?,1,?,?)",
            (name, domain, seal(token), uid, note, now, now),
        )
        acc_id = cur.lastrowid

    with db() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
    st = fetch_status(row)
    warn = ""
    if st.get("ok"):
        today = datetime.now().strftime("%Y-%m-%d")
        update_account_status(
            acc_id,
            last_status_date=today,
            last_checked_in=1 if st["today"] else 0,
            last_streak=st["streak"],
            last_credit=st["balance"] if st["balance"] is not None else 0,
            last_earned=st.get("earned") if st.get("earned") is not None else 0,
            last_msg="今天已签到" if st["today"] else "待签到",
            last_ts=now,
            token_state="ok",
            updated_at=now,
        )
        if st["today"]:
            warn = "该账号今天已经签到过了"
    else:
        warn = st.get("message", "")
        update_account_status(acc_id, token_state="invalid", last_msg=warn, last_ts=now)

    return {"ok": True, "id": acc_id, "name": name, "warn": warn, "status": st}


# ---------------------------------------------------------------- 官方登录

try:
    from browser_login import BrowserLogin

    BROWSER_AVAILABLE = True
except Exception:                                    # 模块缺失时静默降级
    BrowserLogin = None
    BROWSER_AVAILABLE = False


def _official_login_token(token, uid, hint, brand):
    """浏览器登录读到的令牌 → 建账号；令牌不通就把这条脏数据撤掉。"""
    res = create_account({
        "token": token,
        "user_id": uid,
        "name": hint or "",
        "note": "官方登录·%s" % (brand or "浏览器"),
    })
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "添加失败"}
    if "令牌" in (res.get("warn") or ""):
        with db() as conn:
            conn.execute("DELETE FROM accounts WHERE id=?", (res["id"],))
        st = res.get("status") or {}
        detail = (st.get("message") or res.get("warn") or "").strip()
        add_log({"id": 0, "name": "-"}, "官方登录", "invalid", False, message=detail[:200])
        return {"ok": False, "error": detail[:120] or "令牌校验没通过"}
    add_log(
        {"id": res["id"], "name": res.get("name") or "-"},
        "官方登录", "ok", True,
        message="通过%s登录自动添加" % (brand or "浏览器"),
    )
    return {"ok": True, "id": res["id"], "name": res.get("name"), "warn": res.get("warn", "")}


class _NoBrowser:
    def available(self):
        return False

    def start(self, hint="", headless=True):
        return {"ok": False, "error": "官方登录模块不可用"}

    def status(self, sid):
        return {"ok": False, "state": "expired", "message": "不可用"}

    def qr(self, sid):
        return None

    def cancel(self, sid):
        return {"ok": True}

    def cleanup(self):
        pass


BROWSER = BrowserLogin(DATA_DIR, _official_login_token, log) if BROWSER_AVAILABLE else _NoBrowser()


def browser_cleanup_loop():
    while True:
        time.sleep(20)
        try:
            BROWSER.cleanup()
        except Exception:
            pass


# ---------------------------------------------------------------- 扫码添加

_QR_TICKETS = {}
_QR_LOCK = threading.Lock()
QR_TTL = 600


def qr_create():
    now = time.time()
    ticket = secrets.token_urlsafe(18)
    with _QR_LOCK:
        for k in [k for k, v in _QR_TICKETS.items() if v["exp"] < now]:
            _QR_TICKETS.pop(k, None)
        _QR_TICKETS[ticket] = {"exp": now + QR_TTL, "state": "pending", "account": None}
    return {"ticket": ticket, "ttl": QR_TTL}


def qr_status(ticket):
    now = time.time()
    with _QR_LOCK:
        item = _QR_TICKETS.get(ticket)
        if not item or item["exp"] < now:
            return {"state": "expired"}
        if item["state"] == "done":
            return {"state": "done", "account": item["account"]}
        return {"state": "pending", "left": int(item["exp"] - now)}


def qr_claim(ticket):
    """置为提交中，保证一个二维码只能成功提交一次。"""
    now = time.time()
    with _QR_LOCK:
        item = _QR_TICKETS.get(ticket)
        if not item or item["exp"] < now or item["state"] != "pending":
            return False
        item["state"] = "committing"
        return True


def qr_finish(ticket, account):
    with _QR_LOCK:
        item = _QR_TICKETS.get(ticket)
        if item:
            item["state"] = "done"
            item["account"] = account


def qr_release(ticket):
    with _QR_LOCK:
        item = _QR_TICKETS.get(ticket)
        if item and item["state"] == "committing":
            item["state"] = "pending"


def local_ips():
    found = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.4)
        s.connect(("223.5.5.5", 80))
        found.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except Exception:
        pass

    out = []
    for ip in found:
        if not ip or ip.startswith("127.") or ip.startswith("169.254."):
            continue
        if ip.startswith("172.17.") or ip.startswith("172.18."):
            continue  # docker 默认网段，手机连不上
        if ip not in out:
            out.append(ip)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "WBCheckin/" + API_VERSION
    protocol_version = "HTTP/1.1"

    # ---- 工具

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False), extra=extra)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _authed(self):
        if not PASSWORD:
            return True
        want = hashlib.sha256(("wb-checkin::" + PASSWORD).encode()).hexdigest()
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "wb_auth" and hmac.compare_digest(v, want):
                return True
        return False

    def _path(self):
        return urlparse(self.path).path

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    # ---- 静态文件

    def _serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        if path in ("/m", "/m/"):
            path = "/m.html"
        rel = path.lstrip("/")
        if ".." in rel or rel.startswith("/"):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        full = os.path.join(STATIC_DIR, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        ext = os.path.splitext(full)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    # ---- GET

    def do_GET(self):
        path = self._path()
        if not path.startswith("/api/"):
            return self._serve_static(path)

        if path == "/api/me":
            return self._json({"needLogin": bool(PASSWORD), "authed": self._authed(), "version": API_VERSION})

        if not self._authed():
            return self._json({"error": "unauthorized"}, 401)

        if path == "/api/overview":
            return self._json(build_overview())

        if path == "/api/net":
            return self._json({"ips": local_ips(), "port": self.server.server_address[1]})

        if path == "/api/qr/new":
            return self._json(qr_create())

        if path == "/api/qr/status":
            ticket = (self._query().get("t") or [""])[0]
            return self._json(qr_status(ticket))

        if path == "/api/browser/available":
            return self._json({"ok": True, "available": BROWSER_AVAILABLE})

        if path == "/api/browser/status":
            sid = (self._query().get("sid") or [""])[0]
            return self._json(BROWSER.status(sid))

        if path == "/api/browser/qr":
            sid = (self._query().get("sid") or [""])[0]
            png = BROWSER.qr(sid)
            if not png:
                return self._send(404, b"no qr", "text/plain; charset=utf-8")
            return self._send(
                200, png, "image/png",
                extra=[("Cache-Control", "no-store, max-age=0")],
            )

        if path == "/api/logs":
            limit = 100
            try:
                limit = min(500, int((self._query().get("limit") or ["100"])[0]))
            except Exception:
                pass
            with db() as conn:
                rows = conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return self._json(
                {
                    "logs": [
                        {k: r[k] for k in r.keys()} for r in rows
                    ]
                }
            )

        return self._json({"error": "not found"}, 404)

    # ---- POST

    def do_POST(self):
        path = self._path()
        body = self._body()

        if path == "/api/login":
            if not PASSWORD:
                return self._json({"ok": True})
            given = str(body.get("password") or "")
            if hmac.compare_digest(given, PASSWORD):
                want = hashlib.sha256(("wb-checkin::" + PASSWORD).encode()).hexdigest()
                return self._json(
                    {"ok": True},
                    extra=[("Set-Cookie", "wb_auth=%s; Path=/; HttpOnly; Max-Age=2592000" % want)],
                )
            return self._json({"ok": False, "error": "密码不正确"}, 401)

        if path == "/api/logout":
            return self._json({"ok": True}, extra=[("Set-Cookie", "wb_auth=; Path=/; Max-Age=0")])

        # 手机扫码提交：用一次性 ticket 鉴权，不需要登录密码
        if path == "/api/m/add":
            ticket = str(body.get("ticket") or "")
            if not qr_claim(ticket):
                return self._json({"ok": False, "error": "二维码已失效，请在电脑上重新生成"}, 400)
            res = create_account(body)
            if res.get("ok"):
                qr_finish(
                    ticket,
                    {"id": res["id"], "name": res.get("name"), "warn": res.get("warn", "")},
                )
                return self._json({"ok": True, "name": res.get("name"), "warn": res.get("warn", "")})
            qr_release(ticket)
            return self._json(res, 400)

        if not self._authed():
            return self._json({"error": "unauthorized"}, 401)

        if path == "/api/parse":
            parsed = extract_account_from_json(str(body.get("text") or ""))
            if not parsed:
                return self._json({"ok": False, "error": "没解析到可用的 accessToken（可能是加密信封）"})
            return self._json({"ok": True, "data": parsed})

        if path == "/api/accounts":
            res = create_account(body)
            return self._json(res, 200 if res.get("ok") else 400)

        # ---- 官方登录（开浏览器扫码 / 手机号）
        if path == "/api/browser/start":
            headless = body.get("headless")
            headless = True if headless is None else bool(headless)
            return self._json(BROWSER.start(str(body.get("hint") or ""), headless=headless))

        if path == "/api/browser/cancel":
            return self._json(BROWSER.cancel(str(body.get("sid") or "")))

        if path == "/api/checkin":
            ids = body.get("ids") or None
            res = run_batch(ids=ids, trigger="manual")
            return self._json(res)

        if path == "/api/settings":
            if "auto" in body:
                set_setting("auto", "1" if body["auto"] else "0")
            if "times" in body:
                times = parse_times(str(body["times"]))
                if not times:
                    return self._json({"ok": False, "error": "时间格式应为 08:30,12:00"}, 400)
                set_setting("times", ",".join(times))
            return self._json({"ok": True})

        # 单账号动作：/api/accounts/<id>/checkin 或 /test
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "accounts":
            try:
                acc_id = int(parts[2])
            except ValueError:
                return self._json({"error": "bad id"}, 400)
            action = parts[3]
            if action == "checkin":
                with db() as conn:
                    row = conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
                if not row:
                    return self._json({"error": "账号不存在"}, 404)
                return self._json({"ok": True, "results": [checkin_one(acc_id, "manual")], "summary": "已完成"})
            if action == "test":
                with db() as conn:
                    row = conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
                if not row:
                    return self._json({"error": "账号不存在"}, 404)
                st = fetch_status(row)
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if st.get("ok"):
                    update_account_status(
                        acc_id,
                        last_status_date=datetime.now().strftime("%Y-%m-%d"),
                        last_checked_in=1 if st["today"] else 0,
                        last_streak=st["streak"],
                        last_credit=st["balance"] if st["balance"] is not None else row["last_credit"],
            last_earned=st.get("earned") if st.get("earned") is not None else row["last_earned"],
                        last_msg="今天已签到" if st["today"] else "待签到",
                        last_ts=now,
                        token_state="ok",
                    )
                    add_log(row, "test", "status", True, 0, st["streak"], "连通正常")
                else:
                    update_account_status(
                        acc_id,
                        last_msg=st.get("message", ""),
                        last_ts=now,
                        token_state="invalid" if st.get("state") == "invalid" else "error",
                    )
                    add_log(row, "test", st.get("state", "error"), False, 0, 0, st.get("message", ""))
                return self._json({"ok": st.get("ok", False), "status": st})

        return self._json({"error": "not found"}, 404)

    # ---- PUT / DELETE

    def do_PUT(self):
        if not self._authed_if_api():
            return
        path = self._path()
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "accounts":
            try:
                acc_id = int(parts[2])
            except ValueError:
                return self._json({"error": "bad id"}, 400)
            body = self._body()
            fields = {}
            if "name" in body:
                fields["name"] = str(body["name"]).strip() or "未命名账号"
            if "note" in body:
                fields["note"] = str(body["note"])
            if "enabled" in body:
                fields["enabled"] = 1 if body["enabled"] else 0
            if "user_id" in body:
                fields["user_id"] = str(body["user_id"]).strip()
            if "domain" in body:
                fields["domain"] = str(body["domain"]).strip() or DEFAULT_DOMAIN
            if body.get("token"):
                token = str(body["token"]).strip()
                if token.lower().startswith("bearer "):
                    token = token[7:].strip()
                fields["token"] = seal(token)
                fields["token_state"] = ""
            if fields:
                fields["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                update_account_status(acc_id, **fields)
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        if not self._authed_if_api():
            return
        parts = self._path().strip("/").split("/")
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "accounts":
            try:
                acc_id = int(parts[2])
            except ValueError:
                return self._json({"error": "bad id"}, 400)
            with db() as conn:
                conn.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    def _authed_if_api(self):
        if self._path().startswith("/api/") and not self._authed():
            self._json({"error": "unauthorized"}, 401)
            return False
        return True


# ---------------------------------------------------------------- 启动


def main():
    init_db()
    crypto = "AES-GCM" if HAS_AES else ("DPAPI" if os.name == "nt" else "PLAIN")
    if not HAS_AES and os.name != "nt":
        log("警告：未安装 cryptography，令牌将以明文形式存储。建议 pip install cryptography")

    t = threading.Thread(target=scheduler, daemon=True)
    t.start()
    threading.Thread(target=browser_cleanup_loop, daemon=True).start()

    global PORT
    server = None
    for offset in range(6):
        try:
            server = ThreadingHTTPServer((HOST, PORT + offset), Handler)
            PORT = PORT + offset
            break
        except OSError:
            continue
    if not server:
        log("端口被占用，启动失败")
        sys.exit(1)

    host_show = "127.0.0.1" if HOST in ("0.0.0.0", "::") else HOST
    url = "http://%s:%d" % (host_show, PORT)
    log("=" * 52)
    log(" WorkBuddy 多账号签到助手  v%s" % API_VERSION)
    log(" 控制台: %s" % url)
    log(" 数据目录: %s" % DATA_DIR)
    log(" 令牌加密: %s" % crypto)
    log(" 访问密码: %s" % ("已启用" if PASSWORD else "未设置（局域网可见）"))
    log("=" * 52)

    if not NO_BROWSER and HOST in ("127.0.0.1", "localhost"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("已停止")


if __name__ == "__main__":
    main()
