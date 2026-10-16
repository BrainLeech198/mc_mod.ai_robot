#!/usr/bin/env python3
"""DeepSink 的账本：多用户账户、会话、API key、用量与计费。

存储：单文件 SQLite `~/DeepSink/deepsink.db`（路径由 deepsink.json:data_dir 决定）。

表：
  users     id, email, name, pw_salt, pw_hash, pw_iters, created, balance,
            total_recharged, currency, is_admin, enabled
  sessions  token, user_id, created, expires
  api_keys  id, user_id, name, key_hash, key_disp, created, enabled, requests, tokens, cost

API key 只存 SHA-256 哈希（`key_hash`）与展示掩码（`key_disp`），明文仅在创建时返回一次；
旧库（明文 `key` 列）启动时自动表重建迁移。
  usage     id, ts, user_id, key_id, model, prompt_tokens, completion_tokens, cost, backend
  ledger    id, ts, user_id, kind, amount, balance_after, note

兼容迁移：若 DB 为空而旧目录存在 account.json / keys.json / apikey，
会自动建一个「本机账户」（local@deepsink）并把旧余额与旧密钥挂过去。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SESSION_TTL = 7 * 24 * 3600           # 会话有效期 7 天
PW_ITERS = 200_000                    # PBKDF2 迭代数

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  email           TEXT UNIQUE NOT NULL,
  name            TEXT NOT NULL,
  pw_salt         TEXT NOT NULL,
  pw_hash         TEXT NOT NULL,
  pw_iters        INTEGER NOT NULL,
  created         REAL NOT NULL,
  balance         REAL NOT NULL DEFAULT 0,
  total_recharged REAL NOT NULL DEFAULT 0,
  currency        TEXT NOT NULL DEFAULT 'CNY',
  is_admin        INTEGER NOT NULL DEFAULT 0,
  enabled         INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS sessions (
  token   TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  created REAL NOT NULL,
  expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
  id       TEXT PRIMARY KEY,
  user_id  INTEGER NOT NULL,
  name     TEXT NOT NULL,
  key_hash TEXT UNIQUE NOT NULL,
  key_disp TEXT NOT NULL,
  created  REAL NOT NULL,
  enabled  INTEGER NOT NULL DEFAULT 1,
  requests INTEGER NOT NULL DEFAULT 0,
  tokens   INTEGER NOT NULL DEFAULT 0,
  cost     REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                REAL NOT NULL,
  user_id           INTEGER NOT NULL,
  key_id            TEXT,
  model             TEXT,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  cost              REAL NOT NULL DEFAULT 0,
  backend           TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            REAL NOT NULL,
  user_id       INTEGER NOT NULL,
  kind          TEXT NOT NULL,
  amount        REAL NOT NULL,
  balance_after REAL NOT NULL,
  note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_user_ts ON usage(user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_ledger_user_ts ON ledger(user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_keys_user ON api_keys(user_id);
"""


def hash_password(password: str, salt_hex: str | None = None, iters: int = PW_ITERS):
    """返回 (salt_hex, hash_hex, iters)。"""
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, int(iters))
    return salt.hex(), dk.hex(), int(iters)


def verify_password(password: str, salt_hex: str, hash_hex: str, iters: int) -> bool:
    _, got, _ = hash_password(password, salt_hex, iters)
    return secrets.compare_digest(got, hash_hex)


def hash_key(token: str) -> str:
    """API key 的存储哈希（SHA-256）。

    key 本身是 40 位十六进制随机数（高熵），不像口令需要加盐抗暴破，
    与主流平台做法一致：库里只存哈希，明文仅在创建时返回一次。
    """
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def mask_key(token: str) -> str:
    """key 的展示形态：`sk-deepsink-682d••••••••22`（建时算好存库，列表只回它）。"""
    return token[:14] + "•" * 8 + token[-4:] if len(token) > 20 else "•" * 10


class Store:
    """线程安全（HTTP 服务是多线程的）。所有写操作走同一把锁。"""

    def __init__(self, db_path: Path, cfg: dict):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        b = cfg.get("billing", {})
        self.enabled = bool(b.get("enabled", True))
        self.currency = b.get("currency", "CNY")
        self.price_in = float(b.get("price_per_1k_input", 0.002)) / 1000.0
        self.price_out = float(b.get("price_per_1k_output", 0.008)) / 1000.0
        self.initial = float(b.get("initial_balance", 10.0))
        self.chars_per_token = float(b.get("chars_per_token", 1.6)) or 1.6
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
            self._migrate_keys_table()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _q(self, sql: str, args: tuple = ()):  # 查询
        # 与写操作共用同一把锁：HTTP 是多线程共享单连接，读也要串行化，
        # 否则并发读写会让 sqlite3 报 "Recursive use of cursors" 或读到半成品状态。
        with self._lock:
            cur = self.conn.execute(sql, args)
            return [dict(r) for r in cur.fetchall()]

    def _x(self, sql: str, args: tuple = ()):  # 执行
        with self._lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    @staticmethod
    def _pub(u: dict) -> dict:
        u = dict(u)
        for k in ("pw_hash", "pw_salt", "pw_iters"):
            u.pop(k, None)
        u["is_admin"] = bool(u.get("is_admin"))
        u["enabled"] = bool(u.get("enabled"))
        return u

    def _user(self, uid: int) -> dict | None:
        r = self._q("SELECT * FROM users WHERE id=?", (uid,))
        return r[0] if r else None

    # ------------------------------------------------------------------
    # 用户 / 账户
    # ------------------------------------------------------------------
    def count_users(self) -> int:
        return int(self._q("SELECT COUNT(*) AS n FROM users")[0]["n"])

    def create_user(self, email: str, name: str, password: str,
                    *, is_admin: bool = False, balance: float | None = None) -> dict:
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            raise ValueError("邮箱格式不正确")
        if not password or len(password) < 6:
            raise ValueError("密码至少 6 位")
        if self._q("SELECT id FROM users WHERE email=?", (email,)):
            raise ValueError("该邮箱已注册")
        salt, h, iters = hash_password(password)
        bal = round(self.initial if balance is None else float(balance), 6)
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO users(email,name,pw_salt,pw_hash,pw_iters,created,balance,"
                "total_recharged,currency,is_admin,enabled) VALUES(?,?,?,?,?,?,?,?,?,?,1)",
                (email, name or email.split("@")[0], salt, h, iters, time.time(),
                 bal, bal, self.currency, 1 if is_admin else 0))
            uid = cur.lastrowid
            self.conn.execute(
                "INSERT INTO ledger(ts,user_id,kind,amount,balance_after,note) VALUES(?,?,?,?,?,?)",
                (time.time(), uid, "signup_bonus", bal, bal, "注册赠送"))
            self.conn.commit()
        return self._pub(self._user(uid))

    def authenticate(self, email: str, password: str) -> dict | None:
        email = (email or "").strip().lower()
        r = self._q("SELECT * FROM users WHERE email=?", (email,))
        if not r:
            return None
        u = r[0]
        if not u["enabled"]:
            return None
        if not verify_password(password, u["pw_salt"], u["pw_hash"], u["pw_iters"]):
            return None
        return self._pub(u)

    def set_password(self, email: str, password: str) -> bool:
        if not password or len(password) < 6:
            raise ValueError("密码至少 6 位")
        salt, h, iters = hash_password(password)
        self._x("UPDATE users SET pw_salt=?,pw_hash=?,pw_iters=? WHERE email=?",
                (salt, h, iters, (email or "").strip().lower()))
        return True

    def user_by_session(self, token: str | None) -> dict | None:
        if not token:
            return None
        rows = self._q("SELECT * FROM sessions WHERE token=?", (token,))
        if not rows:
            return None
        s = rows[0]
        if s["expires"] < time.time():
            self._x("DELETE FROM sessions WHERE token=?", (token,))
            return None
        u = self._user(s["user_id"])
        return self._pub(u) if u and u["enabled"] else None

    def login(self, email: str, password: str) -> str | None:
        u = self.authenticate(email, password)
        if not u:
            return None
        token = secrets.token_urlsafe(32)
        now = time.time()
        self._x("INSERT INTO sessions(token,user_id,created,expires) VALUES(?,?,?,?)",
                (token, u["id"], now, now + SESSION_TTL))
        return token

    def logout(self, token: str | None) -> bool:
        if not token:
            return False
        self._x("DELETE FROM sessions WHERE token=?", (token,))
        return True

    # ---- 会话管理（对齐主流平台：可见会话、可踢出、改密失效）----
    @staticmethod
    def session_id(token: str) -> str:
        """对外暴露的会话 id：token 的短哈希（不泄露 token 本身）。"""
        return hashlib.sha256((token or "").encode("utf-8")).hexdigest()[:16]

    def sessions_of(self, uid: int, current_token: str | None = None) -> list[dict]:
        now = time.time()
        cur_id = self.session_id(current_token) if current_token else ""
        out = []
        for s in self._q("SELECT token,created,expires FROM sessions WHERE user_id=?"
                         " ORDER BY created DESC", (uid,)):
            sid = self.session_id(s["token"])
            out.append({"id": sid, "created": s["created"], "expires": s["expires"],
                        "current": sid == cur_id, "expired": s["expires"] < now})
        return out

    def revoke_session(self, uid: int, sid: str) -> bool:
        """按对外会话 id（短哈希）踢出会话。"""
        if not sid:
            return False
        with self._lock:
            rows = self.conn.execute("SELECT token FROM sessions WHERE user_id=?", (uid,)).fetchall()
            hit = [r["token"] for r in rows if self.session_id(r["token"]) == sid]
            for tok in hit:
                self.conn.execute("DELETE FROM sessions WHERE token=?", (tok,))
            self.conn.commit()
        return bool(hit)

    def revoke_other_sessions(self, uid: int, keep_token: str | None) -> int:
        with self._lock:
            cur = self.conn.execute("DELETE FROM sessions WHERE user_id=? AND token<>?",
                                    (uid, keep_token or ""))
            self.conn.commit()
            return cur.rowcount

    def change_password(self, uid: int, old: str, new: str,
                        keep_token: str | None = None) -> None:
        """改密：校验旧密码；成功后失效该用户其它会话（保留当前会话）。"""
        if not new or len(new) < 6:
            raise ValueError("新密码至少 6 位")
        u = self._user(uid)
        if not u:
            raise ValueError("用户不存在")
        if not verify_password(old or "", u["pw_salt"], u["pw_hash"], u["pw_iters"]):
            raise ValueError("当前密码不正确")
        salt, h, iters = hash_password(new)
        with self._lock:
            self.conn.execute("UPDATE users SET pw_salt=?,pw_hash=?,pw_iters=? WHERE id=?",
                              (salt, h, iters, uid))
            self.conn.execute("DELETE FROM sessions WHERE user_id=? AND token<>?",
                              (uid, keep_token or ""))
            self.conn.commit()

    def purge_expired_sessions(self) -> int:
        with self._lock:
            cur = self.conn.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            self.conn.commit()
            return cur.rowcount

    def account(self, uid: int) -> dict:
        u = self._user(uid)
        if not u:
            return {}
        return {"balance": round(u["balance"], 6), "currency": u["currency"],
                "total_recharged": round(u["total_recharged"], 6), "created": u["created"],
                "email": u["email"], "name": u["name"]}

    def balance(self, uid: int) -> float:
        u = self._user(uid)
        return float(u["balance"]) if u else 0.0

    def recharge(self, uid: int, amount: float, note: str = "充值") -> dict:
        amount = round(float(amount), 6)
        with self._lock:
            self.conn.execute(
                "UPDATE users SET balance=ROUND(balance+?,6),"
                "total_recharged=ROUND(total_recharged+?,6) WHERE id=?",
                (amount, amount, uid))
            bal = self.conn.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]
            self.conn.execute(
                "INSERT INTO ledger(ts,user_id,kind,amount,balance_after,note) VALUES(?,?,?,?,?,?)",
                (time.time(), uid, "recharge", amount, bal, note))
            self.conn.commit()
        return self.account(uid)

    def ledger(self, uid: int, limit: int = 50) -> list[dict]:
        return self._q("SELECT ts,kind,amount,balance_after,note FROM ledger "
                       "WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, int(limit)))

    def _migrate_keys_table(self) -> int:
        """旧库迁移：`api_keys.key`（明文）→ `key_hash` + `key_disp`（表重建）。

        旧 schema 的 `key` 列带 UNIQUE 约束、无法 DROP COLUMN，所以走
        「建新表 → 逐行算哈希拷贝 → 换表 → 重建索引」。幂等：新库直接返回。
        """
        cols = {r["name"] for r in self._q("PRAGMA table_info(api_keys)")}
        if "key_hash" in cols:
            return 0
        rows = self._q("SELECT * FROM api_keys")
        with self._lock:
            self.conn.execute("DROP TABLE IF EXISTS api_keys_migrate")
            self.conn.execute(
                "CREATE TABLE api_keys_migrate ("
                " id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL,"
                " key_hash TEXT UNIQUE NOT NULL, key_disp TEXT NOT NULL,"
                " created REAL NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,"
                " requests INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0,"
                " cost REAL NOT NULL DEFAULT 0)")
            n = 0
            for r in rows:
                plain = r["key"] or ""
                if not plain:
                    continue
                self.conn.execute(
                    "INSERT OR IGNORE INTO api_keys_migrate(id,user_id,name,key_hash,"
                    "key_disp,created,enabled,requests,tokens,cost)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (r["id"], r["user_id"], r["name"], hash_key(plain),
                     mask_key(plain), r["created"], r["enabled"],
                     r["requests"], r["tokens"], r["cost"]))
                n += 1
            self.conn.execute("DROP TABLE api_keys")
            self.conn.execute("ALTER TABLE api_keys_migrate RENAME TO api_keys")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_keys_user ON api_keys(user_id)")
            self.conn.commit()
        return n

    # ------------------------------------------------------------------
    # API keys
    # ------------------------------------------------------------------
    @staticmethod
    def _new_key_row(uid: int, name: str) -> tuple:
        plain = "sk-deepsink-" + secrets.token_hex(20)
        return ("key_" + uuid.uuid4().hex[:8], uid, name or "未命名",
                hash_key(plain), mask_key(plain), time.time(), 1, 0, 0, 0.0), plain

    def create_key(self, uid: int, name: str) -> dict:
        row, plain = self._new_key_row(uid, name)
        with self._lock:
            self.conn.execute(
                "INSERT INTO api_keys(id,user_id,name,key_hash,key_disp,created,enabled,"
                "requests,tokens,cost) VALUES(?,?,?,?,?,?,?,?,?,?)", row)
            self.conn.commit()
        # 明文只在这里返回一次；之后库里只有哈希，列表只回 key_disp 掩码
        return {"id": row[0], "name": row[2], "key": plain, "created": row[5], "enabled": True}

    def keys(self, uid: int) -> list[dict]:
        return self._q("SELECT * FROM api_keys WHERE user_id=? ORDER BY created", (uid,))

    def find_key(self, token: str | None) -> dict | None:
        """校验 API key，返回 {key 记录, user}；无效返回 None。"""
        if not token or not token.startswith("sk-"):
            return None
        rows = self._q("SELECT * FROM api_keys WHERE key_hash=?", (hash_key(token),))
        if not rows:
            return None
        k = rows[0]
        if not k["enabled"]:
            return None
        u = self._user(k["user_id"])
        if not u or not u["enabled"]:
            return None
        k["user"] = self._pub(u)
        return k

    def own_key(self, uid: int, kid: str) -> dict | None:
        r = self._q("SELECT * FROM api_keys WHERE id=? AND user_id=?", (kid, uid))
        return r[0] if r else None

    def delete_key(self, uid: int, kid: str) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM api_keys WHERE id=? AND user_id=?", (kid, uid))
            self.conn.commit()
            return cur.rowcount > 0

    def toggle_key(self, uid: int, kid: str, enabled: bool) -> bool:
        with self._lock:
            cur = self.conn.execute("UPDATE api_keys SET enabled=? WHERE id=? AND user_id=?",
                                    (1 if enabled else 0, kid, uid))
            self.conn.commit()
            return cur.rowcount > 0

    def public_keys(self, uid: int) -> list[dict]:
        out = []
        for k in self.keys(uid):
            k2 = {kk: k[kk] for kk in ("id", "name", "created", "enabled",
                                       "requests", "tokens", "cost")}
            k2["enabled"] = bool(k2["enabled"])
            k2["key_masked"] = k["key_disp"]
            out.append(k2)
        return out

    # ------------------------------------------------------------------
    # 计费
    # ------------------------------------------------------------------
    def estimate_tokens(self, text: str) -> int:
        return max(1, round(len(text or "") / self.chars_per_token))

    def estimate_messages(self, messages: list) -> int:
        n = 0
        for m in messages or []:
            c = m.get("content")
            if isinstance(c, list):
                c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
            n += self.estimate_tokens(str(c or ""))
        return max(1, n)

    def price(self, ptok: int, ctok: int) -> float:
        if not self.enabled:
            return 0.0
        return round(ptok * self.price_in + ctok * self.price_out, 6)

    def charge(self, uid: int, key_id: str | None, model: str,
               ptok: int, ctok: int, backend: str) -> dict:
        cost = self.price(ptok, ctok)
        with self._lock:
            self.conn.execute("UPDATE users SET balance=ROUND(balance-?,6) WHERE id=?", (cost, uid))
            bal = self.conn.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]
            self.conn.execute(
                "INSERT INTO usage(ts,user_id,key_id,model,prompt_tokens,completion_tokens,"
                "cost,backend) VALUES(?,?,?,?,?,?,?,?)",
                (time.time(), uid, key_id, model, ptok, ctok, cost, backend))
            if key_id:
                self.conn.execute(
                    "UPDATE api_keys SET requests=requests+1,tokens=tokens+?,cost=ROUND(cost+?,6)"
                    " WHERE id=?", (ptok + ctok, cost, key_id))
            if cost:
                self.conn.execute(
                    "INSERT INTO ledger(ts,user_id,kind,amount,balance_after,note)"
                    " VALUES(?,?,?,?,?,?)",
                    (time.time(), uid, "charge", -cost, bal, model))
            self.conn.commit()
        return {"ts": time.time(), "user_id": uid, "key_id": key_id, "model": model,
                "prompt_tokens": ptok, "completion_tokens": ctok, "cost": cost, "backend": backend}

    def usage(self, uid: int, limit: int = 50) -> list[dict]:
        return self._q("SELECT ts,key_id,model,prompt_tokens,completion_tokens,cost,backend "
                       "FROM usage WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, int(limit)))

    def stats(self, uid: int) -> dict:
        r = self._q("SELECT COUNT(*) AS requests, COALESCE(SUM(prompt_tokens),0) AS pt,"
                    " COALESCE(SUM(completion_tokens),0) AS ct, COALESCE(SUM(cost),0) AS cost"
                    " FROM usage WHERE user_id=?", (uid,))[0]
        return {"requests": r["requests"], "prompt_tokens": r["pt"], "completion_tokens": r["ct"],
                "tokens": r["pt"] + r["ct"], "cost_total": round(r["cost"], 6),
                "balance": self.balance(uid), "currency": self.currency,
                "key_count": len(self.keys(uid))}

    # ------------------------------------------------------------------
    # 迁移：旧 JSON 账本 → 本机账户
    # ------------------------------------------------------------------
    def import_legacy(self, legacy_dir: Path) -> dict | None:
        """把旧的 account.json / keys.json / apikey 迁进一个「本机账户」。

        仅当 DB 里一个用户都没有、且旧目录存在时才做。返回该用户（或 None）。
        """
        legacy_dir = Path(legacy_dir)
        if self.count_users() > 0 or not legacy_dir.exists():
            return None
        acc_p = legacy_dir / "account.json"
        keys_p = legacy_dir / "keys.json"
        boot_p = legacy_dir / "apikey"
        if not (acc_p.exists() or keys_p.exists() or boot_p.exists()):
            return None
        balance = None
        total = None
        if acc_p.exists():
            try:
                a = json.loads(acc_p.read_text(encoding="utf-8"))
                balance, total = float(a.get("balance", 0)), float(a.get("total_recharged", 0))
            except Exception:
                pass
        if balance is None:
            balance = self.initial
        try:
            u = self.create_user("local@deepsink", "本机账户",
                                 secrets.token_urlsafe(16), is_admin=True, balance=balance)
        except ValueError:
            return None
        if total is not None:
            self._x("UPDATE users SET total_recharged=? WHERE id=?", (total, u["id"]))
        migrated = 0
        rows = []          # (id, uid, name, key_hash, key_disp, created, enabled, req, tok, cost)
        if keys_p.exists():
            try:
                for k in json.loads(keys_p.read_text(encoding="utf-8")):
                    plain = k.get("key", "")
                    if not plain:
                        continue
                    rows.append((k.get("id") or "key_" + uuid.uuid4().hex[:8], u["id"],
                                 k.get("name", "旧密钥"), hash_key(plain), mask_key(plain),
                                 float(k.get("created", time.time())),
                                 1 if k.get("enabled", True) else 0,
                                 int(k.get("requests", 0)), int(k.get("tokens", 0)),
                                 float(k.get("cost", 0.0))))
            except Exception:
                pass
        if boot_p.exists():
            try:
                bk = boot_p.read_text(encoding="utf-8").strip()
                if bk and not any(r[3] == hash_key(bk) for r in rows):
                    rows.append(("key_" + uuid.uuid4().hex[:8], u["id"], "引导密钥",
                                 hash_key(bk), mask_key(bk), time.time(), 1, 0, 0, 0.0))
            except Exception:
                pass
        for r in rows:
            try:
                self._x("INSERT OR IGNORE INTO api_keys(id,user_id,name,key_hash,key_disp,"
                        "created,enabled,requests,tokens,cost) VALUES(?,?,?,?,?,?,?,?,?,?)", r)
                migrated += 1
            except Exception:
                pass
        return {**self._pub(self._user(u["id"])), "migrated_keys": migrated}
