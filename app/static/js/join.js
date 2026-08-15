// Join page: build skill chips, prefill if already registered, submit.
let selectedSkills = new Set();

async function init() {
  // If already registered, skip straight to the feed.
  try {
    const { profile } = await API.get("/api/me");
    if (profile) { location.href = "/feed"; return; }
  } catch {}

  const { skills } = await API.get("/api/skills");
  const box = document.getElementById("skills");
  skills.forEach((s) => {
    const chip = el("div", { class: "chip", onclick: () => toggle(chip, s) }, s);
    box.appendChild(chip);
  });

  const cap = document.getElementById("cap");
  const capVal = document.getElementById("cap-val");
  cap.addEventListener("input", () => (capVal.textContent = cap.value));

  document.getElementById("join-form").addEventListener("submit", submit);
  document.getElementById("login-form").addEventListener("submit", signIn);
  document.getElementById("tab-join").addEventListener("click", () => showTab("join"));
  document.getElementById("tab-login").addEventListener("click", () => showTab("login"));
}

function showTab(which) {
  const login = which === "login";
  document.getElementById("join-form").hidden = login;
  document.getElementById("login-form").hidden = !login;
  document.getElementById("tab-join").classList.toggle("on", !login);
  document.getElementById("tab-login").classList.toggle("on", login);
}

async function signIn(ev) {
  ev.preventDefault();
  const discord_handle = document.getElementById("login-handle").value.trim();
  try {
    await API.post("/api/login", { discord_handle });
    location.href = "/feed";
  } catch (e) {
    document.getElementById("login-note").textContent =
      e.message.includes("please join") ? "We don't know that username yet — switch to “join” to create your profile." : "Error: " + e.message;
  }
}

function toggle(chip, skill) {
  if (selectedSkills.has(skill)) { selectedSkills.delete(skill); chip.classList.remove("on"); }
  else { selectedSkills.add(skill); chip.classList.add("on"); }
}

async function submit(ev) {
  ev.preventDefault();
  const body = {
    name: document.getElementById("name").value.trim(),
    discord_handle: document.getElementById("handle").value.trim(),
    skills: [...selectedSkills],
    max_capacity_min: parseInt(document.getElementById("cap").value, 10),
  };
  try {
    const res = await API.post("/api/register", body);
    if (!res.matched_upstream) {
      showToast("Saved! (not yet linked to Discord — register the bot to sync)");
      setTimeout(() => (location.href = "/feed"), 1200);
    } else {
      location.href = "/feed";
    }
  } catch (e) { showToast("Error: " + e.message); }
}

init();
