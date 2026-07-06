// tv-tracker client helpers. Vanilla JS, no frameworks.
//
// Pages wire mutations through post(): POST JSON to an /api/ route, then
// reload so the server re-renders current state. Fine for a single user
// on a LAN/tailnet.

async function post(url, body) {
    const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
    });
    if (!res.ok) {
        const text = await res.text().catch(() => "");
        alert(`Request failed (${res.status}): ${text}`);
        throw new Error(`POST ${url} -> ${res.status}`);
    }
    return res.json().catch(() => ({}));
}

// POST then reload — the standard mutation path for buttons.
async function act(url, body) {
    await post(url, body);
    location.reload();
}

// -- /add page ------------------------------------------------------------

function esc(s) {
    const div = document.createElement("div");
    div.textContent = s == null ? "" : String(s);
    return div.innerHTML;
}

async function searchShows() {
    const q = document.getElementById("q").value.trim();
    const box = document.getElementById("results");
    if (!q) return;
    box.innerHTML = '<p class="muted">Searching…</p>';
    const res = await fetch(`/api/search/shows?q=${encodeURIComponent(q)}`);
    if (!res.ok) {
        box.innerHTML = '<p class="muted">Search failed — is TVmaze reachable?</p>';
        return;
    }
    const data = await res.json();
    if (!data.results.length) {
        box.innerHTML = '<p class="muted">No matches.</p>';
        return;
    }
    box.innerHTML = data.results.map(s => `
<div class="card">
  ${s.image_url ? `<img src="${esc(s.image_url)}" alt="" width="48" style="border-radius:6px">` : ""}
  <div class="grow">
    <div class="title">${esc(s.name)}</div>
    <div class="sub">${esc(s.premiered || "date unknown")} · ${esc(s.tvmaze_status || "")}</div>
  </div>
  ${s.already_added
      ? '<span class="muted">added</span>'
      : `<button class="primary" onclick="addShow(${s.tvmaze_id}, this)">Add</button>`}
</div>`).join("");
}

async function addShow(tvmazeId, btn) {
    btn.disabled = true;
    btn.textContent = "Adding…";
    const data = await post("/api/shows", { tvmaze_id: tvmazeId });
    location.href = `/show/${data.show_id}`;
}
