#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agy_backend.py — headless Antigravity (Gemini 3) caller for the antigravity-mcp server.

Why this exists: the official Antigravity CLI (`agy -p`) runs a TUI language-server and
does NOT write the model answer to stdout when stdout is a pipe, so it is unusable for an
MCP wrapper. Instead we talk to the same backend the CLI talks to:

    stdin/prompt  ->  v1internal:streamGenerateContent (via the local RU-unlock proxy)  ->  stdout

Auth: the Antigravity CLI stores its OAuth in Windows Credential Manager under the target
`gemini:antigravity` (JSON {token:{access_token,refresh_token,expiry},auth_method}). We read
it, refresh with Antigravity's own OAuth client when near expiry, and write the fresh token
back so the real CLI keeps working too.

Geo-unlock: requests go through the existing local proxy (CLOUD_CODE_URL, default
127.0.0.1:9999) which rewrites loadCodeAssist eligibility and forwards via the German VPS.

Quota rotation: on 429/RESOURCE_EXHAUSTED we invoke the account switcher (agy_switch.py)
and retry with the next account.

Usage:
    echo "<prompt>" | python agy_backend.py [--model gemini-pro-agent] [--system "<sys>"] [--thinking]
    python agy_backend.py --prompt "<prompt>"

stdout = model answer only. stderr = diagnostics.
"""
import sys, os, json, time, uuid, argparse, subprocess, ctypes, ctypes.wintypes as wt
from datetime import datetime, timezone

import requests

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="backslashreplace")

# ---- config -----------------------------------------------------------------
PROXY = os.environ.get("CLOUD_CODE_URL", "http://127.0.0.1:9999")
CRED_TARGET = os.environ.get("AGY_CRED_TARGET", "gemini:antigravity")
# Refresh uses gemini-cli's PUBLIC installed-app OAuth client (the one published in
# github.com/google-gemini/gemini-cli). It refreshes the Antigravity-scoped token fine —
# the scope is carried by the refresh_token, not by the client. Override via env if rotated.
AGY_CLIENT_ID = os.environ.get("AGY_CLIENT_ID", "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com")
AGY_CLIENT_SECRET = os.environ.get("AGY_CLIENT_SECRET", "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")
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
# OAuth refresh + the API path go STRAIGHT to Google (not via the :9999 proxy). When the VPS IP is
# DPI-throttled you can route them through an external HTTP proxy by setting
#   AGY_UPSTREAM_PROXY="http://user:pass@host:port"
# Empty (the default) => direct connection (hosts->VPS). NEVER hard-code a proxy or credentials here:
# keep them in the environment (e.g. the MCP server's `env` block in .claude.json) so they stay out
# of git and out of this public repo.
_UP = os.environ.get("AGY_UPSTREAM_PROXY", "")
GPROXIES = ({"http": _UP, "https": _UP} if _UP else None)
UA = "antigravity/cli/1.0.9 windows/amd64"
DEFAULT_MODEL = os.environ.get("AGY_MODEL", "gemini-pro-agent")
SWITCH_SCRIPT = os.environ.get(
    "AGY_SWITCH_SCRIPT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "antigravity-auth-manager", "agy_switch.py"),
)
MAX_SWAPS = int(os.environ.get("AGY_MAX_SWAPS", "3"))
HTTP_TIMEOUT = int(os.environ.get("AGY_HTTP_TIMEOUT", "600"))
MAX_RETRIES = int(os.environ.get("AGY_MAX_RETRIES", "3"))         # ретраи обрыва SSE (IncompleteStream)
SAFETY_RETRIES = int(os.environ.get("AGY_SAFETY_RETRIES", "2"))   # ретраи safety-флапа (ContentBlocked)
# Keep-warm: на больших главах Pro High молчит ~60с (server-side thinking ~7k токенов) БЕЗ единого
# байта в SSE — это idle-окно, в котором соединение РФ→VPS→Google рвут промежуточные таймауты.
# Просим сервер стримить thoughts, чтобы канал не простаивал; текст thoughts парсер отбрасывает.
KEEPWARM = os.environ.get("AGY_KEEPWARM", "1") != "0"
# safetySettings: снимаем НАСТРАИВАЕМЫЕ фильтры (harassment/hate/sexual/dangerous/civic — дают
# finishReason=SAFETY). ВНИМАНИЕ: PROHIBITED_CONTENT/BLOCKLIST/SPII/RECITATION у Google НЕотключаемы
# (Prohibited Use Policy) — их это не уберёт; для них работает только ретрай-на-флапе в ask().
SAFETY_OFF = os.environ.get("AGY_SAFETY_OFF", "1") != "0"
_HARM_CATS = ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
              "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT",
              "HARM_CATEGORY_CIVIC_INTEGRITY")


def log(*a):
    print("[agy_backend]", *a, file=sys.stderr, flush=True)


# ---- Windows Credential Manager (ctypes) ------------------------------------
_advapi = ctypes.WinDLL("advapi32", use_last_error=True)


class _CRED(ctypes.Structure):
    _fields_ = [
        ("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR),
        ("Comment", wt.LPWSTR), ("LastWritten", wt.FILETIME),
        ("CredentialBlobSize", wt.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
        ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD), ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR),
    ]


def cred_read(target):
    ptr = ctypes.POINTER(_CRED)()
    if not _advapi.CredReadW(target, 1, 0, ctypes.byref(ptr)):
        raise OSError("CredRead(%s) failed: %s" % (target, ctypes.get_last_error()))
    c = ptr.contents
    blob = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-8")
    user = c.UserName or "antigravity"
    _advapi.CredFree(ptr)
    return json.loads(blob), user


def cred_write(target, blob_obj, username="antigravity"):
    data = json.dumps(blob_obj, separators=(",", ":")).encode("utf-8")
    buf = ctypes.create_string_buffer(data, len(data))
    c = _CRED()
    c.Flags = 0
    c.Type = 1  # GENERIC
    c.TargetName = target
    c.CredentialBlobSize = len(data)
    c.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))
    c.Persist = 2  # LOCAL_MACHINE (matches the CLI's own persistence)
    c.UserName = username
    if not _advapi.CredWriteW(ctypes.byref(c), 0):
        raise OSError("CredWrite(%s) failed: %s" % (target, ctypes.get_last_error()))


# ---- token handling ---------------------------------------------------------
def _parse_expiry(s):
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


class Session:
    def __init__(self):
        self.blob, self.cred_user = cred_read(CRED_TARGET)
        self.proj = None

    @property
    def token(self):
        return self.blob["token"]["access_token"]

    def ensure_fresh(self, force=False):
        exp = _parse_expiry(self.blob["token"].get("expiry"))
        if not force and exp and (exp - time.time()) > 120:
            return  # still valid for >2 min
        self.refresh()

    def refresh(self):
        rt = self.blob["token"]["refresh_token"]
        log("refreshing antigravity token...")
        r = requests.post(OAUTH_TOKEN_URL, data={
            "client_id": AGY_CLIENT_ID, "client_secret": AGY_CLIENT_SECRET,
            "refresh_token": rt, "grant_type": "refresh_token"}, timeout=60, proxies=GPROXIES)
        if not r.ok:
            log("refresh FAILED", r.status_code, r.text[:200])
            return False
        j = r.json()
        self.blob["token"]["access_token"] = j["access_token"]
        exp_dt = datetime.now(timezone.utc).astimezone()
        new_exp = exp_dt.timestamp() + int(j.get("expires_in", 3600))
        self.blob["token"]["expiry"] = datetime.fromtimestamp(new_exp, tz=exp_dt.tzinfo).isoformat()
        try:
            cred_write(CRED_TARGET, self.blob, self.cred_user)  # share fresh token with the real CLI
            log("token refreshed + written back to Credential Manager")
        except Exception as e:
            log("token refreshed (write-back failed, non-fatal):", e)
        return True

    def reload_after_swap(self):
        self.blob, self.cred_user = cred_read(CRED_TARGET)
        self.proj = None

    def headers(self):
        return {"Authorization": "Bearer " + self.token, "Content-Type": "application/json", "User-Agent": UA}

    def load_project(self):
        r = requests.post(PROXY + "/v1internal:loadCodeAssist", headers=self.headers(),
                          json={"metadata": {"ideType": "ANTIGRAVITY"}}, timeout=60)
        if r.status_code == 401:
            self.refresh()
            r = requests.post(PROXY + "/v1internal:loadCodeAssist", headers=self.headers(),
                              json={"metadata": {"ideType": "ANTIGRAVITY"}}, timeout=60)
        r.raise_for_status()
        self.proj = (r.json() or {}).get("cloudaicompanionProject")
        return self.proj


# ---- generation -------------------------------------------------------------
def build_body(proj, prompt, model, system, thinking):
    req = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 65535},
    }
    if system:
        req["systemInstruction"] = {"role": "user", "parts": [{"text": system}]}
    if SAFETY_OFF:
        # снять настраиваемые safety-категории; PROHIBITED_CONTENT это НЕ покрывает (неотключаем)
        req["safetySettings"] = [{"category": c, "threshold": "OFF"} for c in _HARM_CATS]
    if KEEPWARM:
        # держим SSE-канал тёплым во время длинной фазы thinking (см. KEEPWARM выше)
        req["generationConfig"]["thinkingConfig"] = {"includeThoughts": True}
    elif thinking:
        req["generationConfig"]["thinkingConfig"] = {"includeThoughts": False, "thinkingBudget": 10001}
    return {
        "project": proj,
        "requestId": "agent/%s/%d/%s/1" % (uuid.uuid4(), int(time.time() * 1000), uuid.uuid4()),
        "request": req,
        "model": model,
        "userAgent": "antigravity",
        "requestType": "agent",
    }


class QuotaExceeded(Exception):
    pass


class IncompleteStream(Exception):
    """SSE-поток оборвался ДО финального finishReason — т.е. соединение разорвано
    (upstream/agy_proxy/VPS), а не модель закончила. agy_proxy маскирует такой разрыв под
    обычный EOF (Connection: close без Content-Length), поэтому клиент получает «успех» с
    усечённым/пустым телом. Это РЕТРАИБЕЛЬНО: переоткрытие соединения чинит."""
    def __init__(self, partial_len, detail):
        super().__init__("incomplete stream (got %d chars, no finishReason): %s" % (partial_len, detail))
        self.partial_len = partial_len


class ContentBlocked(Exception):
    """Сервер ШТАТНО завершил поток (finishReason пришёл), но текста нет: ответ вырезан
    safety/recitation/лимитом вывода. Это НЕ сеть. У Gemini safety недетерминирован (флапает
    от прогона к прогону), поэтому ограниченно ретраибельно; причина уходит в stderr."""
    def __init__(self, reason, message):
        super().__init__("content blocked: %s — %s" % (reason, (message or "")[:200]))
        self.reason = reason


# finishReason'ы, означающие «сервер закончил, но текст вырезан/не сгенерирован» (НЕ обрыв сети).
_BLOCK_FINISH = {"SAFETY", "PROHIBITED_CONTENT", "RECITATION", "BLOCKLIST", "SPII",
                 "IMAGE_SAFETY", "MAX_TOKENS", "OTHER", "LANGUAGE", "MALFORMED_FUNCTION_CALL"}
# Сетевые исключения requests/urllib3, при которых тело рвётся на полуслове (обрыв upstream).
_NET_EXC = (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError,
            requests.exceptions.ReadTimeout, requests.exceptions.ContentDecodingError)


def stream_generate(sess, prompt, model, system, thinking, on_progress=None, _depth=0):
    if sess.proj is None:
        sess.load_project()
    body = build_body(sess.proj, prompt, model, system, thinking)
    r = requests.post(PROXY + "/v1internal:streamGenerateContent?alt=sse",
                      headers=sess.headers(), json=body, stream=True, timeout=HTTP_TIMEOUT)
    if r.status_code == 401:
        if _depth >= 2:        # защита от бесконечной рекурсии при «вечном» 401
            raise RuntimeError("streamGenerateContent HTTP 401 (refresh didn't help)")
        sess.refresh()
        return stream_generate(sess, prompt, model, system, thinking, on_progress, _depth + 1)
    if r.status_code in (429,) or (r.status_code == 403 and "exhaust" in r.text.lower()):
        raise QuotaExceeded("HTTP %s" % r.status_code)
    if r.status_code != 200:
        raise RuntimeError("streamGenerateContent HTTP %s: %s" % (r.status_code, r.text[:300]))

    text = []
    finish = None            # финальный finishReason (None => поток НЕ завершён штатно => обрыв)
    finish_msg = None
    net_exc = None
    # Decode the SSE stream as UTF-8 ourselves: cloudcode-pa omits charset, so
    # requests' decode_unicode=True would fall back to latin-1 and mojibake any
    # non-ASCII (e.g. Cyrillic) into double-encoded garbage.
    try:
        for raw in r.iter_lines(decode_unicode=False):
            if not raw:
                continue
            line = raw.decode("utf-8", "replace")
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                finish = finish or "DONE"
                break
            try:
                j = json.loads(data)
            except Exception:
                continue
            cands = (j.get("response", {}) or {}).get("candidates") or j.get("candidates") or []
            for c in cands:
                fr = c.get("finishReason")
                if fr:
                    finish = fr
                    finish_msg = c.get("finishMessage") or finish_msg
                for p in c.get("content", {}).get("parts", []):
                    if p.get("thought"):
                        continue
                    t = p.get("text")
                    if t:
                        text.append(t)
                        if on_progress:
                            on_progress(t)
            # surface mid-stream quota errors
            err = j.get("error")
            if err and ("RESOURCE_EXHAUSTED" in json.dumps(err) or err.get("code") == 429):
                raise QuotaExceeded(json.dumps(err)[:200])
    except _NET_EXC as e:
        net_exc = "%s: %s" % (type(e).__name__, str(e)[:160])

    result = "".join(text).strip()
    # Полнота определяется НАЛИЧИЕМ finishReason, а не тем, есть ли текст: усечённый ответ
    # (текст есть, но finishReason не пришёл) — это тоже обрыв, его нельзя отдавать как успех.
    if finish is not None and finish != "DONE":
        if result and finish.upper() not in _BLOCK_FINISH:
            return result                       # STOP с текстом — чистый полный ответ
        if result:
            return result                       # поздний SAFETY/PROHIBITED: текст уже собран — отдаём
        raise ContentBlocked(finish, finish_msg)  # завершён, но текста нет — отказ модели
    # finishReason не пришёл (None/DONE-без-текста) ИЛИ тело оборвалось сетевым исключением —
    # это разрыв соединения, маскируемый agy_proxy под EOF. Ретрай.
    raise IncompleteStream(len(result), net_exc or ("finish=%r, no terminal finishReason" % finish))


def swap_account():
    if not os.path.exists(SWITCH_SCRIPT):
        log("no switcher at", SWITCH_SCRIPT, "- cannot rotate")
        return False
    try:
        p = subprocess.run([sys.executable, SWITCH_SCRIPT, "next"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        log("switcher:", (p.stdout or "").strip()[-200:], (p.stderr or "").strip()[-120:])
        return p.returncode == 0
    except Exception as e:
        log("swap failed:", e)
        return False


# ---- proactive quota-based rotation (configurable threshold) ----------------
# Mirrors the gemini-cli + gchange pre-check: BEFORE a request, if the active
# account's quota for the target model is below the threshold, switch first.
# Lives here in the "agy" layer; the MCP server stays a thin caller.
CONFIG_FILE = os.path.expanduser("~/.gemini/antigravity_config.json")
QUOTA_CACHE_FILE = os.path.expanduser("~/.gemini/antigravity_quota_cache.json")
ACCOUNTS_FILE = os.path.expanduser("~/.gemini/antigravity_accounts.json")


def load_config():
    cfg = {"enabled": True, "threshold": 10.0, "cache_minutes": 3}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    if os.environ.get("AGY_QUOTA_THRESHOLD"):
        try:
            cfg["threshold"] = float(os.environ["AGY_QUOTA_THRESHOLD"])
        except ValueError:
            pass
    if os.environ.get("AGY_PROACTIVE") == "0":
        cfg["enabled"] = False
    return cfg


def _active_email():
    try:
        with open(ACCOUNTS_FILE, encoding="utf-8") as f:
            return json.load(f).get("active")
    except Exception:
        return None


def _pool_size():
    try:
        with open(ACCOUNTS_FILE, encoding="utf-8") as f:
            return len(json.load(f).get("order", [])) or 1
    except Exception:
        return 1


def _retrieve_quota(sess):
    if sess.proj is None:
        sess.load_project()
    r = requests.post(PROXY + "/v1internal:retrieveUserQuota", headers=sess.headers(),
                      json={"project": sess.proj}, timeout=30)
    if not r.ok:
        return None
    return {b["modelId"]: b.get("remainingFraction")
            for b in (r.json() or {}).get("buckets", []) if b.get("modelId")}


def _quota_fraction(sess, email, model, cache_min):
    cache = {}
    if os.path.exists(QUOTA_CACHE_FILE):
        try:
            with open(QUOTA_CACHE_FILE, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}
    ent = cache.get(email or "")
    if ent and (time.time() - ent.get("ts", 0)) < cache_min * 60:
        return ent.get("buckets", {}).get(model)
    q = _retrieve_quota(sess)
    if q is None:
        return None
    if email:
        cache[email] = {"ts": time.time(), "buckets": q}
        try:
            with open(QUOTA_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f)
        except Exception:
            pass
    return q.get(model)


def ensure_quota_before(sess, model, cfg):
    """Proactively switch BEFORE the request if the active account's quota for
    `model` is below the configured threshold (percent)."""
    if not cfg.get("enabled") or float(cfg.get("threshold", 0)) <= 0:
        return
    thr = float(cfg["threshold"]) / 100.0
    for _ in range(max(1, _pool_size())):
        email = _active_email()
        frac = _quota_fraction(sess, email, model, int(cfg.get("cache_minutes", 3)))
        if frac is None or frac >= thr:
            return  # quota OK (or unknown) — proceed; reactive 429 still covers surprises
        log("proactive rotate: %s %s=%.1f%% < %s%%" % (email, model, frac * 100, cfg["threshold"]))
        if not swap_account():
            return
        sess.reload_after_swap()
        sess.ensure_fresh()
    # all accounts below threshold — proceed anyway


def ask(prompt, model=DEFAULT_MODEL, system=None, thinking=False, on_progress=None):
    sess = Session()
    sess.ensure_fresh()
    ensure_quota_before(sess, model, load_config())
    swaps = incompletes = blocks = 0
    last = None
    # один общий потолок итераций (квота-свапы + ретраи обрывов + ретраи safety)
    for _ in range(MAX_SWAPS + MAX_RETRIES + SAFETY_RETRIES + 1):
        try:
            return stream_generate(sess, prompt, model, system, thinking, on_progress)
        except QuotaExceeded as q:
            last = q
            log("quota exceeded (%s) swap %d/%d" % (q, swaps + 1, MAX_SWAPS))
            if swaps >= MAX_SWAPS or not swap_account():
                raise
            swaps += 1
            sess.reload_after_swap()
            sess.ensure_fresh()
        except IncompleteStream as e:
            last = e
            incompletes += 1
            log("incomplete stream, retry %d/%d: %s" % (incompletes, MAX_RETRIES, e))
            if incompletes > MAX_RETRIES:
                # стабильный обрыв (деградация VPS/сети) — транзиент для движка (глава в pending)
                raise RuntimeError("stream kept cutting off (network/proxy): %s" % e)
            time.sleep(min(2 * incompletes, 6))   # короткий backoff перед переоткрытием соединения
        except ContentBlocked as e:
            last = e
            blocks += 1
            log("content blocked (%s), retry %d/%d: %s" % (e.reason, blocks, SAFETY_RETRIES, e))
            if blocks > SAFETY_RETRIES:
                # safety стабильно режет этот контент. Причина уже в stderr (выше). Пустой ответ
                # с НЕнулевым rc, чтобы движок не принял пустоту за валидный перевод.
                raise RuntimeError("content blocked by safety after %d retries: %s" % (SAFETY_RETRIES, e))
            time.sleep(1)
    raise RuntimeError("exhausted retries: %s" % last)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--system", default=None)
    ap.add_argument("--prompt", default=None)
    ap.add_argument("--thinking", action="store_true")
    a = ap.parse_args()
    prompt = a.prompt if a.prompt is not None else sys.stdin.read()
    if not prompt or not prompt.strip():
        log("empty prompt"); sys.exit(2)
    try:
        ans = ask(prompt, model=a.model, system=a.system, thinking=a.thinking)
    except Exception as e:
        log("ERROR:", e); sys.exit(1)
    sys.stdout.write(ans + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
