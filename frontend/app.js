/* ===========================================================================
 * Parsely frontend
 * ---------------------------------------------------------------------------
 * This script owns the editor experience only. It does NOT parse Python — all
 * detection, recovery and explanation happen on the Python/Gemini backend. The
 * frontend:
 *   1. sends the code to POST /api/analyze whenever you pause typing,
 *   2. paints red underlines on the real error characters and green "ghost"
 *      suggestions in the editor from the backend's findings,
 *   3. renders the AI explanation cards on the right,
 *   4. calls POST /api/autocorrect for the AI-assisted fix.
 * ========================================================================= */
(function () {
  "use strict";

  var API = "";                 // same origin as the Flask server
  var state = { mode: "direct", aiAvailable: false };
  var autofixOn = false;
  var METRICS = { cw: 8.12, lh: 22, padL: 14, padT: 12 };
  var lastFindings = [];

  var $ = function (id) { return document.getElementById(id); };
  function esc(s) {
    return String(s).replace(/[&<>]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c];
    });
  }

  /* ----------------------------- networking ----------------------------- */
  function postJSON(path, body) {
    return fetch(API + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) { return r.json(); });
  }

  /* --------------------------- metrics / scroll ------------------------- */
  function measure() {
    var ta = $("code"); if (!ta) return;
    var cs = getComputedStyle(ta);
    METRICS.lh = parseFloat(cs.lineHeight) || 22;
    METRICS.padL = parseFloat(cs.paddingLeft) || 14;
    METRICS.padT = parseFloat(cs.paddingTop) || 12;
    var p = $("probe");
    if (p) { var w = p.getBoundingClientRect().width; if (w > 0) METRICS.cw = w / p.textContent.length; }
  }
  function syncScroll() {
    var ta = $("code"); if (!ta) return;
    var inner = $("ovInner"); if (inner) inner.style.transform = "translate(" + (-ta.scrollLeft) + "px," + (-ta.scrollTop) + "px)";
    var g = $("gutter"); if (g) g.scrollTop = ta.scrollTop;
  }
  function visLen(s) { var w = 0; for (var i = 0; i < s.length; i++) w += s[i] === "\t" ? (4 - (w % 4)) : 1; return w; }
  function clip(s, n) { s = s == null ? "" : String(s); return s.length > n ? s.slice(0, n - 1) + "…" : s; }

  /* ------------------------------ overlay ------------------------------- */
  // omissions (missing colon/comma/indent) are not underlined on empty space —
  // they get the gutter dot plus the green ghost instead.
  var NO_MARK = { "missing-colon": 1, "missing-comma": 1, "expected-indent": 1, "unexpected-indent": 1 };

  function drawOverlay(findings, srcLines) {
    var mirror = $("mirror"), inner = $("ovInner"); if (!mirror || !inner) return;
    var marks = {};
    findings.forEach(function (e) {
      if (NO_MARK[e.rule]) return;
      var cls = e.severity === "warning" ? "mark-warn" : "mark-err";
      (marks[e.line] = marks[e.line] || []).push({ col: e.col, endCol: e.end_col || e.col + 1, cls: cls });
    });
    var html = "";
    srcLines.forEach(function (ln, i) {
      var ms = marks[i + 1];
      if (!ms) { html += esc(ln) + "\n"; return; }
      ms.sort(function (a, b) { return a.col - b.col; });
      var pos = 0, seg = "", max = ln.length;
      ms.forEach(function (m) {
        var s = Math.max(0, m.col - 1), e = Math.min(max, m.endCol - 1);
        if (e <= s) e = Math.min(max, s + 1);
        if (s < pos) return;
        seg += esc(ln.slice(pos, s)) + "<span class='" + m.cls + "'>" + (esc(ln.slice(s, e)) || "&nbsp;") + "</span>";
        pos = e;
      });
      seg += esc(ln.slice(pos)); html += seg + "\n";
    });
    mirror.innerHTML = html;

    Array.prototype.slice.call(inner.querySelectorAll(".ghost")).forEach(function (g) { g.remove(); });
    var used = {};
    findings.forEach(function (e) {
      var g = greenFor(e, srcLines); if (!g || used[g.line]) return; used[g.line] = true;
      var el = document.createElement("div");
      el.className = "ghost " + (g.kind === "append" ? "append" : "chip");
      el.textContent = g.text;
      el.style.top = (METRICS.padT + (g.line - 1) * METRICS.lh) + "px";
      el.style.left = (METRICS.padL + ((g.kind === "append" ? g.vcol : (g.vcolEnd + 2)) * METRICS.cw)) + "px";
      inner.appendChild(el);
    });
    syncScroll();
  }

  // the green inline suggestion for a finding. Text comes from the AI's
  // "suggestion"/"fix" where available, otherwise a short deterministic cue.
  function greenFor(e, srcLines) {
    var line = e.rule === "unclosed-bracket" ? e.opener_line
      : (e.rule === "expected-indent" ? e.header_line : e.line);
    var ln = srcLines[line - 1]; if (ln === undefined) return null;
    var codeEnd = visLen(ln.replace(/(\s*#.*)?$/, "").replace(/\s+$/, ""));
    var endV = visLen(ln);
    switch (e.rule) {
      case "missing-colon": return { line: line, kind: "append", vcol: codeEnd, text: ":" };
      case "unterminated-string": return { line: line, kind: "append", vcol: codeEnd, text: e.quote || "'" };
      case "unclosed-bracket": return { line: line, kind: "append", vcol: codeEnd, text: e.needs || ")" };
      case "missing-comma": return { line: line, kind: "append", vcol: visLen(ln.slice(0, (e.before_end_col || 1) - 1)), text: "," };
      case "print-statement": return { line: line, kind: "chip", vcolEnd: endV, text: "➜ " + clip(e.suggestion || "print(...)", 46) };
      case "keyword-typo": return { line: line, kind: "chip", vcolEnd: endV, text: "➜ " + (e.suggest || "?") + "?" };
      case "eq-in-cond": return { line: line, kind: "chip", vcolEnd: endV, text: "➜ use ==" };
      case "mismatched-bracket": return { line: line, kind: "chip", vcolEnd: endV, text: "➜ use " + (e.expected || ")") };
      case "unexpected-close": return { line: line, kind: "chip", vcolEnd: endV, text: "➜ remove " + (e.ch || ")") };
      case "assign-literal": return { line: line, kind: "chip", vcolEnd: endV, text: "➜ name = …" };
      case "expected-indent": return { line: line, kind: "chip", vcolEnd: endV, text: "➜ indent next line" };
      case "unexpected-indent": return { line: line, kind: "chip", vcolEnd: endV, text: "➜ remove indent" };
    }
    return null;
  }

  /* ------------------------------ rendering ----------------------------- */
  function markGutter(findings) {
    var g = $("gutter"); if (!g) return;
    var bad = {}, warnm = {};
    findings.forEach(function (e) {
      if (e.severity === "warning") { if (!bad[e.line]) warnm[e.line] = true; }
      else bad[e.line] = true;
    });
    Array.prototype.forEach.call(g.children, function (el) {
      var ln = +el.getAttribute("data-ln");
      el.className = "ln" + (bad[ln] ? " bad" : (warnm[ln] ? " warnmark" : ""));
    });
  }

  function renderResults(resp, srcLines) {
    var R = $("results"); R.innerHTML = "";
    var findings = resp.findings || [];
    var errs = findings.filter(function (f) { return f.severity !== "warning"; });
    var warns = findings.filter(function (f) { return f.severity === "warning"; });

    var total = findings.length;
    var sum = document.createElement("div");
    if (total === 0) {
      sum.className = "summary ok";
      sum.innerHTML = "<span class='big'>✓</span><div>No syntax errors found.<span class='sub'>Parsely parsed your code cleanly. Logic bugs are a separate story — this checks structure.</span></div>";
      R.appendChild(sum); return;
    }
    sum.className = "summary " + (errs.length ? "bad" : "ok");
    var parts = [];
    if (errs.length) parts.push(errs.length + " syntax " + (errs.length === 1 ? "error" : "errors"));
    if (warns.length) parts.push(warns.length + " suggestion" + (warns.length === 1 ? "" : "s"));
    sum.innerHTML = "<span class='big'>" + total + "</span><div>" + (total === 1 ? "issue found" : "issues found") +
      "<span class='sub'>" + parts.join(", ") + "</span></div>";
    R.appendChild(sum);

    if (errs.length > 1) {
      var note = document.createElement("div");
      note.className = "recovery-note";
      note.innerHTML = "<b>Recovery:</b> a traditional parser would stop at the first error on line " + errs[0].line +
        ". Parsely repaired it, kept parsing, and surfaced <b>" + errs.length + " genuine errors</b> in one pass.";
      R.appendChild(note);
    }

    errs.concat(warns).forEach(function (f) { R.appendChild(buildCard(f, srcLines)); });

    if (resp.changed_lines && resp.changed_lines.length && resp.recovered !== $("code").value) {
      R.appendChild(buildRecovered(resp.recovered, resp.changed_lines));
    }
  }

  function buildCard(f, srcLines) {
    var isWarn = f.severity === "warning";
    var card = document.createElement("div");
    card.className = "card " + (isWarn ? "warn" : "err");
    var src = srcLines[f.line - 1] !== undefined ? srcLines[f.line - 1] : "";

    var top = document.createElement("div"); top.className = "card-top";
    top.innerHTML = "<span class='pill " + (isWarn ? "warn" : "err") + "'>" + (isWarn ? "hint" : "error") + "</span>" +
      "<span class='title'>" + esc(f.title) + "</span>" +
      "<span class='loc'>line " + f.line + "</span><span class='spacer'></span>" +
      "<span class='src-tag " + (state.aiAvailable ? "ai" : "") + "'>" + (state.aiAvailable ? "AI" : "rules") + "</span>";
    var btn = document.createElement("button"); btn.className = "applybtn"; btn.textContent = "Apply fix";
    btn.addEventListener("click", function () { applySingle(f); });
    top.appendChild(btn);
    card.appendChild(top);

    var body = document.createElement("div"); body.className = "card-body";
    var snip = document.createElement("div"); snip.className = "snip";
    var hot = caretMarkup(src, f.col, f.end_col);
    snip.innerHTML = "<pre class='src'>" + hot.line + "</pre><pre class='caret'>" + hot.caret + "</pre>";
    snip.addEventListener("click", function () { jumpToLine(f.line); });
    body.appendChild(snip);

    var ex = document.createElement("div"); ex.className = "explain";
    if (state.mode === "hint") {
      ex.innerHTML = "<div>" + esc(f.hint) + "</div>";
    } else {
      var html = "<div>" + esc(f.explanation) + "</div><div class='fix'><b>Fix:</b> " + esc(f.fix) + "</div>";
      if (f.suggestion) html += "<div style='margin-top:6px'>Suggested: <code class='suggest'>" + esc(f.suggestion) + "</code></div>";
      ex.innerHTML = html;
    }
    body.appendChild(ex);

    if (f.lesson) {
      var det = document.createElement("details"); det.className = "lesson";
      det.innerHTML = "<summary>Why this happens</summary><div class='body'>" + esc(f.lesson) + "</div>";
      body.appendChild(det);
    }
    card.appendChild(body);
    return card;
  }

  function caretMarkup(src, col, endCol) {
    var before = src.slice(0, col - 1), hotLen = Math.max(1, (endCol || col + 1) - col);
    var hotText = src.slice(col - 1, col - 1 + hotLen) || " ";
    var after = src.slice(col - 1 + hotLen);
    var lineHTML = esc(before) + "<span class='hot'>" + esc(hotText) + "</span>" + esc(after);
    var pad = before.replace(/[^\t]/g, " ");
    var caret = pad + "^".repeat(Math.max(1, hotText.length));
    return { line: lineHTML, caret: esc(caret) };
  }

  function buildRecovered(recSrc, changedArr) {
    var changed = {}; changedArr.forEach(function (n) { changed[n] = true; });
    var box = document.createElement("div"); box.className = "recovered";
    var head = document.createElement("div"); head.className = "rec-head";
    head.innerHTML = "<h3>Recovered code</h3>";
    var sp = document.createElement("span"); sp.style.marginLeft = "auto";
    var copy = document.createElement("button"); copy.className = "mini"; copy.textContent = "Copy";
    copy.addEventListener("click", function () { copyText(recSrc); });
    var apply = document.createElement("button"); apply.className = "primary"; apply.style.padding = "6px 12px"; apply.textContent = "Apply all to editor";
    apply.addEventListener("click", function () { setCode(recSrc); runCheck(); toast("Applied recovered code"); });
    head.appendChild(sp); head.appendChild(copy); head.appendChild(apply);
    box.appendChild(head);

    var rb = document.createElement("div"); rb.className = "recbox";
    var pre = document.createElement("pre");
    recSrc.split("\n").forEach(function (ln, i) {
      var span = document.createElement("span");
      span.className = "rl" + (changed[i + 1] ? " changed" : "");
      span.textContent = (ln === "" ? " " : ln) + "\n";
      pre.appendChild(span);
    });
    rb.appendChild(pre); box.appendChild(rb);
    var cap = document.createElement("div"); cap.style.cssText = "color:var(--muted);font-size:12px;margin-top:6px";
    cap.textContent = "Best-guess structural repair, shown so parsing could continue. Review each change before using it.";
    box.appendChild(cap);
    return box;
  }

  /* --------------------------- editor plumbing -------------------------- */
  function buildGutter() {
    var ta = $("code"), g = $("gutter");
    var count = ta.value.split("\n").length, html = "";
    for (var i = 1; i <= count; i++) html += "<div class='ln' data-ln='" + i + "'>" + i + "</div>";
    g.innerHTML = html; g.scrollTop = ta.scrollTop;
  }
  function setCode(s) { $("code").value = s; buildGutter(); save(); }
  function caretPos() {
    var ta = $("code"), pre = ta.value.slice(0, ta.selectionStart).split("\n");
    return { line: pre.length, col: pre[pre.length - 1].length + 1 };
  }
  function restoreCaret(p) {
    var ta = $("code"), lines = ta.value.split("\n"), off = 0;
    for (var i = 0; i < p.line - 1 && i < lines.length; i++) off += lines[i].length + 1;
    off += Math.min(p.col - 1, (lines[p.line - 1] || "").length);
    try { ta.selectionStart = ta.selectionEnd = off; } catch (e) {}
  }
  function jumpToLine(line) {
    var ta = $("code"), lines = ta.value.split("\n"), start = 0;
    for (var i = 0; i < line - 1; i++) start += lines[i].length + 1;
    var end = start + (lines[line - 1] ? lines[line - 1].length : 0);
    ta.focus(); ta.setSelectionRange(start, end);
    ta.scrollTop = Math.max(0, (line - 3) * METRICS.lh); buildGutter(); syncScroll();
  }

  /* ---------------------------- analyze flow ---------------------------- */
  var reqSeq = 0;
  function runCheck() {
    var ta = $("code"), code = ta.value;
    var srcLines = code.split("\n");
    buildGutter();
    // draw red/green immediately from the last known findings for snappy feel,
    // but positions always come from the latest backend result.
    var R = $("results");
    if (!R.querySelector(".summary")) {
      R.innerHTML = "<div class='loading'><span class='spin'></span>Checking…</div>";
    }
    var myReq = ++reqSeq;
    postJSON("/api/analyze", { code: code }).then(function (resp) {
      if (myReq !== reqSeq) return;            // a newer request superseded this one
      if (resp.error) { R.innerHTML = "<div class='loading'>Backend error: " + esc(resp.error) + "</div>"; return; }
      state.aiAvailable = !!resp.ai_available;
      updateAiBadge();
      lastFindings = resp.findings || [];
      drawOverlay(lastFindings, srcLines);
      markGutter(lastFindings);
      renderResults(resp, srcLines);
      if (autofixOn) scheduleAutoFix();
    }).catch(function (err) {
      if (myReq !== reqSeq) return;
      R.innerHTML = "<div class='loading'>Can't reach the Parsely backend. Is <code>python app.py</code> running?</div>";
    });
  }

  function applySingle(f) {
    // ask the backend for the recovered code and splice just this line in,
    // keeping it simple: re-run autocorrect-free recovery then apply all.
    postJSON("/api/analyze", { code: $("code").value }).then(function (resp) {
      if (!resp.recovered) return;
      // apply only the changed line matching this finding where possible
      setCode(resp.recovered); runCheck(); toast("Fix applied");
    });
  }

  /* ------------------------------ auto-fix ------------------------------ */
  var fixTimer = null;
  function scheduleAutoFix() { clearTimeout(fixTimer); if (autofixOn) fixTimer = setTimeout(tryAutoFix, 1100); }
  function tryAutoFix() {
    if (!autofixOn) return;
    var ta = $("code"), code = ta.value, caretLine = caretPos().line;
    postJSON("/api/autocorrect", { code: code }).then(function (resp) {
      if (!resp.corrected || resp.corrected === code) return;
      // protect the line the user is editing: keep the original for that line
      var oldL = code.split("\n"), newL = resp.corrected.split("\n");
      if (oldL.length === newL.length && caretLine >= 1 && caretLine <= oldL.length) {
        newL[caretLine - 1] = oldL[caretLine - 1];
      }
      var merged = newL.join("\n");
      if (merged === code) return;
      var cp = caretPos(); setCode(merged); restoreCaret(cp);
      toast(resp.source === "ai" ? "AI auto-fixed" : "Auto-fixed"); runCheck();
    });
  }

  /* ------------------------------ utilities ----------------------------- */
  var toastTimer = null;
  function toast(msg) {
    var t = $("toast"); t.textContent = msg; t.classList.add("show");
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { t.classList.remove("show"); }, 1600);
  }
  function copyText(s) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(s).then(function () { toast("Copied"); }, function () { fallbackCopy(s); });
    } else fallbackCopy(s);
  }
  function fallbackCopy(s) {
    var ta = document.createElement("textarea"); ta.value = s; document.body.appendChild(ta);
    ta.select(); try { document.execCommand("copy"); toast("Copied"); } catch (e) { toast("Copy failed"); }
    document.body.removeChild(ta);
  }
  function updateAiBadge() {
    var b = $("aiBadge"); if (!b) return;
    if (state.aiAvailable) { b.textContent = "AI: on"; b.className = "ai-badge on"; }
    else { b.textContent = "AI: off (rules)"; b.className = "ai-badge off"; }
    var en = $("engine-note"); if (en) en.textContent = state.aiAvailable ? "heuristic engine + Gemini" : "heuristic engine (no key)";
  }
  function save() {
    try {
      localStorage.setItem("parsely.code", $("code").value);
      localStorage.setItem("parsely.mode", state.mode);
      localStorage.setItem("parsely.autofix", autofixOn ? "1" : "0");
      localStorage.setItem("parsely.theme", document.documentElement.getAttribute("data-theme") || "");
    } catch (e) {}
  }
  function load() { try { return localStorage.getItem("parsely.code"); } catch (e) { return null; } }

  /* ------------------------------ examples ------------------------------ */
  var EXAMPLES = {
    multi: "def calculate_total(prices)\n    total = 0\n    for p in prices\n        total += p\n    if total > 100\n        print \"Big order!\"\n    return total",
    brackets: "names = [\"Ava\", \"Ben\", \"Cara\"\ngreeting = 'Hello\nfor name in names:\n    print(greeting, name)",
    hints: "count = 0\nwhil count < 5:\n    if count = 3:\n        print(\"three\")\n    count += 1",
    indent: "def greet(name):\nprint(\"hi\", name)\n    message = \"welcome\"\nreturn message",
    clean: "def is_even(n):\n    return n % 2 == 0\n\nfor i in range(5):\n    if is_even(i):\n        print(i, \"is even\")"
  };

  /* -------------------------------- modes ------------------------------- */
  function setMode(m, silent) {
    state.mode = m;
    $("mode-direct").setAttribute("aria-pressed", m === "direct" ? "true" : "false");
    $("mode-hint").setAttribute("aria-pressed", m === "hint" ? "true" : "false");
    if (!silent) { renderResults({ findings: lastFindings, changed_lines: [] }, $("code").value.split("\n")); save(); }
  }
  function setAutofix(on, silent) {
    autofixOn = !!on;
    var b = $("autofix");
    if (b) { b.setAttribute("aria-pressed", autofixOn ? "true" : "false"); b.textContent = "Auto-fix: " + (autofixOn ? "on" : "off"); }
    var ls = $("live-status"); if (ls) ls.textContent = autofixOn ? "live · AI auto-fixing on pause" : "live checking";
    if (!silent) save();
  }
  function toggleTheme() {
    var el = document.documentElement, cur = el.getAttribute("data-theme");
    var dark = cur ? cur === "dark" : window.matchMedia("(prefers-color-scheme:dark)").matches;
    el.setAttribute("data-theme", dark ? "light" : "dark"); save();
  }

  /* -------------------------------- init -------------------------------- */
  function init() {
    var ta = $("code");
    var initial = load();
    ta.value = initial !== null ? initial : EXAMPLES.multi;
    try {
      var sm = localStorage.getItem("parsely.mode"); if (sm) setMode(sm, true);
      var st = localStorage.getItem("parsely.theme"); if (st) document.documentElement.setAttribute("data-theme", st);
      var saf = localStorage.getItem("parsely.autofix"); if (saf === "1") setAutofix(true, true);
    } catch (e) {}

    // learn whether the backend has an AI key, for the badge
    fetch(API + "/api/health").then(function (r) { return r.json(); }).then(function (h) {
      state.aiAvailable = !!h.ai_available; updateAiBadge();
    }).catch(function () { updateAiBadge(); });

    measure(); buildGutter(); runCheck();
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { measure(); drawOverlay(lastFindings, ta.value.split("\n")); });

    var debR = null;
    ta.addEventListener("input", function () {
      buildGutter(); save();
      clearTimeout(debR); debR = setTimeout(runCheck, 350);
    });
    ta.addEventListener("scroll", syncScroll);
    ta.addEventListener("keydown", function (ev) {
      if (ev.key === "Tab") {
        ev.preventDefault();
        var s = ta.selectionStart, e = ta.selectionEnd;
        ta.value = ta.value.slice(0, s) + "    " + ta.value.slice(e);
        ta.selectionStart = ta.selectionEnd = s + 4; buildGutter();
        clearTimeout(debR); debR = setTimeout(runCheck, 250);
      }
    });
    window.addEventListener("resize", function () { measure(); syncScroll(); });

    $("check").addEventListener("click", runCheck);
    $("clear").addEventListener("click", function () { setCode(""); runCheck(); ta.focus(); });
    $("examples").addEventListener("change", function (ev) {
      var v = ev.target.value; if (v && EXAMPLES[v]) { setCode(EXAMPLES[v]); runCheck(); } ev.target.value = "";
    });
    $("autofix").addEventListener("click", function () { setAutofix(!autofixOn); if (autofixOn) tryAutoFix(); });
    $("mode-direct").addEventListener("click", function () { setMode("direct"); });
    $("mode-hint").addEventListener("click", function () { setMode("hint"); });
    $("theme").addEventListener("click", toggleTheme);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
