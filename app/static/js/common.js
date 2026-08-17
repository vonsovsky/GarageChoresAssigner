// Shared helpers: fetch wrappers, WebSocket connection, small DOM utils.

const API = {
  async get(path) {
    const r = await fetch(path, { headers: { "Accept": "application/json" } });
    if (!r.ok) throw new Error((await safeJson(r))?.detail || r.statusText);
    return r.json();
  },
  async send(method, path, body) {
    const r = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) throw new Error((await safeJson(r))?.detail || r.statusText);
    return r.json();
  },
  post(path, body) { return this.send("POST", path, body); },
  put(path, body) { return this.send("PUT", path, body); },
  del(path) { return this.send("DELETE", path); },
};

async function safeJson(r) { try { return await r.json(); } catch { return null; } }

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) e.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null) continue;
    e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return e;
}

// Auto-reconnecting WebSocket with a heartbeat + watchdog so a silently-dropped
// connection (idle TV, proxy timeout, half-open socket) is detected and
// reconnected — reconnecting re-fetches a fresh snapshot, so anything missed
// during the gap shows up. `onMessage(data)` receives parsed JSON.
const WS_PING_MS = 20000;   // client ping cadence
const WS_STALE_MS = 45000;  // no traffic for this long => assume dead, reconnect
function connectWS(onMessage, onStatus) {
  let ws, closed = false, backoff = 500, lastRx = Date.now();
  let pingTimer, watchdog;
  const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";

  function clearTimers() { clearInterval(pingTimer); clearInterval(watchdog); }

  function open() {
    ws = new WebSocket(url);
    ws.onopen = () => {
      backoff = 500; lastRx = Date.now(); onStatus && onStatus(true);
      clearTimers();
      // Send a lightweight ping so proxies see traffic and the server can tell
      // we're alive; the server also pings us so lastRx stays fresh.
      pingTimer = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) { try { ws.send("ping"); } catch (e) {} }
      }, WS_PING_MS);
      // If nothing has arrived for a while, the socket is probably dead even if
      // no close event fired — force a reconnect.
      watchdog = setInterval(() => {
        if (Date.now() - lastRx > WS_STALE_MS) { try { ws.close(); } catch (e) {} }
      }, WS_PING_MS / 2);
    };
    ws.onclose = () => {
      clearTimers();
      onStatus && onStatus(false);
      if (!closed) setTimeout(open, backoff = Math.min(backoff * 2, 8000));
    };
    ws.onerror = () => ws.close();
    ws.onmessage = (ev) => {
      lastRx = Date.now();
      let data;
      try { data = JSON.parse(ev.data); } catch (e) { return; }
      if (data && data.type === "ping") return;  // heartbeat, not app data
      try { onMessage(data); } catch (e) { console.error(e); }
    };
  }
  open();
  return { close() { closed = true; clearTimers(); ws && ws.close(); } };
}

function showToast(msg, ms = 3200) {
  let t = document.querySelector(".toast");
  if (!t) { t = el("div", { class: "toast" }); document.body.appendChild(t); }
  t.textContent = msg;
  requestAnimationFrame(() => t.classList.add("show"));
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), ms);
}

const fmtMin = (m) => (m == null ? "" : m >= 60 ? `${Math.round(m / 6) / 10} h` : `${m} min`);
