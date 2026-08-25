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
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..core.disk import TOP_N, by_repo, scan_disk
from ..core.gitops import git_available, is_repo
from ..core.handoff import EXIT_CONCURRENT, EXIT_OK, Options, run_handoff
from ..core.portable import bare_session_id
from ..core.report import _redact_home
from ..core.transcript import merge_tool_runs, read_turns, render_markdown
from ..core.vitals import (
    SessionRow,
    find_sessions,
    group_by_agent,
    locate_by_id,
    scan_session_vitals,
)
from ..i18n import LANG_NAMES, LANG_SHORT, Translator, available, normalize
from ..platform import (
    agent_session_roots,
    find_guide,
    norm_path,
    open_in_browser,
    split_multi,
)

STATIC = Path(__file__).resolve().parent / "static"
# 图文说明的位置由 platform.find_guide() 决定：装好的包里找 gui/static/ 下的
# 副本（打包时从 docs/ 复制进去），源码检出里找 docs/guide.html。
# 每次请求都重新查而不是启动时定死一个常量：用户可能在进程活着的时候才设上
# AGENT_HANDOFF_HOME，也可能刚把检出放到位。查一次 is_file() 很便宜。
# 找不到时返回 404，前端据此隐藏入口。
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


def _run_job(jid: str, opts: Options, tr: Translator, bundle: str | None = None) -> None:
    try:
        res = run_handoff(opts, tr, log=lambda s: _job_log(jid, s))
        payload = res.to_dict()
        payload["body_bytes"] = len(res.body)
        # 交接成功后打包。失败只记进日志，不改变任务状态：交接本身已经完成，
        # 包是附加产物——为了打包失败而把整个任务标成 error，会让用户以为
        # 交接也没成，那比没有包糟得多。
        if bundle is not None and res.code == EXIT_OK and not opts.dry_run:
            try:
                from ..core.portable import default_bundle_dir, export_bundle

                target = (
                    Path(bundle).expanduser()
                    if bundle
                    else default_bundle_dir(opts.repo, datetime.now().strftime("%Y-%m-%d"))
                )
                mf = export_bundle(
                    out_dir=target,
                    doc_path=Path(res.out_path) if res.out_path else None,
                    prompt=res.prompt,
                    sessions=opts.sessions,
                    meta={
                        "name": res.ctx.get("repo_name", ""),
                        "branch": res.ctx.get("branch", ""),
                        "head": res.ctx.get("head_sha", ""),
                        "remote": res.ctx.get("remote", ""),
                    },
                )
                carried = sum(1 for s in mf["sessions"] if s.get("stored_name"))
                payload["bundle"] = str(target)
                payload["bundle_carried"] = carried
                _job_log(jid, tr.t("cli.bundle.exported", path=str(target), count=carried))
                if carried:
                    # 副本是原样字节，没脱敏。只在真带了副本时说，否则这条提示
                    # 会变成对每次运行都出现的噪声，然后被忽略。
                    _job_log(jid, tr.t("cli.bundle.verbatim"))
            except OSError as exc:
                _job_log(jid, tr.t("cli.bundle.export_failed", path=bundle or "", err=str(exc)))
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
        #
        # `extra` 里同名的头**覆盖**这里的默认值，不叠加：HTTP 允许同一个响应带
        # 多条 Content-Security-Policy，而浏览器会强制执行它们的**交集**——
        # 两条策略并存时更宽松的那条毫无作用。实测图文说明页因此白屏：
        # 它自带内联脚本，需要 'unsafe-inline'，但默认这条的 script-src 是
        # 'self'，交集等于什么都不许跑。
        extra = dict(extra or {})
        default_headers = {
            "Content-Security-Policy":
                "default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; font-src 'self'",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        }
        for name, value in default_headers.items():
            self.send_header(name, extra.pop(name, value))
        for k, v in extra.items():
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
        if path == "/guide.html":
            self._serve_guide()
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
        # 语言可能来自 ?lang=、也可能来自启动时的 --lang，两者都是用户显式指定的；
        # 都没有时才落到系统区域设置。前端要靠 langExplicit 判断该不该让
        # localStorage 里记住的旧选择覆盖本次的显式要求。
        asked = (params.get("lang") or [""])[0] or getattr(self.server, "lang", "")
        lang = normalize(asked)
        tr = Translator(lang)
        try:
            html = (STATIC / "index.html").read_text(encoding="utf-8")
        except OSError:
            self._err(HTTPStatus.INTERNAL_SERVER_ERROR, "index.html missing")
            return
        boot = {
            "token": TOKEN,
            "lang": lang,
            "langExplicit": bool(asked),
            "langs": [
                {"code": c, "name": LANG_NAMES.get(c, c), "short": LANG_SHORT.get(c, c[:2].upper())}
                for c in available()
            ],
            "strings": tr.table(),
            "version": __version__,
            "defaultRepo": getattr(self.server, "default_repo", ""),
            "gitAvailable": git_available(),
            # 图文说明只在仓库里跑时才有（装好的包不带 docs/）。前端据此决定
            # 显不显示入口——给一个点开是 404 的按钮比没有按钮更糟。
            "guideAvailable": find_guide() is not None,
            "sep": os.sep,
        }
        # </script> 在 JSON 字符串里出现会提前关闭标签；转义掉。
        blob = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
        html = html.replace("__BOOTSTRAP__", blob)
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def _serve_guide(self) -> None:
        """送图文说明。不带令牌校验：它是纯静态文档，没有任何本机数据。

        单独一条路由而不是塞进 static/：guide.html 由 build_guide.py 从三份
        JSON 重新生成，源码检出里它只该有一份（在 docs/ 下），仓库里放第二份
        副本只会让两边漂移。打包时才把它复制进 gui/static/，这样 wheel 用户
        也能看到——此前 docs/ 从不进包，`pip install` 之后这条路由必然 404。

        找不到时返回 404，前端据此隐藏入口，而不是给一个点开是空白的按钮。
        """
        guide = find_guide()
        if guide is None:
            self._err(HTTPStatus.NOT_FOUND, "guide not available")
            return
        try:
            body = guide.read_bytes()
        except OSError:
            self._err(HTTPStatus.NOT_FOUND, "guide not available")
            return
        # 说明文档自己带完整的 <script>（三语文案与渲染逻辑都在里面），
        # 所以这一份不能套用首页那条 script-src 'self' 的 CSP——那会把内联脚本
        # 拦掉，页面渲染出一片空白。它不读任何本机数据、也不发请求，
        # 允许内联脚本与样式就够，其余一律禁掉。
        self._send(
            200, body, "text/html; charset=utf-8",
            {"Cache-Control": "no-store", "Content-Security-Policy":
             "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
             "img-src data:; font-src 'none'; connect-src 'none'"},
        )

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

        if path == "/api/doctor":
            # 环境自检。只读，且最贵的一步是一次目录遍历（实测 481 份转录
            # 十几毫秒），所以同步执行——不必像交接那样起后台任务。
            #
            # 路径要脱敏：这份结果会显示在界面上，而用户可能在录屏或截图。
            # 数据根与临时目录的路径里带用户名。
            from ..core.doctor import run_doctor

            result = run_doctor()
            for c in result["checks"]:
                for key in ("path", "value"):
                    if key in c["data"]:
                        c["data"][key] = _redact_home(str(c["data"][key]))
            self._json(result)
            return

        if path == "/api/disk":
            # 磁盘报告只 stat 不读内容，所以同步执行也就十几毫秒——不需要像
            # 交接那样起后台任务。按仓库聚合要读内容（慢几个数量级），
            # 所以由前端显式请求，不默认做。
            report = scan_disk()
            want_repo = (params.get("by_repo") or ["0"])[0] not in ("0", "false", "no")
            repos: list[dict[str, object]] = []
            if want_repo:
                vit = scan_session_vitals(limit=max(len(report.rows), 12))
                cwd_of = {norm_path(str(v.path)): (v.repo or v.cwd or "") for v in vit}
                repos = [
                    {"repo": name, "count": n, "bytes": size}
                    for name, n, size in by_repo(report, cwd_of)[:TOP_N]
                ]
            self._json({
                "total_bytes": report.total_bytes,
                "count": len(report.rows),
                "elapsed_ms": round(report.elapsed_ms, 1),
                "roots": [{"agent": a, "path": _redact_home(str(p))} for a, p in report.roots],
                "reclaimable": [
                    {
                        "kind": kind,
                        "count": len(rows),
                        "bytes": sum(r.size for r in rows),
                    }
                    for kind, rows in report.reclaimable()
                ],
                "biggest": [r.to_dict() for r in report.biggest],
                "by_repo": repos,
            })
            return

        if path == "/api/find":
            # 一次可以给多个 ID / 关键词，逗号或空格分隔。走的是与 CLI 完全相同
            # 的两条路径（ID 全量精确定位 + 关键词受限搜索），否则同一个输入
            # 在网页里和命令行里给出不同结果，那种不一致比任何一边慢都难查。
            needles = split_multi((params.get("q") or [""])[0])
            if not needles:
                self._json({"rows": []})
                return
            hits: list[SessionRow] = []
            seen: set[str] = set()
            for row in locate_by_id(needles):
                key = norm_path(row.path)
                if key not in seen:
                    seen.add(key)
                    hits.append(row)
            rows = scan_session_vitals(limit=60)
            for needle in needles:
                for row in find_sessions(needle, rows):
                    key = norm_path(row.path)
                    if key not in seen:
                        seen.add(key)
                        hits.append(row)
            self._json({
                "rows": [r.to_dict() for r in hits][:40],
                # 多 ID 常常横跨不同项目，而交接固化的是**一个仓库**的状态。
                # 前端据此提示「这些会话不在同一个项目里」，让用户分别交接。
                "repos": sorted({r.repo for r in hits if r.repo}),
            })
            return

        if path == "/api/session-md":
            # 把一份转录渲染成可粘贴的 Markdown。
            #
            # 这个端点按 URL 参数读文件，所以**必须**把路径钉在 agent 的数据目录里。
            # 不钉的后果不是「可能被滥用」而是「这就是一个任意文件读取接口」：
            # 服务只绑 127.0.0.1 且校验令牌，但令牌会随页面进入浏览器，
            # 而浏览器里的任何一个标签页都可能被诱导发这个请求。
            raw = (params.get("path") or [""])[0]
            if not raw.strip():
                self._json({"error": "empty"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                target = Path(raw).expanduser().resolve()
            except (OSError, ValueError):
                self._json({"error": "bad-path"}, HTTPStatus.BAD_REQUEST)
                return
            agent = ""
            for name, root in agent_session_roots():
                try:
                    rroot = root.resolve()
                except OSError:
                    continue
                if norm_path(str(target)).startswith(norm_path(str(rroot)) + "/"):
                    agent = name
                    break
            if not agent or not target.is_file():
                # 刻意不区分「不在允许的目录里」与「文件不存在」：区分开等于
                # 提供一个探测本机文件是否存在的接口。
                self._json({"error": "not-allowed"}, HTTPStatus.FORBIDDEN)
                return
            turns = merge_tool_runs(read_turns(agent, target))
            meta = {"session_id": bare_session_id(target), "agent": agent}
            md = render_markdown(agent, meta, turns, tr)
            self._json({"markdown": md, "turns": len(turns)})
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
            # 没有 git 不再是「不合格」。会话传承、计划完成度、测试取证都不依赖
            # git，只有提交快照依赖。返回 ok=True 但带上 warn，让界面提示而不是拦。
            self._json({
                "ok": True,
                "reason": "",
                "warn": "" if is_repo(p) else "not_git",
                "path": str(p.resolve()),
            })
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
        # 不再因为缺 git 而拒绝：run_handoff 自己会降级（跳过提交与 git 现场），
        # 而用户要的往往正是不依赖 git 的那部分——把前序会话的结论带走。

        def flag(key: str) -> bool:
            return bool(body.get(key))

        try:
            timeout = int(body.get("test_timeout") or 900)
        except (TypeError, ValueError):
            timeout = 900

        # 勾选要传承的会话。只接受字符串列表，逐项去空白——请求体来自浏览器，
        # 不能假设结构正确；非列表当作没选，而不是抛异常让整个请求 500。
        raw_sessions = body.get("sessions")
        sessions: list[str] = []
        if isinstance(raw_sessions, list):
            for item in raw_sessions:
                text = str(item).strip()
                if text and text not in sessions:
                    sessions.append(text)

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
            sessions=sessions,
        )
        jid = _new_job()
        # 打包开关。请求体来自浏览器，所以三种形态都要认：缺字段 / false 表示
        # 不打包；true 表示打包到默认位置；字符串表示打包到指定目录。
        raw_bundle = body.get("export_bundle")
        bundle: str | None = None
        if isinstance(raw_bundle, str) and raw_bundle.strip():
            bundle = raw_bundle.strip()
        elif raw_bundle is True:
            bundle = ""
        threading.Thread(target=_run_job, args=(jid, opts, tr, bundle), daemon=True).start()
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
