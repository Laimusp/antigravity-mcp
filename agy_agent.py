#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agy_agent.py — AGENTIC Antigravity backend for the antigravity-mcp server.

Unlike agy_backend.py (which hits the raw streamGenerateContent API, where the model is a
"brain in a jar" with no tools), this runs the REAL `agy` CLI AGENT — the same one you use
interactively, which HAS Read/Write/Bash tools like Claude Code. Mechanism (proven in
fb2-translator's engines._run_agy_cli):

    prompt --> temp in.txt
    agy.exe -p "read in.txt, do the task, write the answer to out.txt" --dangerously-skip-permissions
    out.txt --> stdout

Why files and not stdout: `agy -p` runs a TUI language-server and writes NOTHING to stdout when
stdout is a pipe — so we make the agent deliver its answer through a FILE it writes itself, then
read that file. This is also why we don't put the (possibly huge) user prompt in argv: it goes in
in.txt, argv only carries the short fixed wrapper + temp paths (no Windows argv limit, no injection).

Isolation: the agent runs with USERPROFILE/HOME pointed at a private dir so its ~/.gemini/antigravity
state (knowledge.lock) does NOT fight the user's interactive `agy`. cwd is the temp dir, so without
an explicit --workspace the agent can only touch its own in/out files (read-only w.r.t. the repo).

Auth + geo-unlock + rotation: the agent authenticates with the Antigravity token in Windows
Credential Manager and goes through the local RU-unlock proxy (CLOUD_CODE_URL). Account rotation is
reused from agy_backend.py: proactive quota pre-check + reactive swap on quota errors.

Usage:
    echo "<prompt>" | python agy_agent.py [--model M] [--system S] [--workspace DIR]
stdout = the agent's answer (contents of out.txt). stderr = diagnostics.
"""
import sys, os, time, tempfile, shutil, subprocess, argparse
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="backslashreplace")
# stdin carries the prompt (UTF-8 from the MCP layer / a pipe). Without this, Windows decodes it as
# ascii+surrogateescape → high bytes (e.g. 0x98 in Cyrillic «И») become lone surrogates that blow up
# on write_text(utf-8). Force utf-8 read here so the script is correct even run directly.
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")

# Reuse account rotation / quota pre-check from the raw-API backend (same folder). Best-effort:
# if it can't import (e.g. requests missing), the agent still runs, just without auto-rotation.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from agy_backend import Session, swap_account, ensure_quota_before, load_config
    _HAS_ROTATION = True
except Exception as _e:                                   # pragma: no cover
    _HAS_ROTATION = False
    _IMPORT_ERR = _e

# ---- config -----------------------------------------------------------------
AGY_EXE = os.environ.get(
    "AGY_EXE",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "agy", "bin", "agy.exe"),
)
PROXY = os.environ.get("CLOUD_CODE_URL", "http://127.0.0.1:9999")
# Private agy state dir → own knowledge.lock, never collides with the user's interactive agy.
PROFILE = os.environ.get("AGY_AGENT_PROFILE", os.path.expanduser("~/.agy-mcp-profile"))
TIMEOUT = int(os.environ.get("AGY_AGENT_TIMEOUT", "900"))   # agent thinks for minutes on big tasks
MAX_SWAPS = int(os.environ.get("AGY_MAX_SWAPS", "3"))
DEFAULT_MODEL = os.environ.get("AGY_MODEL", "")             # set AGY_MODEL=gemini-pro-agent. "" => agy's built-in default = FLASH, NOT Pro (isolated MCP profile has no antigravity-cli/settings.json)!

_QUOTA_MARKERS = ("resource_exhausted", "resource has been exhausted", "quota",
                  "rate limit", "rate_limit", "too many requests", "exhausted", "429")
_AUTH_MARKERS = ("please login", "please run /login", "login required", "unauthorized",
                 "invalid api key", "credential", "reauthenticate", "401")
_LAUNCH_MARKERS = ("not found", "no such file", "not recognized", "cannot find",
                   "is not recognized as")


def log(*a):
    print("[agy_agent]", *a, file=sys.stderr, flush=True)


def _env():
    """Subprocess env: route agy through the unlock proxy + isolate its state dir."""
    e = dict(os.environ)
    e["CLOUD_CODE_URL"] = PROXY
    try:
        os.makedirs(PROFILE, exist_ok=True)
    except Exception:
        pass
    e["USERPROFILE"] = PROFILE      # Windows home → isolated ~/.gemini/antigravity (own lock)
    e["HOME"] = PROFILE             # POSIX home (harmless on Windows, future-proof)
    e["PYTHONIOENCODING"] = "utf-8"
    return e


_WRAP = (
    "Прочитай файл {infile} (кодировка UTF-8) — в нём ТВОЯ ПОЛНАЯ ЗАДАЧА/ПРОМПТ. "
    "Выполни ровно то, что в нём написано. Свой ИТОГОВЫЙ ответ запиши ТОЛЬКО в файл {outfile} "
    "в кодировке UTF-8 — без каких-либо пояснений до или после и НЕ печатай его в чат. "
    "Как только файл {outfile} записан — задача выполнена, останавливайся."
)


def run_agent(prompt, model=None, workspace=None, timeout=TIMEOUT):
    """One agy-agent run. Returns (result_text, stdout, stderr, rc). result_text from out.txt."""
    tmp = tempfile.mkdtemp(prefix="agymcp_")
    infile = os.path.join(tmp, "in.txt")
    outfile = os.path.join(tmp, "out.txt")
    try:
        Path(infile).write_text(prompt, encoding="utf-8")
        wrapped = _WRAP.format(infile=infile, outfile=outfile)
        args = [AGY_EXE, "-p", wrapped, "--dangerously-skip-permissions"]
        if model:
            args += ["--model", model]
        if workspace:                       # let the agent READ the repo too (opt-in; also writable!)
            args += ["--add-dir", workspace]
        try:
            p = subprocess.run(args, input=b"", capture_output=True,
                               cwd=tmp, env=_env(), timeout=timeout)
            out = p.stdout.decode("utf-8", "replace")
            err = p.stderr.decode("utf-8", "replace")
            rc = p.returncode
        except subprocess.TimeoutExpired:
            return "", "", "timeout", -1
        except (FileNotFoundError, NotADirectoryError, OSError) as e:
            return "", "", "agy.exe not launchable (%s): %s" % (AGY_EXE, e), -1
        result = ""
        try:
            if os.path.exists(outfile):
                result = Path(outfile).read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            result = ""
        return result, out, err, rc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)   # temp in/out ALWAYS removed


def ask(prompt, model=None, system=None, workspace=None):
    """Run the agent with proactive+reactive account rotation. Raises on terminal failure."""
    if not _HAS_ROTATION:
        log("rotation disabled (agy_backend import failed): %r" % _IMPORT_ERR)
    model = model or (DEFAULT_MODEL or None)
    if system and system.strip():
        prompt = "[СИСТЕМНАЯ ИНСТРУКЦИЯ]\n%s\n\n[ЗАДАЧА]\n%s" % (system.strip(), prompt)

    # proactive: switch BEFORE wasting a slow agent run if the active account is low on quota
    if _HAS_ROTATION:
        try:
            sess = Session()
            sess.ensure_fresh()
            ensure_quota_before(sess, model or "gemini-pro-agent", load_config())
        except Exception as e:
            log("proactive quota check skipped:", e)

    swaps = 0
    last = ""
    while True:
        result, out, err, rc = run_agent(prompt, model, workspace)
        if result:
            return result
        blob = ((err or "") + "\n" + (out or "")).lower()
        last = err or out or ("rc=%s" % rc)
        # reactive: quota -> rotate account and retry the whole agent run
        if _HAS_ROTATION and swaps < MAX_SWAPS and any(m in blob for m in _QUOTA_MARKERS):
            log("quota hit, rotating account (%d/%d)" % (swaps + 1, MAX_SWAPS))
            if swap_account():
                swaps += 1
                time.sleep(1)
                continue
        if any(m in blob for m in _AUTH_MARKERS):
            raise RuntimeError("agy agent: auth required (login/token). %s" % (last[-300:]))
        if err == "timeout":
            raise RuntimeError("agy agent timed out after %ds" % TIMEOUT)
        if any(m in blob for m in _LAUNCH_MARKERS):
            raise RuntimeError("agy agent could not launch: %s" % (last[-300:]))
        raise RuntimeError("agy agent produced no out-file (rc=%s): %s"
                           % (rc, (last[-400:] or "empty stderr")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    ap.add_argument("--system", default=None)
    ap.add_argument("--workspace", default=None, help="dir to --add-dir (let the agent read the repo)")
    ap.add_argument("--prompt", default=None)
    a = ap.parse_args()
    prompt = a.prompt if a.prompt is not None else sys.stdin.read()
    if not prompt or not prompt.strip():
        log("empty prompt"); sys.exit(2)
    try:
        ans = ask(prompt, model=a.model, system=a.system, workspace=a.workspace)
    except Exception as e:
        log("ERROR:", e); sys.exit(1)
    sys.stdout.write(ans + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
