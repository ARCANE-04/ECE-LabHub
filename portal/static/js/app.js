(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const loader = $("#pageLoader");
  const startLoading = () => loader && loader.classList.add("run");

  // Mobile nav
  $$("[data-nav-toggle]").forEach((b) => b.addEventListener("click", () => {
    const nav = $("#mainNav"); const open = nav.classList.toggle("open");
    b.setAttribute("aria-expanded", open ? "true" : "false");
  }));

  // Flash messages: dismiss + auto-hide non-errors
  $$("[data-flash]").forEach((f) => {
    const close = () => { f.style.transition = "opacity .25s"; f.style.opacity = "0"; setTimeout(() => f.remove(), 260); };
    $("[data-dismiss]", f)?.addEventListener("click", close);
    if (!f.classList.contains("danger")) setTimeout(close, 6000);
  });

  // Password visibility
  $$("[data-toggle-password]").forEach((btn) => btn.addEventListener("click", () => {
    const input = document.getElementById(btn.dataset.togglePassword);
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    btn.setAttribute("aria-pressed", show ? "true" : "false");
    btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
    $(".show", btn).style.display = show ? "none" : "";
    $(".hide", btn).style.display = show ? "" : "none";
  }));

  // Confirmation prompts
  $$("form[data-confirm]").forEach((f) => f.addEventListener("submit", (e) => {
    if (!confirm(f.dataset.confirm)) { e.preventDefault(); e.stopImmediatePropagation(); }
  }));

  // Client-side validation for auth forms (server validates again)
  const domains = ["iitr.ac.in"];
  const isIITR = (v) => { const d = (v.split("@")[1] || "").toLowerCase(); return domains.some((x) => d === x || d.endsWith("." + x)); };
  const setErr = (input, msg) => {
    const box = input.form.querySelector(`[data-error-for="${input.name}"]`);
    input.closest(".field")?.classList.toggle("invalid", !!msg);
    if (box) { box.hidden = !msg; box.textContent = msg || ""; }
  };
  $$("form[data-validate]").forEach((form) => {
    form.addEventListener("submit", (e) => {
      let ok = true;
      $$("input[required]", form).forEach((i) => {
        let msg = "";
        if (!i.value.trim()) msg = "This field is required.";
        else if (i.type === "email" && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(i.value)) msg = "Enter a valid email address.";
        else if (i.hasAttribute("data-iitr-email") && !isIITR(i.value)) msg = "Use your IIT Roorkee institutional email (…@iitr.ac.in).";
        else if (i.minLength > 0 && i.value.length < i.minLength) msg = `Must be at least ${i.minLength} characters.`;
        if (msg) ok = false;
        if (i.form.querySelector(`[data-error-for="${i.name}"]`)) setErr(i, msg);
        else if (msg) i.closest(".field")?.classList.add("invalid");
      });
      const p1 = form.querySelector("#password"), p2 = form.querySelector("#password2");
      if (p1 && p2 && p1.value !== p2.value) { ok = false; p2.closest(".field")?.classList.add("invalid"); alert("Passwords do not match."); }
      if (!ok) { e.preventDefault(); e.stopImmediatePropagation(); form.querySelector(".invalid input")?.focus(); }
    });
  });

  // Loading state on submit
  $$("form[data-loading-form], form[data-booking-form]").forEach((form) => form.addEventListener("submit", (e) => {
    if (e.defaultPrevented) return;
    const btn = e.submitter || form.querySelector("button[type=submit]");
    if (btn) btn.classList.add("loading");
    startLoading();
  }));
  $$("a[href^='/']").forEach((a) => a.addEventListener("click", (e) => {
    if (!e.ctrlKey && !e.metaKey && !a.target && !a.getAttribute("href").startsWith("/#")) startLoading();
  }));

  // Staff: condition dropdown saves immediately
  $$("form[data-autosubmit] select").forEach((sel) => {
    const initial = sel.value;
    sel.addEventListener("change", () => {
      if (sel.value === "Damaged" && sel.dataset.confirmDamaged && !confirm(sel.dataset.confirmDamaged)) { sel.value = initial; return; }
      startLoading(); sel.disabled = false; sel.form.submit();
    });
  });

  // Live search suggestions
  $$("[data-live-search]").forEach((form) => {
    const input = $("input[name=q]", form), box = $("[data-suggest]", form);
    let timer, ctrl, idx = -1;
    const hide = () => { box.style.display = "none"; idx = -1; };
    input.addEventListener("input", () => {
      clearTimeout(timer);
      const q = input.value.trim();
      if (q.length < 2) return hide();
      timer = setTimeout(async () => {
        ctrl?.abort(); ctrl = new AbortController();
        try {
          const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`, { signal: ctrl.signal });
          const rows = await res.json();
          box.innerHTML = "";
          rows.forEach((r) => {
            const a = document.createElement("a"); a.href = r.url;
            const name = document.createElement("span"); name.textContent = r.name;
            const meta = document.createElement("small"); meta.textContent = `${r.category} · ${r.lab}`;
            a.append(name, meta); box.append(a);
          });
          if (!rows.length) { const d = document.createElement("a"); d.href = `/equipment?q=${encodeURIComponent(q)}`; d.textContent = `Search all equipment for “${q}”`; box.append(d); }
          box.style.display = "block";
        } catch (_) { /* aborted */ }
      }, 180);
    });
    input.addEventListener("keydown", (e) => {
      const items = $$("a", box); if (!items.length || box.style.display !== "block") return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault(); idx = (idx + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
        items.forEach((x, i) => x.classList.toggle("focus", i === idx));
      } else if (e.key === "Enter" && idx >= 0) { e.preventDefault(); items[idx].click(); }
      else if (e.key === "Escape") hide();
    });
    document.addEventListener("click", (e) => { if (!form.contains(e.target)) hide(); });
  });

  // Booking: availability checker + client checks
  $$("form[data-booking-form]").forEach((form) => {
    const out = $("[data-avail-result]", form), btn = $("[data-check-avail]", form);
    const vals = () => ({ date: form.date.value, start: form.start.value, end: form.end.value });
    const render = (cls, html) => { out.className = "result-box show"; out.innerHTML = `<div class="notice ${cls}">${html}</div>`; };
    const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    btn.addEventListener("click", async () => {
      const v = vals();
      if (!v.date || !v.start || !v.end) return render("warn", "Choose a date, start time and end time first.");
      if (v.end <= v.start) return render("bad", "End time must be after start time.");
      btn.classList.add("loading");
      try {
        const res = await fetch(`${form.dataset.availUrl}?${new URLSearchParams(v)}`);
        const d = await res.json();
        if (!res.ok) return render("bad", esc(d.error || "Could not check availability."));
        if (d.blockers.length) return render("bad", "Not bookable: " + d.blockers.map(esc).join("; "));
        if (d.lab_conflicts.length) return render("bad", "Laboratory is not available then — " + d.lab_conflicts.map(esc).join("; "));
        const qty = parseInt(form.quantity.value || "1", 10);
        if (d.available <= 0) return render("bad", `No units free in this window (${d.booked} of ${d.total} booked).`);
        if (qty > d.available) return render("warn", `Only <b>${d.available}</b> of ${d.total} unit(s) free in this window — reduce the quantity.`);
        render("ok", `<b>${d.available}</b> of ${d.total} unit(s) free in this window. Final availability is re-checked when you confirm.`);
      } catch (_) { render("bad", "Network error — please try again."); }
      finally { btn.classList.remove("loading"); }
    });
    form.addEventListener("submit", (e) => {
      const v = vals();
      if (v.start && v.end && v.end <= v.start) { e.preventDefault(); e.stopImmediatePropagation(); render("bad", "End time must be after start time."); }
    }, true);
  });

  // Staff sidebar (mobile)
  $$("[data-sidebar-toggle]").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); $("#sidebar")?.classList.toggle("open"); }));
  document.addEventListener("click", (e) => { const sb = $("#sidebar"); if (sb && sb.classList.contains("open") && !sb.contains(e.target)) sb.classList.remove("open"); });

  // Copy-to-clipboard
  $$("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
    const text = $(b.dataset.copy)?.textContent.trim();
    try { await navigator.clipboard.writeText(text); b.lastChild.textContent = " Copied"; } catch (_) { prompt("Copy this code:", text); }
  }));

  // Unit-condition editor (dialog + inline form share the same logic)
  function wireUnits(root) {
    const get = (k) => root.querySelector(`[data-u="${k}"]`);
    const keys = ["quantity", "functional", "partial", "damaged"];
    const num = (k) => { const v = parseInt(get(k).value, 10); return Number.isFinite(v) ? v : NaN; };
    const bar = root.querySelector("[data-ud-bar]"), unk = root.querySelector("[data-ud-unknown]");
    const msg = root.querySelector("[data-ud-msg]"), sum = root.querySelector("[data-ud-sum]");
    const save = root.querySelector("[data-ud-save]");
    const update = () => {
      const q = num("quantity"), f = num("functional"), p = num("partial"), d = num("damaged");
      const bad = [q, f, p, d].some((x) => !Number.isFinite(x) || x < 0);
      const u = q - f - p - d;
      unk.textContent = bad ? "—" : String(Math.max(u, 0));
      const ok = !bad && u >= 0;
      msg.textContent = bad ? "Enter whole numbers of 0 or more." : (u < 0 ? `Counts exceed stock by ${-u} unit(s).` : "Counts add up correctly.");
      sum.textContent = bad ? "" : `${f + p + d + Math.max(u, 0)} / ${q} units`;
      sum.className = ok ? "" : "bad";
      msg.style.color = ok ? "" : "var(--bad)";
      if (save) save.disabled = !ok;
      if (bar && ok && q > 0) {
        bar.innerHTML = [["functional", f], ["partially-functional", p], ["damaged", d], ["unknown", u]]
          .filter(([, n]) => n > 0).map(([c, n]) => `<i class="u-${c}" style="width:${(n / q) * 100}%"></i>`).join("");
      } else if (bar) bar.innerHTML = "";
    };
    root.querySelectorAll("[data-step]").forEach((b) => b.addEventListener("click", () => {
      const inp = get(b.dataset.step); const v = (parseInt(inp.value, 10) || 0) + parseInt(b.dataset.d, 10);
      inp.value = Math.max(0, v); update();
    }));
    root.querySelectorAll("[data-quick]").forEach((b) => b.addEventListener("click", () => {
      const q = Math.max(0, num("quantity") || 0);
      const map = { Functional: [q, 0, 0], Damaged: [0, 0, q], Unknown: [0, 0, 0] }[b.dataset.quick];
      ["functional", "partial", "damaged"].forEach((k, i) => (get(k).value = map[i])); update();
    }));
    keys.forEach((k) => get(k).addEventListener("input", update));
    root.__unitsUpdate = update;
    update();
  }
  $$("form[data-units-inline]").forEach(wireUnits);
  const dlg = $("#unitsDialog");
  if (dlg) {
    const form = $("form", dlg);
    wireUnits(form);
    $$("[data-units]").forEach((btn) => btn.addEventListener("click", () => {
      const d = JSON.parse(btn.dataset.units);
      form.action = d.url;
      $("[data-ud-name]", dlg).textContent = d.name;
      $("[data-ud-lab]", dlg).textContent = d.lab;
      $("[data-ud-img]", dlg).src = d.img;
      form.quantity.value = d.q; form.functional.value = d.f; form.partial.value = d.p; form.damaged.value = d.d;
      form.__unitsUpdate();
      if (dlg.showModal) dlg.showModal(); else dlg.setAttribute("open", "");
      form.functional.focus(); form.functional.select();
    }));
    $("[data-ud-close]", dlg).addEventListener("click", () => dlg.close());
    form.addEventListener("submit", () => { $("[data-ud-save]", dlg).classList.add("loading"); startLoading(); });
  }

  // Staff timetable: keyboard clock (starts at 00:00, type digits to fill HH:MM)
  $$("input[data-clock]").forEach((inp) => {
    const POS = [0, 1, 3, 4];
    let i = 0;
    const digits = () => { const m = /^(\d\d):(\d\d)$/.exec(inp.value); return m ? (m[1] + m[2]).split("").map(Number) : [0, 0, 0, 0]; };
    const write = (d) => { inp.value = `${d[0]}${d[1]}:${d[2]}${d[3]}`; };
    const mark = () => { const p = POS[i]; requestAnimationFrame(() => inp.setSelectionRange(p, p + 1)); };
    const fix = (d) => { // keep it a valid 24 h time
      if (d[0] > 2) d[0] = 2;
      if (d[0] === 2 && d[1] > 3) d[1] = 3;
      if (d[2] > 5) d[2] = 5;
      return d;
    };
    if (!/^\d\d:\d\d$/.test(inp.value)) inp.value = "00:00";
    inp.addEventListener("focus", () => { i = 0; mark(); });
    inp.addEventListener("mouseup", (e) => { e.preventDefault(); const c = inp.selectionStart ?? 0; i = c <= 1 ? (c === 1 ? 1 : 0) : (c <= 3 ? 2 : 3); mark(); });
    inp.addEventListener("keydown", (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey || e.key === "Tab" || e.key === "Enter") return;
      e.preventDefault();
      const d = digits();
      if (/^[0-9]$/.test(e.key)) {
        const n = Number(e.key);
        if (i === 0 && n > 2) { d[0] = 0; d[1] = n; i = 2; }        // "9" -> 09:__
        else if (i === 2 && n > 5) { d[2] = 0; d[3] = n; i = 0; }   // "7" -> __:07
        else { d[i] = n; if (i === 0 && n === 2 && d[1] > 3) d[1] = 3; i = (i + 1) % 4; }
        write(fix(d));
      } else if (e.key === "Backspace" || e.key === "Delete") {
        if (e.key === "Backspace") i = (i + 3) % 4;
        d[i] = 0; write(d);
      } else if (e.key === "ArrowLeft") i = (i + 3) % 4;
      else if (e.key === "ArrowRight" || e.key === ":") i = (i + 1) % 4;
      else if (e.key === "ArrowUp" || e.key === "ArrowDown") {
        const max = [2, 9, 5, 9][i], step = e.key === "ArrowUp" ? 1 : -1;
        d[i] = (d[i] + step + max + 1) % (max + 1); write(fix(d));
      } else if (e.key === "Escape") { write([0, 0, 0, 0]); i = 0; }
      mark();
    });
    inp.addEventListener("paste", (e) => { // accept "9:30", "0930", "09:30"
      e.preventDefault();
      const t = (e.clipboardData.getData("text") || "").replace(/\D/g, "").padStart(4, "0").slice(-4);
      write(fix(t.split("").map(Number))); i = 0; mark();
    });
  });

  // Booking: weekend / lunch / hours checks before contacting the server
  $$("form[data-booking-form]").forEach((form) => {
    const allowed = (form.dataset.weekdays || "0,1,2,3,4,5,6").split(",").map(Number);
    const out = $("[data-avail-result]", form);
    const dayOk = () => {
      if (!form.date.value) return true;
      const js = new Date(form.date.value + "T00:00:00").getDay(); // 0 = Sun
      return allowed.includes((js + 6) % 7);
    };
    const warn = () => {
      if (!dayOk()) { out.className = "result-box show"; out.innerHTML = '<div class="notice bad">Equipment cannot be booked on Saturdays or Sundays. Choose a weekday (Mon–Fri).</div>'; return false; }
      return true;
    };
    form.date.addEventListener("change", () => { if (warn()) { out.className = "result-box"; out.innerHTML = ""; } });
    form.addEventListener("submit", (e) => { if (!warn()) { e.preventDefault(); e.stopImmediatePropagation(); } }, true);
    $("[data-check-avail]", form).addEventListener("click", (e) => { if (!warn()) e.stopImmediatePropagation(); }, true);
  });
})();
