(function () {
  const STORAGE_KEY = "wbProductCapture";
  const MARKETPLACE = "wildberries";
  const SCAN_INTERVAL_MS = 1200;

  let active = false;
  let scanTimer = null;
  let observer = null;

  function isProductUrl(value) {
    if (!value) return false;

    let url;
    try {
      url = new URL(value, window.location.href);
    } catch {
      return false;
    }

    const hostname = url.hostname.toLowerCase();
    if (hostname !== "www.wildberries.ru" && hostname !== "wildberries.ru") {
      return false;
    }

    const path = url.pathname.toLowerCase();
    return /^\/catalog\/\d+\/detail\.aspx$/.test(path);
  }

  function normalizeProductUrl(value) {
    const url = new URL(value, window.location.href);
    return `https://www.wildberries.ru${url.pathname}`;
  }

  async function getState() {
    const data = await chrome.storage.local.get(STORAGE_KEY);
    return data[STORAGE_KEY] || { active: false, urls: [] };
  }

  async function setState(state) {
    await chrome.storage.local.set({ [STORAGE_KEY]: state });
  }

  async function addUrls(urls) {
    if (!urls.length) return;

    const state = await getState();
    const nextUrls = Array.from(new Set([...(state.urls || []), ...urls])).sort();
    await setState({
      active: state.active === true,
      marketplace: MARKETPLACE,
      urls: nextUrls,
      updatedAt: new Date().toISOString()
    });
  }

  function scanPage() {
    if (!active) return;

    const urls = [];
    document.querySelectorAll("a[href]").forEach((anchor) => {
      const href = anchor.getAttribute("href");
      if (isProductUrl(href)) {
        urls.push(normalizeProductUrl(href));
      }
    });

    addUrls(urls).catch(() => {});
  }

  function startCapture() {
    active = true;
    scanPage();

    if (!scanTimer) {
      scanTimer = window.setInterval(scanPage, SCAN_INTERVAL_MS);
    }

    if (!observer) {
      observer = new MutationObserver(() => scanPage());
      observer.observe(document.documentElement, {
        childList: true,
        subtree: true
      });
    }
  }

  function stopCapture() {
    active = false;

    if (scanTimer) {
      window.clearInterval(scanTimer);
      scanTimer = null;
    }

    if (observer) {
      observer.disconnect();
      observer = null;
    }
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "WB_CAPTURE_START") {
      getState()
        .then((state) => setState({ ...state, active: true, marketplace: MARKETPLACE }))
        .then(() => {
          startCapture();
          sendResponse({ ok: true });
        })
        .catch((error) => sendResponse({ ok: false, error: String(error) }));
      return true;
    }

    if (message?.type === "WB_CAPTURE_STOP") {
      getState()
        .then((state) => setState({ ...state, active: false, marketplace: MARKETPLACE }))
        .then(() => {
          stopCapture();
          sendResponse({ ok: true });
        })
        .catch((error) => sendResponse({ ok: false, error: String(error) }));
      return true;
    }

    if (message?.type === "WB_CAPTURE_SCAN") {
      scanPage();
      sendResponse({ ok: true });
      return false;
    }

    return false;
  });

  getState()
    .then((state) => {
      if (state.active) {
        startCapture();
      }
    })
    .catch(() => {});
})();
