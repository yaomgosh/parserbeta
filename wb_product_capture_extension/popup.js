const STORAGE_KEY = "wbProductCapture";
const MARKETPLACE = "wildberries";

const elements = {
  status: document.getElementById("status"),
  count: document.getElementById("count"),
  start: document.getElementById("start"),
  stop: document.getElementById("stop"),
  export: document.getElementById("export"),
  clear: document.getElementById("clear")
};

function csvEscape(value) {
  const text = String(value ?? "");
  if (/[",\n\r]/.test(text)) {
    return `"${text.replaceAll('"', '""')}"`;
  }
  return text;
}

async function getState() {
  const data = await chrome.storage.local.get(STORAGE_KEY);
  return data[STORAGE_KEY] || { active: false, marketplace: MARKETPLACE, urls: [] };
}

async function setState(state) {
  await chrome.storage.local.set({ [STORAGE_KEY]: state });
}

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

function isWildberriesTab(tab) {
  return /^https:\/\/(www\.)?wildberries\.ru\//.test(tab?.url || "");
}

async function sendToContent(type) {
  const tab = await getActiveTab();
  if (!isWildberriesTab(tab)) {
    throw new Error("Открой активную вкладку Wildberries.");
  }
  return chrome.tabs.sendMessage(tab.id, { type });
}

function render(state, tabIsSupported) {
  const count = state.urls?.length || 0;
  elements.count.textContent = String(count);
  elements.stop.disabled = !state.active;
  elements.export.disabled = count === 0;
  elements.clear.disabled = count === 0 && !state.active;
  elements.start.disabled = !tabIsSupported || state.active;

  if (!tabIsSupported) {
    elements.status.textContent = "Открой вкладку Wildberries, затем нажми Start capture.";
  } else if (state.active) {
    elements.status.textContent = "Сбор включен. Скролль страницу вручную.";
  } else {
    elements.status.textContent = "Сбор остановлен. Можно продолжить или экспортировать CSV.";
  }
}

async function refresh() {
  const [state, tab] = await Promise.all([getState(), getActiveTab()]);
  render(state, isWildberriesTab(tab));
}

async function startCapture() {
  const state = await getState();
  await setState({ ...state, active: true, marketplace: MARKETPLACE });
  await sendToContent("WB_CAPTURE_START");
  await refresh();
}

async function stopCapture() {
  const state = await getState();
  await setState({ ...state, active: false, marketplace: MARKETPLACE });
  await sendToContent("WB_CAPTURE_STOP").catch(() => {});
  await refresh();
}

async function clearCapture() {
  await setState({ active: false, marketplace: MARKETPLACE, urls: [] });
  await sendToContent("WB_CAPTURE_STOP").catch(() => {});
  await refresh();
}

async function exportCsv() {
  const state = await getState();
  const urls = state.urls || [];
  const rows = [
    ["marketplace", "product_url"],
    ...urls.map((url) => [MARKETPLACE, url])
  ];
  const csv = rows.map((row) => row.map(csvEscape).join(",")).join("\r\n") + "\r\n";
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);

  await chrome.downloads.download({
    url,
    filename: "product_urls.csv",
    saveAs: true
  });

  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

function showError(error) {
  elements.status.textContent = error?.message || String(error);
}

elements.start.addEventListener("click", () => startCapture().catch(showError));
elements.stop.addEventListener("click", () => stopCapture().catch(showError));
elements.clear.addEventListener("click", () => clearCapture().catch(showError));
elements.export.addEventListener("click", () => exportCsv().catch(showError));

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName === "local" && changes[STORAGE_KEY]) {
    refresh().catch(showError);
  }
});

refresh().catch(showError);
