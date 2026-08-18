#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地网页界面的 HTTP 服务。零第三方依赖：标准库 http.server。

安全边界（这些不是可选项，因为服务器能对任意路径跑 git commit）：
  · 只绑定 127.0.0.1，永不监听外部接口
  · 启动时生成一次性令牌，写进注入到页面里的 bootstrap，所有 API 都校验它；
    这样同机上别的进程（或浏览器里别的站点）拿不到这个服务的控制权
  · 校验 Origin / Host，挡掉 DNS rebinding
  · 所有写操作走 POST，GET 只读
"""
from __future__ import annotations

import json
import mimetypes
import os
import secrets
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..core.gitops import git_available, is_repo
from ..core.handoff import EXIT_CONCURRENT, Options, run_handoff
from ..core.vitals import find_sessions, group_by_agent, scan_session_vitals
from ..i18n import LANG_NAMES, Translator, available, normalize
from ..platform import open_in_browser

STATIC = Path(__file__).resolve().parent / "static"
# 一次运行只发一个令牌。页面从注入的 bootstrap 里读它，不落盘、不进 URL。
TOKEN = secrets.token_urlsafe(24)
# 交接任务在后台线程跑，页面轮询进度。以任务 id 为键，保留最近若干个。
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
JOB_KEEP = 8


def _new_job() -> str:
    jid = secrets.token_urlsafe(9)
    with _jobs_lock:
        _jobs[jid] = {"state": "running", "log": [], "result": None, "started": time.time()}
        # 只留最近几个，避免长时间开着页面把内存越吃越多。
        if len(_jobs) > JOB_KEEP:
            for old in sorted(_jobs, key=lambda k: _jobs[k]["started"])[: len(_jobs) - JOB_KEEP]:
                if _jobs[old]["state"] != "running":
                    _jobs.pop(old, None)
    return jid


def _job_log(jid: str, line: str) -> None:
    with _jobs_lock:
        job = _jobs.get(jid)
        if job is not None:
            job["log"].append(line)


def _run_job(jid: str, opts: Options, tr: Translator) -> None:
    try:
        res = run_handoff(opts, tr, log=lambda s: _job_log(jid, s))
        payload = res.to_dict()
        payload["body_bytes"] = len(res.body)
        with _jobs_lock:
            job = _jobs.get(jid)
            if job is not None:
                job["result"] = payload
                job["state"] = "concurrent" if res.code == EXIT_CONCURRENT else (
                    "error" if res.error else "done"
                )
    except Exception as exc:  # noqa: BLE001 - 后台线程里任何异常都必须变成可见结果
        with _jobs_lock:
            job = _jobs.get(jid)
            if job is not None:
                job["state"] = "error"
                job["result"] = {"code": 1, "error": f"{type(exc).__name__}: {exc}"}


class Handler(BaseHTTPRequestHandler):
    server_version = f"agent-handoff/{__version__}"
    # 默认的 HTTP/1.0 会让每个请求新建连接；轮询进度时开销明显。
    protocol_version = "HTTP/1.1"

    # --- 基础设施 ---------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        """默认实现把每个请求打到 stderr，会把用户的终端刷满。"""
        return

    def _origin_ok(self) -> bool:
        """挡 DNS rebinding：Host 必须是回环地址，Origin 若有也必须是回环。"""
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "[::1]", "::1"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            netloc = urlparse(origin).hostname or ""
            if netloc not in ("127.0.0.1", "localhost", "::1"):
                return False
        return True

    def _auth_ok(self, params: dict[str, list[str]] | None = None, body: dict | None = None) -> bool:
        tok = self.headers.get("X-Handoff-Token") or ""
        if not tok and params:
            tok = (params.get("token") or [""])[0]
        if not tok and body:
            tok = str(body.get("token") or "")
        return secrets.compare_digest(tok, TOKEN)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 界面是本地生成的；禁掉外链与内联事件之外的一切，降低误引入远程资源的风险。
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _err(self, code: int, msg: str) -> None:
        self._json({"error": msg}, code)

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0 or n > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8", "replace")) or {}
        except (json.JSONDecodeError, ValueError, OSError):
            return {}

    # --- 路由 -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        if not self._origin_ok():
            self._err(HTTPStatus.FORBIDDEN, "bad host")
            return
        u = urlparse(self.path)
        params = parse_qs(u.query)
        path = u.path

        if path in ("/", "/index.html"):
            self._serve_index(params)
            return
        if path.startswith("/api/"):
            if not self._auth_ok(params):
                self._err(HTTPStatus.UNAUTHORIZED, "bad token")
                return
            self._api_get(path, params)
            return
        self._serve_static(path)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if not self._origin_ok():
            self._err(HTTPStatus.FORBIDDEN, "bad host")
            return
        u = urlparse(self.path)
        body = self._read_json()
        if not self._auth_ok(parse_qs(u.query), body):
            self._err(HTTPStatus.UNAUTHORIZED, "bad token")
            return
        self._api_post(u.path, body)

    # --- 页面与静态资源 ---------------------------------------------------

    def _serve_index(self, params: dict[str, list[str]]) -> None:
        lang = normalize((params.get("lang") or [getattr(self.server, "lang", "")])[0])
        tr = Translator(lang)
        try:
            html = (STATIC / "index.html").read_text(encoding="utf-8")
        except OSError:
            self._err(HTTPStatus.INTERNAL_SERVER_ERROR, "index.html missing")
            return
        boot = {
            "token": TOKEN,
            "lang": lang,
            "langs": [{"code": c, "name": LANG_NAMES.get(c, c)} for c in available()],
            "strings": tr.table(),
            "version": __version__,
            "defaultRepo": getattr(self.server, "default_repo", ""),
            "gitAvailable": git_available(),
            "sep": os.sep,
        }
        # </script> 在 JSON 字符串里出现会提前关闭标签；转义掉。
        blob = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
        html = html.replace("__BOOTSTRAP__", blob)
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def _serve_static(self, path: str) -> None:
        rel = path.lstrip("/")
        # 目录穿越防线：解析后必须仍在 STATIC 之内。
        target = (STATIC / rel).resolve()
        try:
            target.relative_to(STATIC.resolve())
        except ValueError:
            self._err(HTTPStatus.FORBIDDEN, "outside static root")
            return
        if not target.is_file():
            self._err(HTTPStatus.NOT_FOUND, "not found")
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        try:
            data = target.read_bytes()
        except OSError:
            self._err(HTTPStatus.INTERNAL_SERVER_ERROR, "read failed")
            return
        self._send(200, data, ctype, {"Cache-Control": "no-store"})

    # --- API --------------------------------------------------------------

    def _api_get(self, path: str, params: dict[str, list[str]]) -> None:
        lang = normalize((params.get("lang") or [getattr(self.server, "lang", "")])[0])
        tr = Translator(lang)

        if path == "/api/strings":
            self._json({"lang": lang, "strings": tr.table()})
            return

        if path == "/api/vitals":
            try:
                limit = int((params.get("limit") or ["12"])[0])
            except ValueError:
                limit = 12
            deep = (params.get("deep") or ["1"])[0] not in ("0", "false", "no")
            rows = scan_session_vitals(limit=max(1, min(limit, 200)), deep=deep)
            # 按 APP 分组、组内最近活动在前。分组顺序在 core 里定，前端只按
            # agent 字段切段，两个前端的排序规则不可能漂移。
            grouped = group_by_agent(rows)
            self._json(
                {
                    "rows": [r.to_dict() for _agent, group in grouped for r in group],
                    "groups": [
                        {"agent": agent, "count": len(group)} for agent, group in grouped
                    ],
                }
            )
            return

        if path == "/api/find":
            needle = (params.get("q") or [""])[0]
            if not needle.strip():
                self._json({"rows": []})
                return
            rows = scan_session_vitals(limit=60)
            self._json({"rows": [r.to_dict() for r in find_sessions(needle, rows)][:20]})
            return

        if path == "/api/check-repo":
            raw = (params.get("path") or [""])[0].strip().strip('"').strip("'")
            if not raw:
                self._json({"ok": False, "reason": "empty"})
                return
            p = Path(raw).expanduser()
            if not p.is_dir():
                self._json({"ok": False, "reason": "missing"})
                return
            self._json({"ok": is_repo(p), "reason": "" if is_repo(p) else "not_git", "path": str(p.resolve())})
            return

        if path == "/api/job":
            jid = (params.get("id") or [""])[0]
            since = 0
            try:
                since = int((params.get("since") or ["0"])[0])
            except ValueError:
                pass
            with _jobs_lock:
                job = _jobs.get(jid)
                if job is None:
                    self._err(HTTPStatus.NOT_FOUND, "no such job")
                    return
                self._json(
                    {
                        "state": job["state"],
                        "log": job["log"][since:],
                        "next": len(job["log"]),
                        "result": job["result"],
                    }
                )
            return

        self._err(HTTPStatus.NOT_FOUND, "unknown endpoint")

    def _api_post(self, path: str, body: dict) -> None:
        if path != "/api/handoff":
            self._err(HTTPStatus.NOT_FOUND, "unknown endpoint")
            return

        tr = Translator(normalize(str(body.get("lang") or getattr(self.server, "lang", ""))))
        raw = str(body.get("repo") or "").strip().strip('"').strip("'")
        if not raw:
            self._err(HTTPStatus.BAD_REQUEST, tr.t("gui.err.no_input"))
            return
        repo = Path(raw).expanduser()
        if not repo.is_dir():
            self._err(HTTPStatus.BAD_REQUEST, tr.t("gui.err.repo_missing"))
            return
        if not is_repo(repo):
            self._err(HTTPStatus.BAD_REQUEST, tr.t("gui.err.not_git"))
            return

        def flag(key: str) -> bool:
            return bool(body.get(key))

        try:
            timeout = int(body.get("test_timeout") or 900)
        except (TypeError, ValueError):
            timeout = 900

        opts = Options(
            repo=repo,
            plan=(str(body["plan"]).strip() or None) if body.get("plan") else None,
            out=(str(body["out"]).strip() or None) if body.get("out") else None,
            message=(str(body["message"]).strip() or None) if body.get("message") else None,
            no_commit=flag("no_commit"),
            skip_tests=flag("skip_tests"),
            test_timeout=max(10, min(timeout, 7200)),
            no_vitals=flag("no_vitals"),
            force=flag("force"),
            dry_run=flag("dry_run"),
        )
        jid = _new_job()
        threading.Thread(target=_run_job, args=(jid, opts, tr), daemon=True).start()
        self._json({"job": jid})


def _pick_port(preferred: int) -> int:
    """挑一个端口。指定的端口被占用时退回让内核分配，而不是直接失败。"""
    for cand in ([preferred] if preferred else []) + [0]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", cand))
                return int(s.getsockname()[1])
        except OSError:
            continue
    return 0


def serve(lang: str = "", port: int = 0, open_browser: bool = True, default_repo: str = "") -> int:
    """启动本地网页界面。阻塞直到 Ctrl+C。"""
    chosen = _pick_port(port)
    httpd = ThreadingHTTPServer(("127.0.0.1", chosen), Handler)
    httpd.daemon_threads = True
    httpd.lang = normalize(lang)  # type: ignore[attr-defined]
    httpd.default_repo = default_repo  # type: ignore[attr-defined]

    real_port = httpd.server_address[1]
    url = f"http://127.0.0.1:{real_port}/?token={TOKEN}"
    print(f"agent-handoff {__version__}  ->  {url}")
    print("Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.4, lambda: open_in_browser(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


def main_entry() -> None:
    from ..platform import force_utf8_io

    force_utf8_io()
    raise SystemExit(serve())


if __name__ == "__main__":
    main_entry()
