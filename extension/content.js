// Runs in the page to read what is actually rendered.
//
// The DOM after script execution is materially different provenance from a
// fetch (spec §10.2): better fidelity, but rendered *for this viewer*,
// personalisation and A/B bucketing included. seshat records it as
// `extraction=browser` so that difference survives in the data.
(() => {
  const drop = "script,style,noscript,template,svg,canvas,iframe,nav,header,footer,aside,form";
  const clone = document.body ? document.body.cloneNode(true) : null;
  if (clone) clone.querySelectorAll(drop).forEach((n) => n.remove());
  const text = clone ? clone.innerText.replace(/\n{3,}/g, "\n\n").trim() : "";
  return {
    url: location.href,
    title: document.title || "",
    text,
    // Full outerHTML, so extraction stays re-runnable over the captured bytes
    // exactly as it is for a fetch.
    html: document.documentElement ? document.documentElement.outerHTML : "",
    selection: String(window.getSelection() || ""),
  };
})();
