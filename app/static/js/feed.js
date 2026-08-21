// Chore feed: live list, claim with funny ack, urgent + suggested highlighting.
const state = { chores: new Map(), suggestions: new Map(), myUid: null, myName: null };

async function init() {
  const { profile, discord_id } = await API.get("/api/me");
  if (!profile) { location.href = "/"; return; }
  state.myUid = discord_id;
  state.myName = profile.name;
  document.getElementById("greeting").textContent = `Hi ${profile.name} — grab a chore when you can 💪`;

  connectWS(onMessage, (up) => {
    const c = document.getElementById("conn");
    c.className = "conn " + (up ? "up" : "down");
    c.textContent = up ? "● live" : "● offline";
  });
}

function onMessage(msg) {
  switch (msg.type) {
    case "snapshot":
      state.chores.clear(); state.suggestions.clear();
      msg.chores.forEach((c) => state.chores.set(c.id, c));
      Object.entries(msg.suggestions || {}).forEach(([id, top]) => state.suggestions.set(+id, top));
      break;
    case "task_done":
      state.chores.delete(msg.chore?.id);
      break;
    default:
      if (msg.chore && msg.chore.id != null) {
        if (msg.chore.active === false) state.chores.delete(msg.chore.id);
        else state.chores.set(msg.chore.id, msg.chore);
        if (msg.suggestions) state.suggestions.set(msg.chore.id, msg.suggestions);
        // Ping me if a fresh chore wants me.
        if (msg.type === "task_created" && (msg.suggestions || []).includes(state.myUid)) {
          showToast("🔔 A new chore suggests YOU!");
        }
      }
  }
  render();
}

function sortChores(list) {
  return list.sort((a, b) =>
    (a.urgent === b.urgent ? 0 : a.urgent ? -1 : 1) ||
    ((a.minutes_to_deadline ?? 1e9) - (b.minutes_to_deadline ?? 1e9)) ||
    (b.estimated_time_min - a.estimated_time_min));
}

function render() {
  const wrap = document.getElementById("chores");
  wrap.innerHTML = "";
  const list = sortChores([...state.chores.values()].filter((c) => c.active !== false));
  document.getElementById("empty").hidden = list.length > 0;
  list.forEach((c) => wrap.appendChild(choreCard(c)));
}

function choreCard(c) {
  const top = state.suggestions.get(c.id) || [];
  const suggested = top.includes(state.myUid);
  const iClaimed = (c.claimers || []).some((p) => p.discord_id === state.myUid);

  const badges = el("div", { class: "badges" },
    el("span", { class: `badge size-${c.size}` }, `${c.size} · ${fmtMin(c.estimated_time_min)}`),
    c.urgent ? el("span", { class: "badge urgent" }, "URGENT") : null,
    suggested ? el("span", { class: "badge suggest" }, "⭐ Suggested for you") : null,
    ...(c.necessary_capabilities || []).map((s) => el("span", { class: "badge skill" }, "needs " + s)),
    el("span", { class: "badge" }, `${c.claimed_count}/${c.necessary_workers} claimed`),
    c.minutes_to_deadline != null ? el("span", { class: "badge" }, `⏰ ${fmtMin(Math.max(0, c.minutes_to_deadline))} left`) : null,
  );

  const claimers = (c.claimers || []).length
    ? el("p", { class: "muted", style: "margin:.3em 0 0;font-size:.85rem" }, "On it: " + c.claimers.map((p) => p.name).join(", "))
    : null;

  const btn = iClaimed
    ? el("button", { class: "secondary", onclick: () => unclaim(c.id) }, "✓ You're on it — tap to drop")
    : el("button", { onclick: (e) => claim(c.id, e.target) }, "Claim it");

  const done = el("button", { class: "ghost small", onclick: () => markDone(c.id) }, "Mark done");

  const card = el("div", { class: `card chore ${c.urgent ? "urgent" : ""} ${suggested ? "suggested" : ""}` },
    el("h3", {}, el("a", { href: `/chores/${c.id}`, style: "color:inherit" }, c.name)),
    badges,
    claimers,
    el("div", { class: "row", style: "margin-top:12px;gap:8px" }, btn, done),
  );
  return card;
}

async function claim(id, btn) {
  btn.disabled = true;
  try {
    const { ack } = await API.post(`/api/chores/${id}/claim`);
    showToast(ack);
  } catch (e) { showError(e); btn.disabled = false; }
}
async function unclaim(id) {
  try { await API.post(`/api/chores/${id}/unclaim`); } catch (e) { showError(e); }
}
async function markDone(id) {
  try { await API.post(`/api/chores/${id}/done`); showToast("Nice — chore done! 🎊"); }
  catch (e) { showError(e); }
}

init();
