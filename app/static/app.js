/* Frappe Docker Manager - shared front-end helpers (no build step). */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  async function api(url, { method = "GET", body, raw = false } = {}) {
    const opts = { method, headers: {} };
    if (body instanceof FormData) opts.body = body;
    else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
    const res = await fetch(url, opts);
    const type = res.headers.get("content-type") || "";
    const data = type.includes("json") ? await res.json() : await res.text();
    if (!res.ok) {
      const msg = (data && (data.error || data.detail)) || (typeof data === "string" ? data : res.statusText);
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return raw ? res : data;
  }

  function toast(msg, kind = "") {
    let box = $(".toasts");
    if (!box) { box = document.createElement("div"); box.className = "toasts"; document.body.appendChild(box); }
    const t = document.createElement("div");
    t.className = `toast ${kind}`;
    t.textContent = msg;
    box.appendChild(t);
    setTimeout(() => t.remove(), kind === "err" ? 9000 : 4500);
  }

  function confirmAction(message, { danger = false, okText = "Continue" } = {}) {
    return new Promise((resolve) => {
      const d = document.createElement("dialog");
      d.innerHTML = `<p style="white-space:pre-wrap">${esc(message)}</p>
        <div class="row" style="justify-content:flex-end;margin-top:16px">
          <button class="btn" value="no">Cancel</button>
          <button class="btn ${danger ? "danger solid" : "primary"}" value="yes">${esc(okText)}</button></div>`;
      document.body.appendChild(d);
      d.addEventListener("click", (e) => { if (e.target.value) { d.close(e.target.value); } });
      d.addEventListener("close", () => { resolve(d.returnValue === "yes"); d.remove(); });
      d.showModal();
    });
  }

  /* ---------------- job console ---------------- */
  function renderLine(line, showTs = true) {
    const div = document.createElement("div");
    div.className = `l-${line.level}`;
    div.innerHTML = (showTs ? `<span class="ts">${String(line.t.toFixed ? line.t.toFixed(1) : line.t).padStart(6)}s</span>` : "") + esc(line.text);
    return div;
  }

  function fmtDuration(sec) {
    const s = Math.max(0, Math.round(sec));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    const pad = (n) => String(n).padStart(2, "0");
    if (h) return `${h}h ${pad(m)}m ${pad(r)}s`;
    return m ? `${m}m ${pad(r)}s` : `${r}s`;
  }

  /** Stream a job's log into consoleEl; if timerEl is given, show a live elapsed / final duration. */
  function streamJob(jobId, consoleEl, { onDone, onStatus, timerEl } = {}) {
    consoleEl.innerHTML = "";
    let tick = null;
    const stopTimer = () => { if (tick) { clearInterval(tick); tick = null; } };
    const es = new EventSource(`/api/jobs/${jobId}/stream`);
    es.addEventListener("meta", (ev) => {
      // The server replays the whole log on (re)connect, so start from a clean console.
      consoleEl.innerHTML = "";
      const job = JSON.parse(ev.data);
      if (!timerEl) return;
      stopTimer();
      if (job.status !== "running") { timerEl.textContent = `⏱ ${job.duration_text}`; return; }
      const base = job.elapsed, t0 = performance.now();
      const paint = () => { timerEl.textContent = `⏱ ${fmtDuration(base + (performance.now() - t0) / 1000)}`; };
      paint();
      tick = setInterval(paint, 1000);
    });
    es.onmessage = (ev) => {
      const atBottom = consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight < 60;
      consoleEl.appendChild(renderLine(JSON.parse(ev.data)));
      if (atBottom) consoleEl.scrollTop = consoleEl.scrollHeight;
    };
    es.addEventListener("done", (ev) => {
      es.close();
      stopTimer();
      const job = JSON.parse(ev.data);
      if (timerEl) timerEl.textContent = `⏱ ${job.duration_text}`;
      onStatus && onStatus(job.status);
      onDone && onDone(job);
    });
    es.onerror = () => { /* EventSource reconnects automatically; the server replays the log */ };
    const close = es.close.bind(es);
    es.close = () => { stopTimer(); close(); };
    return es;
  }

  const drawer = {
    el: null, es: null,
    ensure() {
      if (this.el) return this.el;
      const el = document.createElement("div");
      el.className = "drawer";
      el.innerHTML = `<div class="drawer-head"><span class="pill running js-status">running</span><strong class="js-title"></strong>
        <span class="muted small mono js-timer" title="Elapsed time"></span>
        <a class="btn sm ghost js-open" target="_blank" title="Open in new tab">↗</a>
        <button class="btn sm ghost js-min" title="Minimise">–</button><button class="btn sm ghost js-close" title="Close">✕</button></div>
        <div class="console js-console"></div>`;
      document.body.appendChild(el);
      $(".js-close", el).onclick = () => { el.classList.remove("open"); this.es && this.es.close(); };
      $(".js-min", el).onclick = () => el.classList.toggle("min");
      this.el = el;
      return el;
    },
    open(jobId, title, onDone) {
      const el = this.ensure();
      this.es && this.es.close();
      el.classList.add("open"); el.classList.remove("min");
      $(".js-title", el).textContent = title || "Task";
      $(".js-open", el).href = `/jobs/${jobId}`;
      const status = $(".js-status", el);
      status.className = "pill running js-status"; status.innerHTML = `running`;
      $(".js-timer", el).textContent = "";
      this.es = streamJob(jobId, $(".js-console", el), {
        timerEl: $(".js-timer", el),
        onStatus: (s) => { status.className = `pill ${s} js-status`; status.textContent = s; },
        onDone: (job) => {
          toast(`${job.title}: ${job.status} in ${job.duration_text}`, job.status === "success" ? "ok" : "err");
          onDone && onDone(job);
        },
      });
    },
  };

  /** POST to an endpoint that returns {job_id} and show its live output. */
  async function runJob(url, body, { title, onDone } = {}) {
    try {
      const res = await api(url, { method: "POST", body: body ?? {} });
      drawer.open(res.job_id, title || res.title, onDone);
      return res;
    } catch (e) { toast(e.message, "err"); throw e; }
  }

  /* ---------------- tabs ---------------- */
  function initTabs(root = document) {
    $$("[data-tabs]", root).forEach((tabs) => {
      const buttons = $$("button[data-tab]", tabs);
      const activate = (name, push = true) => {
        buttons.forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
        $$(".tab-panel", tabs.parentElement).forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
        if (push) history.replaceState(null, "", `#${name}`);
        document.dispatchEvent(new CustomEvent("tab:shown", { detail: name }));
      };
      buttons.forEach((b) => b.addEventListener("click", () => activate(b.dataset.tab)));
      const initial = location.hash.slice(1);
      activate(buttons.some((b) => b.dataset.tab === initial) ? initial : buttons[0].dataset.tab, false);
    });
  }

  /* ---------------- copy buttons ---------------- */
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-copy]");
    if (!btn) return;
    const text = btn.dataset.copy || $(btn.dataset.copyFrom)?.textContent || "";
    try { await navigator.clipboard.writeText(text.trim()); toast("Copied", "ok"); }
    catch { toast("Copy failed - select the text manually", "err"); }
  });

  /* ---------------- log viewer ---------------- */
  function logViewer(consoleEl, urlFor) {
    let es = null;
    const stop = () => { if (es) { es.close(); es = null; } };
    return {
      async load(source, lines) {
        stop();
        consoleEl.textContent = "Loading…";
        try { consoleEl.textContent = await api(urlFor(source, lines, false)); }
        catch (e) { consoleEl.textContent = e.message; }
        consoleEl.scrollTop = consoleEl.scrollHeight;
      },
      follow(source, lines) {
        stop();
        consoleEl.textContent = "";
        es = new EventSource(urlFor(source, lines, true));
        es.onmessage = (ev) => {
          const atBottom = consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight < 60;
          consoleEl.appendChild(document.createTextNode(JSON.parse(ev.data) + "\n"));
          if (consoleEl.childNodes.length > 6000) consoleEl.removeChild(consoleEl.firstChild);
          if (atBottom) consoleEl.scrollTop = consoleEl.scrollHeight;
        };
        es.addEventListener("done", (ev) => { consoleEl.appendChild(document.createTextNode(JSON.parse(ev.data) + "\n")); stop(); });
      },
      stop,
      get following() { return !!es; },
    };
  }

  window.FDM = { $, $$, esc, api, toast, confirmAction, runJob, drawer, streamJob, initTabs, logViewer, renderLine };
  document.addEventListener("DOMContentLoaded", () => initTabs());
})();
