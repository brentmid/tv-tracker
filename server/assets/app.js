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
