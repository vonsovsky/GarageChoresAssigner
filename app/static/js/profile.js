// Profile page: edit info, view workload, log out-of-scope work.
let selectedSkills = new Set();
let myUid = null;

async function init() {
  const { profile, discord_id } = await API.get("/api/me");
  if (!profile) { location.href = "/"; return; }
  myUid = discord_id;

  document.getElementById("name").textContent = profile.name;
  document.getElementById("handle").textContent = profile.discord_handle ? "@" + profile.discord_handle : "";
  const cap = document.getElementById("cap");
  cap.value = profile.max_capacity_min;
  const capVal = document.getElementById("cap-val");
  capVal.textContent = profile.max_capacity_min;
  cap.addEventListener("input", () => (capVal.textContent = cap.value));

  const { skills } = await API.get("/api/skills");
  const box = document.getElementById("skills");
  selectedSkills = new Set(profile.skills || []);
  skills.forEach((s) => {
    const chip = el("div", { class: "chip" + (selectedSkills.has(s) ? " on" : ""), onclick: () => toggle(chip, s) }, s);
    box.appendChild(chip);
  });

  document.getElementById("profile-form").addEventListener("submit", save);
  document.getElementById("manual-form").addEventListener("submit", addManual);
  document.getElementById("signout").addEventListener("click", signOut);
  await Promise.all([loadWorkload(), loadManual()]);
}

function toggle(chip, skill) {
  if (selectedSkills.has(skill)) { selectedSkills.delete(skill); chip.classList.remove("on"); }
  else { selectedSkills.add(skill); chip.classList.add("on"); }
}

async function save(ev) {
  ev.preventDefault();
  const body = {
    skills: [...selectedSkills],
    max_capacity_min: parseInt(document.getElementById("cap").value, 10),
  };
  try { await API.put("/api/me", body); showToast("Saved ✓"); loadWorkload(); }
  catch (e) { showToast("Error: " + e.message); }
}

function signOut() {
  location.href = "/auth/logout";
}

async function loadWorkload() {
  const { people } = await API.get("/api/people");
  const me = people.find((p) => p.discord_id === myUid);
  const box = document.getElementById("workload");
  if (!me) { box.innerHTML = '<p class="muted">No workload data yet.</p>'; return; }
  const pct = Math.min(100, Math.round((me.workload_min / me.max_capacity_min) * 100));
  box.innerHTML = "";
  box.appendChild(el("p", {}, `${me.workload_min} / ${me.max_capacity_min} min used (${pct}%)`));
  box.appendChild(el("div", { class: "bar" }, el("span", { style: `width:${pct}%` })));
  if (me.capabilities?.length) box.appendChild(el("p", { class: "muted", style: "margin-top:8px" }, "Skills: " + me.capabilities.join(", ")));
}

async function loadManual() {
  const { entries } = await API.get("/api/me/manual-work");
  const list = document.getElementById("manual-list");
  list.innerHTML = "";
  if (!entries.length) { list.appendChild(el("li", { class: "muted" }, "No entries yet.")); return; }
  entries.forEach((e) =>
    list.appendChild(el("li", {}, el("span", {}, e.description), el("span", { class: "muted" }, `${e.minutes} min`))));
}

async function addManual(ev) {
  ev.preventDefault();
  const body = {
    description: document.getElementById("desc").value.trim(),
    minutes: parseInt(document.getElementById("mins").value, 10),
  };
  try {
    await API.post("/api/me/manual-work", body);
    document.getElementById("desc").value = "";
    showToast("Logged ✓");
    await Promise.all([loadManual(), loadWorkload()]);
  } catch (e) { showToast("Error: " + e.message); }
}

init();
