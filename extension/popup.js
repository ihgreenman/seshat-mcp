const $ = (id) => document.getElementById(id);
let page = null;
let config = {};

function say(message, bad = false) {
  $("status").textContent = message;
  $("status").className = bad ? "bad" : "";
}

async function api(path, options = {}) {
  return fetch(`${config.endpoint}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${config.token}`,
      ...(options.headers || {}),
    },
  });
}

async function init() {
  config = await chrome.storage.local.get(["endpoint", "token"]);
  config.endpoint = (config.endpoint || "http://127.0.0.1:8765").replace(/\/+$/, "");
  if (!config.token) {
    say("No token set — open the extension options first.", true);
    $("save").disabled = true;
    return;
  }

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  $("url").textContent = tab.url;

  const injected = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    files: ["content.js"],
  });
  page = injected[0]?.result;
  if (page?.selection) $("detail").value = page.selection;

  // If this page is already sitting in the triage queue, repairing it is one
  // click and no typing -- the whole point of being here with it open.
  try {
    const res = await api("/api/targets");
    if (res.ok) {
      const { targets } = await res.json();
      const match = targets.find((t) => t.target === tab.url);
      if (match) showRepair(match);
    }
  } catch (err) {
    /* the endpoint being down is reported on save, not here */
  }
}

function showRepair(match) {
  const box = $("queued");
  box.hidden = false;
  box.innerHTML =
    `This page is in your triage queue (<b>${match.status}</b>). ` +
    `<button id="repair">Repair with this page</button>`;
  $("repair").addEventListener("click", async () => {
    say("repairing…");
    try {
      const res = await api("/api/repair", {
        method: "POST",
        body: JSON.stringify({
          note_id: match.note_id,
          target: match.target,
          text: page?.text || "",
          html: page?.html || "",
          title: page?.title || "",
        }),
      });
      const body = await res.json();
      say(res.ok && body.ok ? "repaired." : `refused: ${body.error || res.status}`, !res.ok);
    } catch (err) {
      say(`no answer from ${config.endpoint}`, true);
    }
  });
}

$("save").addEventListener("click", async () => {
  const desc = $("desc").value.trim();
  if (!desc) {
    say("Say what the page told you first.", true);
    return;
  }
  say("saving…");
  try {
    const res = await api("/api/capture", {
      method: "POST",
      body: JSON.stringify({
        desc,
        detail: $("detail").value.trim(),
        url: page?.url || $("url").textContent,
        title: page?.title || "",
        text: page?.text || "",
        html: page?.html || "",
      }),
    });
    const body = await res.json();
    if (!res.ok) {
      say(`refused: ${body.error || res.status}`, true);
      return;
    }
    const thin = body.snapshot_status === "thin";
    say(
      `saved as ${body.id}` +
        (thin ? " — but the captured page looks thin; check it in triage." : ""),
      thin
    );
    $("save").disabled = true;
  } catch (err) {
    say(`no answer from ${config.endpoint} — is \`seshat review\` running?`, true);
  }
});

init();
