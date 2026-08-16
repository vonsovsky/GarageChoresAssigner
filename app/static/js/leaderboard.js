// Leaderboard: sortable table (default: time spent, descending). Live updates.
let rows = [];
let sortKey = "time_spent_min";
let sortDir = -1; // -1 desc, 1 asc

async function init() {
  document.querySelectorAll("th.sortable").forEach((th) =>
    th.addEventListener("click", () => setSort(th.dataset.sort)));
  await load();
  connectWS((msg) => {
    if (["snapshot", "task_done", "task_claimed", "task_created", "workload_updated", "profile_updated"].includes(msg.type)) load();
  });
}

async function load() {
  const data = await API.get("/api/leaderboard");
  rows = data.rows;
  render();
}

function setSort(key) {
  if (sortKey === key) { sortDir = -sortDir; }
  else { sortKey = key; sortDir = key === "name" ? 1 : -1; } // names default A→Z, numbers high→low
  render();
}

function sorted() {
  const r = [...rows];
  r.sort((a, b) => {
    let av = a[sortKey], bv = b[sortKey];
    if (sortKey === "name") { av = av.toLowerCase(); bv = bv.toLowerCase(); return av < bv ? -sortDir : av > bv ? sortDir : 0; }
    return (av - bv) * sortDir;
  });
  return r;
}

function render() {
  // arrows
  document.querySelectorAll("th.sortable").forEach((th) => {
    const a = th.querySelector(".arrow");
    a.textContent = th.dataset.sort === sortKey ? (sortDir === -1 ? "▼" : "▲") : "↕";
    a.style.opacity = th.dataset.sort === sortKey ? "1" : ".35";
  });

  const tbody = document.getElementById("rows");
  tbody.innerHTML = "";
  const list = sorted();
  document.getElementById("empty").hidden = list.length > 0;
  list.forEach((r) => {
    const active = r.performing_count
      ? el("span", { class: "badge suggest", style: "margin-left:8px" }, `${r.performing_count} in progress`)
      : null;
    const left = r.departed
      ? el("span", { class: "badge", style: "margin-left:8px" }, "🚪 left early")
      : null;
    const tr = el("tr", { class: "clickable", onclick: () => (location.href = `/users/${encodeURIComponent(r.discord_id)}`) },
      el("td", {}, el("strong", { style: r.departed ? "opacity:.6" : "" }, r.name), left, active),
      el("td", { class: "num" }, String(r.performed_count)),
      el("td", { class: "num" }, fmtMin(r.time_spent_min) || "0 min"));
    tbody.appendChild(tr);
  });
}

init();
