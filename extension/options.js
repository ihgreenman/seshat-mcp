const $ = (id) => document.getElementById(id);
chrome.storage.local.get(["endpoint", "token"], (stored) => {
  $("endpoint").value = stored.endpoint || "http://127.0.0.1:8765";
  $("token").value = stored.token || "";
});
$("save").addEventListener("click", async () => {
  const endpoint = $("endpoint").value.replace(/\/+$/, "");
  const token = $("token").value.trim();
  await chrome.storage.local.set({ endpoint, token });
  $("status").textContent = "checking…";
  try {
    const res = await fetch(`${endpoint}/api/ping`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const body = await res.json();
    $("status").textContent = res.ok
      ? `connected — spec ${body.spec_version}, capture ${body.capture ? "on" : "off"}`
      : `refused: ${body.error || res.status}`;
  } catch (err) {
    $("status").textContent = `no answer from ${endpoint} — is \`seshat review\` running?`;
  }
});
