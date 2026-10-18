async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function scoreClass(n) { return n >= 7 ? "good" : n >= 5 ? "warn" : "bad"; }

// poll fn every ms until it returns true
function poll(fn, ms = 2000) {
  const tick = async () => { try { if (await fn()) return; } catch (e) { console.error(e); } setTimeout(tick, ms); };
  tick();
}
