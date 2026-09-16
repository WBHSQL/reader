(() => {
  const clean = (value) => String(value || '').replace(/\u200b/g, '').trim();
  const textOf = (...selectors) => {
    for (const selector of selectors) {
      const node = document.querySelector(selector);
      const value = clean(node?.innerText || node?.textContent || '');
      if (value) return value;
    }
    return '';
  };

  const bodyNode = document.querySelector('#js_content');
  const body = clean(bodyNode?.innerText || '');
  const title = textOf('#activity-name', 'h1.rich_media_title');
  if (!title || body.length < 300) return;

  const params = new URLSearchParams(location.hash.replace(/^#/, ''));
  const payload = {
    target_id: params.get('reader_target') || '',
    title,
    author: textOf('#js_name', '.rich_media_meta_text'),
    published_at: textOf('#publish_time'),
    url: location.href.split('#')[0],
    body,
    capture_kind: 'browser_dom_wechat',
    captured_at: new Date().toISOString()
  };

  chrome.runtime.sendMessage({type: 'reader-capture', payload}, () => void chrome.runtime.lastError);
})();
