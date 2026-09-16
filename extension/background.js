// Nothing here writes a note. The popup does, and only when you submit it.
chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.get(["endpoint"], ({ endpoint }) => {
    if (!endpoint) {
      chrome.storage.local.set({ endpoint: "http://127.0.0.1:8765" });
    }
  });
});
