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
    // 无障碍标签也要跟着切；屏幕阅读器用户否则永远听到英文。
    $$("[data-i18n-aria]").forEach((n) => { n.setAttribute("aria-label", t(n.getAttribute("data-i18n-aria"))); });
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
      // 侧栏窄，用短标签；完整名字放 title。两者都由服务端按各自语言给出。
      const b = el("button", { text: l.short || l.code, title: l.name, "aria-pressed": String(l.code === LANG) });
      b.addEventListener("click", () => setLang(l.code));
      seg.appendChild(b);
    });
  }

  // 切语言后要跟着更新的零散东西（目前是说明文档链接上的 #lang=）。
  // 用订阅而不是在 setLang 里直接写死：那样每加一处就要改 setLang，
  // 而漏改的表现是「切了语言但某处还是旧语言」——正是这轮修的那类缺陷。
  const langHooks = [];
  function onLangChange(fn) { langHooks.push(fn); }

  async function setLang(code) {
    if (code === LANG) return;
    try {
      const data = await api("/api/strings?lang=" + encodeURIComponent(code));
      S = data.strings; LANG = data.lang;
    } catch (e) { return; }
    try { localStorage.setItem("ah.lang", LANG); } catch (_) {}
    buildLangSeg(); applyStrings(); rerender();
    langHooks.forEach((fn) => { try { fn(); } catch (_) {} });
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
    // 只管真正的 tab。说明文档那一项是外部链接（没有 data-view），
    // 给它设 aria-selected 会让读屏软件把一个链接念成未选中的标签页。
    $$(".nav-item[data-view]").forEach((b) => {
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

  /* 勾选要传承的会话。键是转录路径——它在本机唯一，而会话 ID 在
     Claude 的子代理转录里会与父会话重复。 */
  const picked = new Set();

  /* 上下文占用。三种情况的可信程度不同，措辞也不同：
       · 压缩过    —— 最硬的证据：自动压缩只在快装不下时触发，说明真顶到过上限
       · 有上限    —— 报真占用率（Codex 在转录里写了 model_context_window）
       · 没有上限  —— 只报占用量，不编一个分母出来（Claude 转录不写上限）
     读不到 token 时返回 null，卡片就只显示体积——那是兜底判据。 */
  function fullnessChip(r) {
    if (r.compactions) {
      const txt = t("gui.label.compacted") + " " + r.compactions + "x";
      return el("span.hot", { title: t("gui.tip.compacted", { count: r.compactions, tokens: fmtNum(r.tokens) }) },
        el("b", { text: txt }));
    }
    if (!r.tokens) return null;
    if (r.context_window) {
      const pct = Math.round(r.tokens * 100 / r.context_window);
      const cls = pct >= 90 ? "hot" : "";
      return el("span", { class: cls, title: t("gui.tip.fullness", { pct: pct, tokens: fmtNum(r.tokens), window: fmtNum(r.context_window) }) },
        el("b", { text: pct + "%" }), " " + t("gui.label.context"));
    }
    return el("span", { title: t("gui.tip.tokens", { tokens: fmtNum(r.tokens) }) },
      el("b", { text: fmtNum(r.tokens) }), " " + t("gui.label.tokens"));
  }

  function fmtNum(n) {
    // 千分位。Intl 在所有目标浏览器里都有，但它会按 locale 变分隔符，
    // 而这里要的是稳定可比的数字，所以自己插。
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  function sessionCard(r, opts) {
    const card = el("div.srow", { "data-band": r.band });
    const top = el("div.srow-top");
    // 组内不重复 APP 名（组标题已经说了）；找会话视图里没有分组，要显示。
    if (opts && opts.showAgent) top.appendChild(el("span.agent", { text: r.agent }));
    top.appendChild(el("span.badge", { text: t("band." + r.band), "data-band": r.band }));
    // 外来转录：路径在本机无效、也续接不了。徽章紧跟判定，因为它同样影响
    // 「这个会话我该怎么处理」——只是原因不同。
    if (r.is_foreign) {
      top.appendChild(el("span.badge.badge-foreign", {
        text: t("gui.label.foreign"), title: t("gui.tip.foreign"),
      }));
    }
    top.appendChild(el("span.when", { text: ago(r.mtime), title: r.mtime_text }));

    const m = el("div.metrics");
    // 占用排在体积前面：它才是判定的主依据。体积与占用严重脱钩——
    // 实测 1.0 MB 的会话可以已用 194183 token，2.0 MB 的会话可以压缩过 10 次。
    const fullness = fullnessChip(r);
    if (fullness) m.appendChild(fullness);
    m.appendChild(el("span", null, el("b", { text: r.mb.toFixed(1) }), " MB"));
    m.appendChild(el("span", { class: r.fatal ? "hot" : "" }, t("gui.label.fatal") + " ", el("b", { text: String(r.fatal) })));
    // 中断轮次只在真的发生过时才占位置：半成品看起来和完成品一样，
    // 按「已完成」继续做下去就会把没做完的工作当成做完的。
    if (r.aborted) {
      m.appendChild(el("span.hot", null, t("gui.label.aborted") + " ", el("b", { text: String(r.aborted) })));
    }
    m.appendChild(el("span", null, t("gui.label.errors") + " ", el("b", { text: String(r.errors) })));
    top.appendChild(m);
    card.appendChild(top);

    // 话题放在最显眼处：会话 ID 前八位对人没有意义，而开场提问在斜杠命令
    // 回显时对所有会话都一样。话题来自 AI 标题或会话自己写的压缩摘要。
    //
    // 话题与提问都是**会话原文**，语言由当时的对话决定，不跟随界面语言——
    // 翻译它们等于篡改证据。所以标成引文而不是普通界面文字，让「英文界面里
    // 出现中文」一眼看得出是引用，不是没翻译。
    if (r.label) {
      card.appendChild(el("p.stopic.is-quote", {
        text: r.label, title: r.label, lang: "", "data-verbatim": t("gui.label.verbatim"),
      }));
    }

    const kv = el("dl.kv");
    const row = (k, v, cls) => {
      if (!v) return;
      kv.appendChild(el("dt", { text: k }));
      // 长文本夹两行 + title 悬停看全文：一屏能扫过更多会话卡片。
      const dd = el("dd", { text: v, class: cls || "", title: cls && cls.indexOf("clamp") >= 0 ? v : null });
      // 引文标 lang=""：告诉浏览器与读屏软件「这段的语言未知、不是页面语言」，
      // 避免读屏用英文腔去念中文原话。
      if (cls && cls.indexOf("prose") >= 0) dd.setAttribute("lang", "");
      kv.appendChild(dd);
    };
    row(t("gui.label.session"), r.session_id || "—");
    row(t("gui.label.thread"), r.thread_id);
    row(t("gui.label.mtime"), r.mtime_text);
    row(t("gui.label.cwd"), r.cwd);
    row(t("gui.label.client"), [r.version, r.origin].filter(Boolean).join(" "));
    /* 判定依据。卡片上会同时出现「谁写的」与「在谈论谁」——一个 Claude Code
       会话完全可以整篇在分析某个 Codex 会话，开场提问里就带 codex://threads/…。
       只标 APP 名不给依据，读者会以为标错了。所以把转录的存放位置摆出来，
       它是判定的唯一来源，也让人能自己核实。 */
    if (r.agent_evidence) row(t("gui.label.evidence"), r.agent_evidence);
    row(t("gui.label.last_prompt"), r.last_prompt, "prose clamp");
    if (!r.last_prompt) row(t("gui.label.first_prompt"), r.first_prompt, "prose clamp");
    if (r.repos && r.repos.length) {
      const extra = r.repos.length > 1 ? t("cli.card.repos_more", { count: r.repos.length - 1 }) : "";
      row(t("gui.label.repos"), r.repos[0] + extra);
    }
    row(t("gui.label.file"), r.path);
    card.appendChild(kv);

    const act = el("div.srow-act");
    // 只有带摘要或带话题的会话才值得传承：没有这两样时，能传下去的只有
    // 一个文件路径，对新会话没有信息量。
    if ((r.digest || r.label) && !(opts && opts.noPick)) {
      const box = el("label.pick");
      const cb = el("input", { type: "checkbox" });
      cb.checked = picked.has(r.path);
      cb.onchange = () => {
        if (cb.checked) picked.add(r.path); else picked.delete(r.path);
        const n = $("#pick-count");
        if (n) n.textContent = t("cli.sessions.picked", { count: picked.size }).trim();
      };
      box.appendChild(cb);
      box.appendChild(el("span", { text: r.digest
        ? t("cli.card.digest", { chars: r.digest.length }).trim()
        : t("gui.sessions.pick") }));
      act.appendChild(box);
    }
    if (r.repo && !(opts && opts.noAction)) {
      act.appendChild(el("button.link", {
        text: t("gui.find.use"),
        onclick: () => { $("#repo").value = r.repo; showView("handoff"); $("#repo").focus(); }
      }));
    }
    // 原生续接严格优于交接：交接是有损的（工具授权、后台进程、被否决方案的
    // 推理都传不过去）。只要还能原生续接就先给那条路——把命令复制走比在这里
    // 生成一份有损摘要更好。归档过的 Codex 会话续接不了，resume_cmd 为空。
    if (r.resume_cmd) {
      const btn = el("button.link.link-quiet", {
        text: t("gui.label.resume"),
        title: t("gui.tip.resume", { cmd: r.resume_cmd }),
      });
      btn.onclick = () => copyText(r.resume_cmd, btn, t("gui.label.resume"));      act.appendChild(btn);
    }
    /* 逐项复制。四样东西对应四个不同的下一步动作，所以分开给而不是塞进一个
       「复制全部」：
         · 工作目录 —— 粘进终端 cd 过去
         · 会话 ID   —— 贴进 issue、或喂给 --find
         · 深度链接 —— 直接唤起 APP 回到那条线程（只有 Codex 注册了 scheme）
         · Markdown  —— 把整段对话粘给新会话，交接的主力产出
       Markdown 要现取（会话正文可能几十万字符），所以它是异步的。 */
    if (!(opts && opts.noAction)) {
      if (r.cwd) {
        const b = el("button.link.link-quiet", { text: t("gui.copy.cwd") });
        b.onclick = () => copyText(r.cwd, b, t("gui.copy.cwd"));
        act.appendChild(b);
      }
      if (r.session_id) {
        const b = el("button.link.link-quiet", { text: t("gui.copy.id") });
        b.onclick = () => copyText(r.session_id, b, t("gui.copy.id"));
        act.appendChild(b);
      }
      if (r.deep_link) {
        const b = el("button.link.link-quiet", { text: t("gui.copy.deeplink") });
        b.onclick = () => copyText(r.deep_link, b, t("gui.copy.deeplink"));
        act.appendChild(b);
      }
      const md = el("button.link.link-quiet", { text: t("gui.copy.markdown") });
      md.onclick = async () => {
        md.disabled = true;
        try {
          const data = await api("/api/session-md?path=" + encodeURIComponent(r.path));
          copyText(data.markdown || "", md, t("gui.copy.markdown"));
        } catch (e) {
          md.textContent = t("gui.err.title");
          setTimeout(() => { md.textContent = t("gui.copy.markdown"); }, 1600);
        } finally {
          md.disabled = false;
        }
      };
      act.appendChild(md);
    }
    if (act.childNodes.length) card.appendChild(act);
    return card;
  }

  /* 复制一段文本并就地反馈。剪贴板 API 在非安全上下文里不可用（http 的
     127.0.0.1 算安全上下文，但用户可能通过别的主机名访问），所以留一条
     选区回退，而不是静默失败。 */
  function copyText(text, btn, restore) {
    const done = () => {
      btn.textContent = t("gui.label.copied");
      setTimeout(() => { btn.textContent = restore; }, 1600);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, () => fallback());
      return;
    }
    fallback();
    function fallback() {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); done(); } catch (e) { /* 复制不了就算了 */ }
      document.body.removeChild(ta);
    }
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
  // 上次查找命中的仓库集合。切语言重渲染时要保留，否则跨项目的提示会消失，
  // 而那条提示恰恰决定用户该不该把这些会话一起交接。
  let lastFindRepos = [];

  async function doFind() {
    const q = $("#find-q").value.trim();
    const out = clear($("#find-out"));
    if (!q) { out.appendChild(errPanel(t("gui.err.no_input"))); return; }
    out.appendChild(el("div.card", null, el("div.progress", null, el("span"))));
    try {
      const data = await api("/api/find?q=" + encodeURIComponent(q));
      lastFind = data.rows || [];
      lastFindRepos = data.repos || [];
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
    /* 找到的会话横跨多个仓库时先说清楚：交接固化的是**一个仓库**的状态，
       把分属不同项目的会话汇总进一份提示词，等于让新会话面对两个现场。
       同一个仓库的多个会话才是「一起交接」的正常用法。 */
    const repos = lastFindRepos || [];
    if (repos.length > 1) {
      out.appendChild(el("div.card.warn", null,
        el("p", { text: t("gui.find.multi_repo", { count: repos.length }) }),
        el("ul", null, ...repos.map((p) => el("li", null, el("code", { text: p }))))));
    }
    rows.forEach((r) => out.appendChild(sessionCard(r, { showAgent: true })));
  }

  /* ── 交接 ──────────────────────────────────────────────── */
  let lastResult = null;
  let lastDry = false;
  let polling = null;
  let lastLog = "";   // 切语言要原地重绘，日志只存在于 DOM 里会被重绘丢掉
  // 上一次运行的请求（不含 lang）。切语言时带着它重跑，让服务端用新语言重新
  // 渲染提示词与环境陷阱——那些是服务端字符串，重绘界面标签改不动它们。
  let lastRunReq = null;

  function errPanel(msg) {
    return el("div.panel.is-danger", null,
      el("div.panel-h", null, t("gui.err.title")),
      el("div.panel-b", null, el("p", { text: msg })));
  }

  // 附着到一个**已存在**的任务，不新建。截图脚本用它：任务在浏览器启动前就
  // 由脚本跑完了，页面只负责渲染结果——否则虚拟时间会把捕获停在进度行上。
  function attachJob(jobId, dry) {
    const out = clear($("#handoff-out"));
    lastDry = dry;
    const logPre = el("pre.log");
    out.appendChild(el("div.panel", null,
      el("div.panel-h", null,
        dry ? el("span.badge.badge-dry", { text: t("gui.badge.dry") }) : null,
        el("span", { text: t("gui.handoff.running") })),
      el("div.panel-b", null, el("div.progress", null, el("span")), logPre)));
    pollJob(jobId, logPre, 0);
  }

  // 轮询一个任务直到它结束。抽出来是因为「新建任务」与「附着到已有任务」
  // 只在前半段不同，后半段的日志追加与结果渲染必须逐字一致。
  function pollJob(jobId, logPre, since) {
    let misses = 0;   // 连续轮询失败次数：服务端没了要说出来，不能一直转圈
    polling = setInterval(async () => {
      let st;
      try {
        st = await api("/api/job?id=" + encodeURIComponent(jobId) + "&since=" + since);
      } catch (e) {
        // 单次失败可能只是一次抖动，连续失败则是服务端已经不在了。
        // 静默 return 会让进度条永远转下去，用户看不出发生了什么。
        if (++misses < 5) return;
        clearInterval(polling); polling = null;
        $("#run-btn").disabled = false; $("#dry-btn").disabled = false;
        $("#handoff-out").prepend(errPanel(t("gui.err.poll_lost")));
        return;
      }
      misses = 0;
      if (st.log && st.log.length) {
        since = st.next;
        logPre.textContent += st.log.join("\n") + "\n";
        lastLog = logPre.textContent;
        logPre.scrollTop = logPre.scrollHeight;
      }
      if (st.state === "running") return;
      clearInterval(polling); polling = null;
      $("#run-btn").disabled = false; $("#dry-btn").disabled = false;
      lastResult = st.result;
      lastLog = logPre.textContent;
      renderResult(st.state, logPre.textContent);
    }, 450);
  }

  async function startHandoff(dry, opts) {
    if (polling) return;
    const rerun = !!(opts && opts.rerun);
    const repo = rerun && lastRunReq ? lastRunReq.repo : $("#repo").value.trim();
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

    // 换语言重跑时不再动仓库：用户要的只是同一份结果换个语言，
    // 不该因此多出一个提交，也不该再回填一次计划文档。
    const req = rerun
      ? Object.assign({}, lastRunReq, { no_commit: true })
      : {
          repo: repo, dry_run: dry,
          skip_tests: $("#o-skip-tests").checked,
          no_commit: $("#o-no-commit").checked,
          no_vitals: $("#o-no-vitals").checked,
          force: $("#o-force").checked,
          // 勾了就打包到默认位置（~/.agent-handoff/bundles/）。界面上不给路径输入框：
          // 默认位置刻意在仓库外，而让用户在网页里填任意写入路径会把这个保护绕掉。
          export_bundle: $("#o-bundle").checked,
          // 体检视图里勾选的会话。它们的摘要会写进交接文件，提示词会点名它们。
          sessions: Array.from(picked)
        };
    lastRunReq = req;

    let job;
    try {
      // lang 每次都取当前值，不进 lastRunReq——它是「用什么语言渲染」，
      // 不是「对哪个仓库做什么」，把它存进请求会在下次重跑时用回旧语言。
      job = await api("/api/handoff", { body: Object.assign({ lang: LANG }, req) });
    } catch (e) {
      clear(out).appendChild(errPanel(e.message));
      $("#run-btn").disabled = false; $("#dry-btn").disabled = false;
      return;
    }

    pollJob(job.job, logPre, 0);
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
      // 列名走各自的 key。原先从 doc.table.head 里 split("|") 抠字段，
      // 文案改列序或列里出现竖线就会错位，而且第一列还漏成了硬编码英文。
      [t("gui.col.task"), t("gui.label.steps"), t("gui.col.file_evidence"), t("gui.col.symbol_evidence"), ""].forEach((h, i) => {
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

  /* 切语言后原地重绘已有结果，不丢状态。

     界面标签可以直接重绘，但**产出**不行：提示词、环境陷阱、交接文档正文
     都是服务端在运行那一刻按当时语言渲染好的字符串，存在 lastResult 里。
     只重绘会得到中英混排——切到 EN 之后「Environment」标题下面仍然是
     「可用解释器：」，提示词整段还是中文。那不是英国人能用的东西。

     所以带着同一份请求重跑，让服务端用新语言重新渲染。重跑是安全的：
     这次刻意 no_commit + 不回填计划（rerun 标记），用户要的只是换语言看
     同一份结果，不该因为切语言而多出一个提交。 */
  async function rerender() {
    if (lastVitals) renderVitals();
    if (lastFind) renderFind();
    if (!lastResult) return;
    if (lastResult.error || !lastRunReq) {
      renderResult(lastResult.error ? "error" : "done", lastLog);
      return;
    }
    await startHandoff(lastDry, { rerun: true });
  }

  /* ── 启动 ──────────────────────────────────────────────── */
  function init() {
    $("#ver").textContent = BOOT.version;

    // hash 里的主题优先于记住的选择：截图脚本靠它固定主题，而浏览器 profile
    // 可能留着上一次运行存下的 ah.theme，那会让「浅色」这一张拍成深色。
    const hashQ = location.hash ? new URLSearchParams(location.hash.slice(1)) : null;
    const forced = hashQ && hashQ.get("theme");
    let saved = null;
    try { saved = localStorage.getItem("ah.theme"); } catch (_) {}
    setTheme((forced === "light" || forced === "dark" || forced === "auto")
      ? forced : (saved || "auto"));
    $$("#theme-seg button").forEach((b) => {
      b.addEventListener("click", () => setTheme(b.getAttribute("data-theme-set")));
    });

    // 语言与主题同理：显式指定的优先于记住的选择。服务端已按 ?lang= 渲染好
    // 首屏（BOOT.lang），此时再套用 localStorage 会把显式要求的语言顶掉；
    // #lang= 同样要能压过记住的值，否则截图脚本拍不出指定语言那一版。
    const forcedLang = hashQ && hashQ.get("lang");
    const known = (BOOT.langs || []).some((l) => l.code === forcedLang);
    let savedLang = null;
    try { savedLang = localStorage.getItem("ah.lang"); } catch (_) {}
    buildLangSeg();
    applyStrings();
    const explicit = (known && forcedLang) || (BOOT.langExplicit ? LANG : null);
    const want = explicit || savedLang;
    if (want && want !== LANG) setLang(want);
    else if (explicit) { try { localStorage.setItem("ah.lang", explicit); } catch (_) {} }

    // 只给真正的 tab 绑切换。说明文档是 <a href> 外部链接，绑上去会让
    // showView(null) 把三个视图全部隐藏。
    $$(".nav-item[data-view]").forEach((b) => b.addEventListener("click", () => showView(b.getAttribute("data-view"))));
    // 说明文档只在仓库里跑时存在；装好的包不带 docs/，那时不显示这个入口。
    const guide = $("#nav-guide");
    if (guide && BOOT.guideAvailable) {
      guide.hidden = false;
      // 语言跟着界面走：说明文档自己认 #lang=，否则英文用户点开会看到简体。
      const sync = () => { guide.setAttribute("href", "/guide.html#lang=" + LANG); };
      sync();
      onLangChange(sync);
    }
    $("#scan-btn").addEventListener("click", scanVitals);
    $("#scan-deep").addEventListener("change", () => { if (scanned) scanVitals(); });
    $("#run-btn").addEventListener("click", () => startHandoff(false));
    $("#dry-btn").addEventListener("click", () => startHandoff(true));

    /* 运行前明示这一次会发生什么。
       复选框的文案说的是「关掉什么」（「不提交，只分析」），读者得自己反推
       「没勾就是会提交」——而提交是不可逆的写操作。这一行把它正过来说，
       并且随勾选实时更新，所以按下按钮之前看到的就是即将发生的事。 */
    const opts = ["o-skip-tests", "o-no-commit", "o-no-vitals", "o-force", "o-bundle"];
    const syncWill = () => {
      const node = $("#will");
      if (!node) return;
      const willCommit = !$("#o-no-commit").checked;
      const parts = [t(willCommit ? "gui.will.commit" : "gui.will.no_commit")];
      parts.push(t($("#o-skip-tests").checked ? "gui.will.no_tests" : "gui.will.tests"));
      if ($("#o-force").checked) parts.push(t("gui.will.force"));
      if ($("#o-bundle").checked) parts.push(t("gui.will.bundle"));
      node.textContent = t("gui.will.head") + " " + parts.join(t("gui.will.sep"));
      node.classList.toggle("will-write", willCommit);
    };
    opts.forEach((id) => {
      const box = $("#" + id);
      if (box) box.addEventListener("change", syncWill);
    });
    syncWill();
    onLangChange(syncWill);

    $("#find-btn").addEventListener("click", doFind);
    $("#find-q").addEventListener("keydown", (e) => { if (e.key === "Enter") doFind(); });
    $("#repo").addEventListener("keydown", (e) => { if (e.key === "Enter") startHandoff(true); });

    if (BOOT.defaultRepo) $("#repo").value = BOOT.defaultRepo;
    // 令牌通过注入的 bootstrap 传递；从地址栏抹掉，免得它进浏览器历史或被截图带走。
    if (location.search) history.replaceState(null, "", location.pathname);

    // 截图脚本用 #view=handoff&job=<id> 附着到一个已经跑完的任务，
    // 让交接结果那一屏有内容可拍。只读地渲染，不会新建任务、不会提交。
    const view = hashQ && hashQ.get("view");
    if (view && $(`.nav-item[data-view="${view}"]`)) showView(view);
    const attach = hashQ && hashQ.get("job");
    if (attach) {
      attachJob(attach, true);
    } else {
      scanVitals();
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
