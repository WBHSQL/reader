chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== 'reader-capture') return;
  fetch('http://127.0.0.1:8765/api/sources/capture', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(message.payload)
  }).then(async (res) => {
    let data = {};
    try { data = await res.json(); } catch (_) {}
    sendResponse({ok: res.ok, status: res.status, data});
  }).catch((error) => sendResponse({ok: false, error: String(error)}));
  return true;
});
