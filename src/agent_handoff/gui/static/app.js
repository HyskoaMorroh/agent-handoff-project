/* agent-handoff 网页界面前端。零依赖、零构建：一个 IIFE。
 *
 * 全部文案从注入的 bootstrap 里取，切语言时重新拉一次字符串表并原地重绘，
 * 不刷新页面——刷新会丢掉正在跑的交接任务的进度。
 *
 * 所有拼进 DOM 的值都走 el() / txt() 走 textContent，绝不 innerHTML 拼接：
 * 会话的开场提问和仓库路径都是外部数据，里面可能有 < 和 &。
 */
(function () {
  "use strict";

  const BOOT = JSON.parse(document.getElementById("bootstrap").textContent);
  let S = BOOT.strings || {};
  let LANG = BOOT.lang;

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const t = (key, vars) => {
    let s = S[key];
    if (s === undefined) return "??" + key + "??";
    if (vars) for (const k in vars) s = s.split("{" + k + "}").join(String(vars[k]));
    return s;
  };

  /* el("div.foo", {attr}, ...children) —— children 里的字符串一律走 textContent */
  function el(spec, attrs) {
    const parts = String(spec).split(".");
    const node = document.createElement(parts[0] || "div");
    for (let i = 1; i < parts.length; i++) node.classList.add(parts[i]);
    if (attrs) {
      for (const k in attrs) {
        const v = attrs[k];
        if (v === null || v === undefined || v === false) continue;
        if (k === "text") node.textContent = String(v);
        else if (k === "onclick") node.addEventListener("click", v);
        else node.setAttribute(k, String(v));
      }
    }
    for (let i = 2; i < arguments.length; i++) {
      const c = arguments[i];
      if (c === null || c === undefined || c === false) continue;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return node;
  }
  const clear = (n) => { while (n.firstChild) n.removeChild(n.firstChild); return n; };

  /* ── 网络 ──────────────────────────────────────────────── */
  async function api(path, opts) {
    const o = Object.assign({ headers: {} }, opts || {});
    o.headers["X-Handoff-Token"] = BOOT.token;
    if (o.body !== undefined && typeof o.body !== "string") {
      o.headers["Content-Type"] = "application/json";
      o.body = JSON.stringify(o.body);
      o.method = o.method || "POST";
    }
    const res = await fetch(path, o);
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (!res.ok) throw new Error((data && data.error) || ("HTTP " + res.status));
    return data;
  }

  /* ── i18n 应用 ─────────────────────────────────────────── */
  function applyStrings() {
    $$("[data-i18n]").forEach((n) => { n.textContent = t(n.getAttribute("data-i18n")); });
    const repo = $("#repo");
    if (repo) repo.placeholder = t("gui.handoff.repo.ph");
    const fq = $("#find-q");
    if (fq) fq.placeholder = t("gui.find.ph");
    $("#scan-btn").textContent = scanned ? t("gui.vitals.rescan") : t("gui.vitals.scan");
    document.documentElement.lang = LANG;
    document.title = t("gui.title");
    $$("#theme-seg button").forEach((b) => {
      b.title = t("gui.theme." + b.getAttribute("data-theme-set"));
    });
  }

  function buildLangSeg() {
    const seg = clear($("#lang-seg"));
    (BOOT.langs || []).forEach((l) => {
      // 侧栏窄，用短标签；完整名字放 title。
      const short = l.code === "zh-Hans" ? "简" : l.code === "zh-Hant" ? "繁" : "EN";
      const b = el("button", { text: short, title: l.name, "aria-pressed": String(l.code === LANG) });
      b.addEventListener("click", () => setLang(l.code));
      seg.appendChild(b);
    });
  }

  async function setLang(code) {
    if (code === LANG) return;
    try {
      const data = await api("/api/strings?lang=" + encodeURIComponent(code));
      S = data.strings; LANG = data.lang;
    } catch (e) { return; }
    try { localStorage.setItem("ah.lang", LANG); } catch (_) {}
    buildLangSeg(); applyStrings(); rerender();
  }

  /* ── 主题 ──────────────────────────────────────────────── */
  function setTheme(mode) {
    document.documentElement.setAttribute("data-theme", mode);
    try { localStorage.setItem("ah.theme", mode); } catch (_) {}
    $$("#theme-seg button").forEach((b) => {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-theme-set") === mode));
    });
  }

  /* ── 视图切换 ──────────────────────────────────────────── */
  function showView(name) {
    $$(".view").forEach((v) => v.classList.toggle("is-active", v.getAttribute("data-view") === name));
    $$(".nav-item").forEach((b) => {
      const on = b.getAttribute("data-view") === name;
      b.classList.toggle("is-active", on);
      b.setAttribute("aria-selected", String(on));
    });
  }

  /* ── 会话卡片 ──────────────────────────────────────────── */
  /* 相对时间：「3 小时前」比「2026-08-18 13:57:34」更快让人认出是哪次对话。
   * 绝对时间仍在下面的键值区里，需要精确对照时看那一行。 */
  function ago(iso) {
    const then = Date.parse(iso);
    if (isNaN(then)) return "";
    const sec = Math.max(0, (Date.now() - then) / 1000);
    const units = [
      [86400 * 30, "mo"], [86400 * 7, "w"], [86400, "d"], [3600, "h"], [60, "m"]
    ];
    for (const u of units) {
      if (sec >= u[0]) return t("gui.ago." + u[1], { n: Math.floor(sec / u[0]) });
    }
    return t("gui.ago.now");
  }

  function sessionCard(r, opts) {
    const card = el("div.srow", { "data-band": r.band });
    const top = el("div.srow-top");
    // 组内不重复 APP 名（组标题已经说了）；找会话视图里没有分组，要显示。
    if (opts && opts.showAgent) top.appendChild(el("span.agent", { text: r.agent }));
    top.appendChild(el("span.badge", { text: t("band." + r.band), "data-band": r.band }));
    top.appendChild(el("span.when", { text: ago(r.mtime), title: r.mtime_text }));

    const m = el("div.metrics");
    m.appendChild(el("span", null, el("b", { text: r.mb.toFixed(1) }), " MB"));
    m.appendChild(el("span", { class: r.fatal ? "hot" : "" }, t("gui.label.fatal") + " ", el("b", { text: String(r.fatal) })));
    m.appendChild(el("span", null, t("gui.label.errors") + " ", el("b", { text: String(r.errors) })));
    top.appendChild(m);
    card.appendChild(top);

    const kv = el("dl.kv");
    const row = (k, v, cls) => {
      if (!v) return;
      kv.appendChild(el("dt", { text: k }));
      // 长文本夹两行 + title 悬停看全文：一屏能扫过更多会话卡片。
      kv.appendChild(el("dd", { text: v, class: cls || "", title: cls && cls.indexOf("clamp") >= 0 ? v : null }));
    };
    row(t("gui.label.session"), r.session_id || "—");
    row(t("gui.label.thread"), r.thread_id);
    row(t("gui.label.mtime"), r.mtime_text);
    row(t("gui.label.cwd"), r.cwd);
    row(t("gui.label.client"), [r.version, r.origin].filter(Boolean).join(" "));
    row(t("gui.label.first_prompt"), r.first_prompt, "prose clamp");
    if (r.repos && r.repos.length) {
      const extra = r.repos.length > 1 ? t("cli.card.repos_more", { count: r.repos.length - 1 }) : "";
      row(t("gui.label.repos"), r.repos[0] + extra);
    }
    row(t("gui.label.file"), r.path);
    card.appendChild(kv);

    if (r.repo && !(opts && opts.noAction)) {
      const act = el("div.srow-act");
      act.appendChild(el("button.link", {
        text: t("gui.find.use"),
        onclick: () => { $("#repo").value = r.repo; showView("handoff"); $("#repo").focus(); }
      }));
      card.appendChild(act);
    }
    return card;
  }

  /* ── 体检 ──────────────────────────────────────────────── */
  let scanned = false;
  let lastVitals = null;

  async function scanVitals() {
    const btn = $("#scan-btn");
    const out = clear($("#vitals-out"));
    btn.disabled = true;
    out.appendChild(el("div.card", null, el("div.progress", null, el("span")),
      el("p.hint", { text: t("gui.vitals.scanning") })));
    try {
      const deep = $("#scan-deep").checked ? "1" : "0";
      const data = await api("/api/vitals?limit=14&deep=" + deep);
      lastVitals = data.rows || [];
      scanned = true;
      btn.textContent = t("gui.vitals.rescan");
      renderVitals();
    } catch (e) {
      clear(out).appendChild(errPanel(e.message));
    } finally {
      btn.disabled = false;
    }
  }

  function renderVitals() {
    const out = clear($("#vitals-out"));
    const rows = lastVitals || [];
    const stats = $("#vitals-stats");
    if (!rows.length) {
      stats.hidden = true;
      out.appendChild(el("div.card", null, el("p.empty", { text: t("gui.vitals.empty") })));
      return;
    }
    const risky = rows.filter((r) => r.band === "critical" || r.band === "high");
    stats.hidden = false;
    $("#st-total").textContent = String(rows.length);
    $("#st-risky").textContent = String(risky.length);
    $("#st-size").textContent = Math.max.apply(null, rows.map((r) => r.mb)).toFixed(1) + " MB";

    /* 按 APP 分组，组内最近活动在前。后端已经排好序（group_by_agent），
     * 这里只按 agent 切段，不重新排 —— 排序规则只留一处。 */
    let current = null;
    let group = null;
    rows.forEach((r) => {
      if (r.agent !== current) {
        current = r.agent;
        const n = rows.filter((x) => x.agent === current).length;
        const riskyN = rows.filter((x) => x.agent === current && (x.band === "critical" || x.band === "high")).length;
        const head = el("div.group-h", null,
          el("span.group-app", { text: r.agent }),
          el("span.n", { text: String(n) }));
        if (riskyN) {
          head.appendChild(el("span.badge", {
            text: t("gui.vitals.risky") + " " + riskyN, "data-band": "critical"
          }));
        }
        out.appendChild(head);
        group = el("div.group");
        out.appendChild(group);
      }
      group.appendChild(sessionCard(r));
    });
  }

  /* ── 找会话 ────────────────────────────────────────────── */
  let lastFind = null;

  async function doFind() {
    const q = $("#find-q").value.trim();
    const out = clear($("#find-out"));
    if (!q) { out.appendChild(errPanel(t("gui.err.no_input"))); return; }
    out.appendChild(el("div.card", null, el("div.progress", null, el("span"))));
    try {
      const data = await api("/api/find?q=" + encodeURIComponent(q));
      lastFind = data.rows || [];
      renderFind();
    } catch (e) {
      clear(out).appendChild(errPanel(e.message));
    }
  }

  function renderFind() {
    const out = clear($("#find-out"));
    const rows = lastFind || [];
    if (!rows.length) {
      out.appendChild(el("div.card", null, el("p.empty", { text: t("cli.find.hint") })));
      return;
    }
    rows.forEach((r) => out.appendChild(sessionCard(r, { showAgent: true })));
  }

  /* ── 交接 ──────────────────────────────────────────────── */
  let lastResult = null;
  let lastDry = false;
  let polling = null;

  function errPanel(msg) {
    return el("div.panel.is-danger", null,
      el("div.panel-h", null, t("gui.err.title")),
      el("div.panel-b", null, el("p", { text: msg })));
  }

  async function startHandoff(dry) {
    if (polling) return;
    const repo = $("#repo").value.trim();
    const out = clear($("#handoff-out"));
    if (!repo) { out.appendChild(errPanel(t("gui.err.no_input"))); return; }

    $("#run-btn").disabled = true;
    $("#dry-btn").disabled = true;
    lastDry = dry;

    const logPre = el("pre.log");
    out.appendChild(el("div.panel", null,
      el("div.panel-h", null,
        dry ? el("span.badge.badge-dry", { text: t("gui.badge.dry") }) : null,
        el("span", { text: t("gui.handoff.running") })),
      el("div.panel-b", null, el("div.progress", null, el("span")), logPre)));

    let job;
    try {
      job = await api("/api/handoff", {
        body: {
          lang: LANG, repo: repo, dry_run: dry,
          skip_tests: $("#o-skip-tests").checked,
          no_commit: $("#o-no-commit").checked,
          no_vitals: $("#o-no-vitals").checked,
          force: $("#o-force").checked
        }
      });
    } catch (e) {
      clear(out).appendChild(errPanel(e.message));
      $("#run-btn").disabled = false; $("#dry-btn").disabled = false;
      return;
    }

    let since = 0;
    polling = setInterval(async () => {
      let st;
      try {
        st = await api("/api/job?id=" + encodeURIComponent(job.job) + "&since=" + since);
      } catch (e) { return; }
      if (st.log && st.log.length) {
        since = st.next;
        logPre.textContent += st.log.join("\n") + "\n";
        logPre.scrollTop = logPre.scrollHeight;
      }
      if (st.state === "running") return;
      clearInterval(polling); polling = null;
      $("#run-btn").disabled = false; $("#dry-btn").disabled = false;
      lastResult = st.result;
      renderResult(st.state, logPre.textContent);
    }, 450);
  }

  function renderResult(state, logText) {
    const out = clear($("#handoff-out"));
    const r = lastResult || {};

    if (state === "error" || r.error) {
      out.appendChild(errPanel(r.error || "unknown error"));
      if (logText) out.appendChild(el("div.panel", null,
        el("div.panel-h", null, t("gui.nav.handoff")), el("div.panel-b", null, el("pre.log", { text: logText }))));
      return;
    }

    if (state === "concurrent") {
      const body = el("div.panel-b");
      const ul = el("ul.list");
      (r.conflicts || []).forEach((c) => ul.appendChild(el("li", { text: c })));
      body.appendChild(ul);
      body.appendChild(el("p.hint", { text: t("gui.stop.hint") }));
      out.appendChild(el("div.panel.is-danger", null,
        el("div.panel-h", null, t("gui.stop.title")), body));
      return;
    }

    /* 现场摘要 */
    const kv = el("dl.kv");
    const row = (k, v) => { if (v || v === 0) { kv.appendChild(el("dt", { text: k })); kv.appendChild(el("dd", { text: String(v) })); } };
    row(t("gui.label.branch"), r.branch);
    row(t("gui.label.head"), r.head);
    row(t("gui.label.plan"), r.plan_rel);
    row(t("gui.label.commit"), (r.commit_result || "").split("\n")[0]);
    if (r.total_steps) row(t("gui.label.steps"), r.ticked + " / " + r.total_steps);
    if (!lastDry && r.out_path) row(t("gui.handoff.wrote"), r.out_path);

    out.appendChild(el("div.panel", null,
      el("div.panel-h", null,
        lastDry ? el("span.badge.badge-dry", { text: t("gui.badge.dry") }) : null,
        el("span", { text: r.repo_name || t("gui.nav.handoff") })),
      el("div.panel-b", null, kv)));

    /* 完成度表 */
    const rep = r.report || {};
    const nums = Object.keys(rep).sort((a, b) => Number(a) - Number(b));
    if (nums.length) {
      const table = el("table.grid");
      const head = el("tr");
      ["Task", t("gui.label.steps"), t("doc.table.head").split("|")[3].trim(), t("doc.table.head").split("|")[4].trim(), ""].forEach((h, i) => {
        head.appendChild(el("th", { text: i === 4 ? "" : h }));
      });
      table.appendChild(el("thead", null, head));
      const tb = el("tbody");
      nums.forEach((n) => {
        const x = rep[n];
        const done = (r.done_by_task || {})[n] || 0;
        const fe = x.files_present.length + x.files_missing.length;
        const se = x.symbols_ok.length + x.symbols_missing.length;
        const cls = x.complete ? "tag-done" : (x.files_present.length || x.symbols_ok.length) ? "tag-part" : "tag-none";
        const label = x.complete ? t("doc.verdict.done")
          : (x.files_present.length || x.symbols_ok.length) ? t("doc.verdict.partial")
          : t("doc.verdict.none").replace(/\*/g, "");
        tb.appendChild(el("tr", null,
          el("td", { text: n + " " + String(x.title).split(":").slice(-1)[0].trim() }),
          el("td.num", { text: done + " / " + (x.steps - done) }),
          el("td.num", { text: fe ? x.files_present.length + "/" + fe : "—" }),
          el("td.num", { text: se ? x.symbols_ok.length + "/" + se : "—" }),
          el("td", null, el("span.tag." + cls, { text: label }))));
      });
      table.appendChild(tb);
      out.appendChild(el("div.panel", null,
        el("div.panel-h", null, t("doc.h.step2").replace(/^#+\s*/, "")),
        el("div.panel-b", null, el("div.scroll-x", null, table))));
    }

    /* 测试 */
    const tests = r.test_results || {};
    const tnames = Object.keys(tests);
    if (tnames.length) {
      const body = el("div.panel-b");
      tnames.forEach((n) => {
        body.appendChild(el("p", null, el("strong", { text: n }), " ", el("code", { text: (r.test_commands || {})[n] || "" })));
        body.appendChild(el("pre.log", { text: tests[n] }));
      });
      out.appendChild(el("div.panel", null, el("div.panel-h", null, t("gui.label.tests")), body));
    }

    /* 缺口 + 环境 + 受保护文件 */
    const lists = [
      [t("gui.label.gaps"), r.gap_hints || []],
      [t("gui.label.env"), r.pitfalls || []],
      [t("gui.label.protected"), r.protected || []]
    ].filter((p) => p[1].length);
    lists.forEach((pair) => {
      const ul = el("ul.list");
      pair[1].forEach((x) => ul.appendChild(el("li", { text: String(x).replace(/[`*]/g, "") })));
      out.appendChild(el("div.panel", null, el("div.panel-h", null, pair[0]), el("div.panel-b", null, ul)));
    });

    /* 赛跑警告 */
    if ((r.race_warnings || []).length) {
      const ul = el("ul.list");
      r.race_warnings.forEach((x) => ul.appendChild(el("li", { text: x })));
      out.appendChild(el("div.panel.is-danger", null,
        el("div.panel-h", null, t("cli.race.warn").trim().replace(/^\[.*?\]\s*/, "")),
        el("div.panel-b", null, ul, el("p.hint", { text: t("cli.race.explain2").trim() }))));
    }

    /* 提示词 —— 最重要的产出，放最后但给主色按钮 */
    if (r.prompt) {
      const copyBtn = el("button.btn.btn-primary", { text: t("gui.handoff.copy") });
      copyBtn.addEventListener("click", async () => {
        const text = r.prompt;
        try {
          await navigator.clipboard.writeText(text);
        } catch (_) {
          // 非安全上下文里 clipboard API 不可用；退回选中让用户按 Ctrl+C。
          const pre = $("#prompt-pre");
          if (pre) {
            const rng = document.createRange(); rng.selectNodeContents(pre);
            const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(rng);
          }
        }
        copyBtn.textContent = t("gui.handoff.copied");
        setTimeout(() => { copyBtn.textContent = t("gui.handoff.copy"); }, 1600);
      });
      out.appendChild(el("div.panel", null,
        el("div.panel-h", null,
          el("span", { text: t("gui.handoff.prompt_title") }),
          el("span.grow"), copyBtn),
        el("div.panel-b", null,
          el("pre.prompt", { text: r.prompt, id: "prompt-pre" }),
          el("p.hint", { text: t("gui.handoff.prompt_hint") }))));
    }

    /* 完整日志收在最后 */
    if (logText) {
      out.appendChild(el("div.panel", null,
        el("div.panel-h", null, t("gui.nav.handoff")),
        el("div.panel-b", null, el("pre.log", { text: logText }))));
    }
  }

  /* 切语言后原地重绘已有结果，不丢状态 */
  function rerender() {
    if (lastVitals) renderVitals();
    if (lastFind) renderFind();
    if (lastResult) renderResult(lastResult.error ? "error" : "done", "");
  }

  /* ── 启动 ──────────────────────────────────────────────── */
  function init() {
    $("#ver").textContent = BOOT.version;

    let saved = null;
    try { saved = localStorage.getItem("ah.theme"); } catch (_) {}
    setTheme(saved || "auto");
    $$("#theme-seg button").forEach((b) => {
      b.addEventListener("click", () => setTheme(b.getAttribute("data-theme-set")));
    });

    let savedLang = null;
    try { savedLang = localStorage.getItem("ah.lang"); } catch (_) {}
    buildLangSeg();
    applyStrings();
    if (savedLang && savedLang !== LANG) setLang(savedLang);

    $$(".nav-item").forEach((b) => b.addEventListener("click", () => showView(b.getAttribute("data-view"))));
    $("#scan-btn").addEventListener("click", scanVitals);
    $("#scan-deep").addEventListener("change", () => { if (scanned) scanVitals(); });
    $("#run-btn").addEventListener("click", () => startHandoff(false));
    $("#dry-btn").addEventListener("click", () => startHandoff(true));
    $("#find-btn").addEventListener("click", doFind);
    $("#find-q").addEventListener("keydown", (e) => { if (e.key === "Enter") doFind(); });
    $("#repo").addEventListener("keydown", (e) => { if (e.key === "Enter") startHandoff(true); });

    if (BOOT.defaultRepo) $("#repo").value = BOOT.defaultRepo;
    // 令牌通过注入的 bootstrap 传递；从地址栏抹掉，免得它进浏览器历史或被截图带走。
    if (location.search) history.replaceState(null, "", location.pathname);

    scanVitals();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
