// User detail: chores in progress (top) and completed, live-updated.
const uid = window.USER_ID;

async function init() {
  await load();
  connectWS((msg) => {
    if (["snapshot", "task_done", "task_claimed", "task_created", "profile_updated"].includes(msg.type)) load();
  });
}

async function load() {
  const d = await API.get(`/api/users/${encodeURIComponent(uid)}`);
  const head = document.getElementById("head");
  head.innerHTML = "";
  const toggle = el("button", { class: d.departed ? "secondary" : "ghost small", onclick: () => setDeparted(!d.departed) },
    d.departed ? "↩︎ Mark as back" : "🚪 Mark as left early");
  head.appendChild(el("div", { class: "card" },
    el("div", { class: "row", style: "display:flex;justify-content:space-between;align-items:start;gap:10px" },
      el("div", {},
        el("h1", { style: "margin:.1em 0" }, d.name,
          d.departed ? el("span", { class: "badge", style: "margin-left:10px;vertical-align:middle" }, "🚪 left early") : null),
        d.handle ? el("p", { class: "muted", style: "margin:0" }, "@" + d.handle) : null),
      toggle),
    d.departed ? el("p", { class: "muted", style: "margin:.4em 0 0" }, "Kept on the leaderboard, but no longer suggested or auto-assigned.") : null,
    el("p", { class: "muted", style: "margin:.4em 0 0" },
      `${d.performed.length} done · ${fmtMin(d.time_spent_min) || "0 min"} spent · ${d.performing.length} in progress`)));

  renderList("performing", d.performing, "Not working on anything right now.");
  renderList("performed", d.performed, "Nothing completed yet.");
}

async function setDeparted(departed) {
  try {
    const r = await API.post(`/api/users/${encodeURIComponent(uid)}/departure`, { departed });
    showToast(departed
      ? `Marked as left early${r.released ? ` — released ${r.released} chore(s)` : ""}`
      : "Welcome back!");
    await load();
  } catch (e) { showError(e); }
}

function renderList(id, chores, emptyMsg) {
  const box = document.getElementById(id);
  box.innerHTML = "";
  if (!chores.length) { box.appendChild(el("p", { class: "muted" }, emptyMsg)); return; }
  chores.forEach((c) => {
    const badges = el("div", { class: "badges" },
      el("span", { class: `badge size-${c.size}` }, `${c.size} · ${fmtMin(c.estimated_time_min)}`),
      c.urgent && !c.completed ? el("span", { class: "badge urgent" }, "URGENT") : null,
      ...(c.necessary_capabilities || []).map((s) => el("span", { class: "badge skill" }, s)),
      c.completed ? el("span", { class: "badge claimed" }, "✓ done") : null);
    box.appendChild(el("div", { class: `card chore ${c.urgent && !c.completed ? "urgent" : ""} ${c.completed ? "done" : ""}` },
      el("h3", { style: "margin:0" }, el("a", { href: `/chores/${c.id}`, style: "color:inherit" }, c.name)),
      badges));
  });
}

init();
