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

// Refresh buttons can take a while (rate-limited TVmaze calls) — show
// progress on the button itself, then reload.
async function refreshBtn(url, btn) {
    btn.disabled = true;
    btn.textContent = "Refreshing…";
    try {
        await post(url);
    } finally {
        location.reload();
    }
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

// -- /movies page -----------------------------------------------------------

async function searchMovies() {
    const q = document.getElementById("q").value.trim();
    const box = document.getElementById("results");
    if (!q) return;
    box.innerHTML = '<p class="muted">Searching…</p>';
    const res = await fetch(`/api/search/movies?q=${encodeURIComponent(q)}`);
    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        box.innerHTML = `<p class="muted">${esc(err.error || "Search failed.")}</p>`;
        return;
    }
    const data = await res.json();
    if (!data.results.length) {
        box.innerHTML = '<p class="muted">No matches.</p>';
        return;
    }
    box.innerHTML = data.results.map(m => `
<div class="card">
  ${m.poster_url ? `<img src="${esc(m.poster_url)}" alt="" width="48" style="border-radius:6px">` : ""}
  <div class="grow">
    <div class="title">${esc(m.title)}${m.year ? ` (${m.year})` : ""}</div>
  </div>
  ${m.already_added
      ? '<span class="muted">added</span>'
      : `<button class="primary" onclick="addMovie(${m.tmdb_id}, this)">Add</button>`}
</div>`).join("");
}

async function addMovie(tmdbId, btn) {
    btn.disabled = true;
    btn.textContent = "Adding…";
    await post("/api/movies", { tmdb_id: tmdbId });
    location.reload();
}

// -- /import page -----------------------------------------------------------

async function importSearch(stagingId, kind, btn) {
    const card = document.getElementById(`staging-${stagingId}`);
    const box = card.querySelector(".resolve-results");
    const name = card.querySelector(".title").textContent;
    btn.disabled = true;
    box.innerHTML = '<p class="muted">Searching…</p>';
    const url = kind === "show"
        ? `/api/search/shows?q=${encodeURIComponent(name)}`
        : `/api/search/movies?q=${encodeURIComponent(name)}`;
    const res = await fetch(url);
    btn.disabled = false;
    if (!res.ok) {
        box.innerHTML = '<p class="muted">Search failed.</p>';
        return;
    }
    const data = await res.json();
    if (!data.results.length) {
        box.innerHTML = '<p class="muted">No candidates found — Skip, or rename and retry.</p>';
        return;
    }
    box.innerHTML = data.results.slice(0, 5).map(r => {
        const id = kind === "show" ? r.tvmaze_id : r.tmdb_id;
        const label = kind === "show"
            ? `${esc(r.name)} (${esc(r.premiered || "?")})`
            : `${esc(r.title)}${r.year ? ` (${r.year})` : ""}`;
        const payload = kind === "show" ? `{tvmaze_id: ${id}}` : `{tmdb_id: ${id}}`;
        return `<p>${label}
            <button class="primary"
              onclick="resolveImport(${stagingId}, ${payload}, this)">Link</button></p>`;
    }).join("");
}

async function resolveImport(stagingId, extra, btn) {
    btn.disabled = true;
    btn.textContent = "…";
    await post("/api/import/resolve", { staging_id: stagingId, ...extra });
    document.getElementById(`staging-${stagingId}`).remove();
}
