/* 表情包图库前端逻辑
 *
 * 通过 AstrBot 插件 View 的 bridge SDK 调用后端 API：
 *   window.AstrBotPluginView.apiGet("emoji-harvester/api/assets", {...})
 *
 * bridge 会把 endpoint 拼成：
 *   /api/v1/plugins/extensions/<插件名>/<endpoint>
 * 所以 endpoint 里不要再带插件名，否则会重复成
 *   /api/v1/plugins/extensions/<插件名>/<插件名>/<endpoint>
 * 导致后端匹配不到路由。
 */

const PLUGIN_NAME = "astrbot_plugin_emoji_harvester";
/**
 * 后端端点前缀。
 *
 * bridge 会把 endpoint 拼成：
 *   /api/v1/plugins/extensions/<插件名>/<endpoint>
 * 后端把整段（含插件名）当 plugin_path 匹配路由，所以
 * **endpoint 里不能再带插件名**，否则会拼成
 *   /extensions/<插件名>/<插件名>/<endpoint>
 * 从而匹配不到路由。
 */
const API = "emoji-harvester/api";

const state = {
  page: 1,
  pageSize: 48,
  total: 0,
  items: [],
  keyword: "",
  source: "",
  order: "new",
  sender: "",
  emotion: "",
  animated: false,
  staticOnly: false,
  lbIndex: -1,
};

const $ = (id) => document.getElementById(id);

const el = {
  subtitle: $("subtitle"),
  stats: $("stats"),
  grid: $("grid"),
  empty: $("empty"),
  emptyText: $("empty-text"),
  emptyHint: $("empty-hint"),
  loading: $("loading"),
  pager: $("pager"),
  pageInfo: $("page-info"),
  keyword: $("keyword"),
  source: $("source"),
  order: $("order"),
  sender: $("sender"),
  emotion: $("emotion"),
  animated: $("animated"),
  static: $("static"),
  lightbox: $("lightbox"),
  lbImg: $("lb-img"),
  lbMeta: $("lb-meta"),
  toast: $("toast"),
};

/* ------------------------------------------------------------------ */
/* 工具                                                                */
/* ------------------------------------------------------------------ */
function apiGet(endpoint, params) {
  const view = window.AstrBotPluginView;
  if (!view || typeof view.apiGet !== "function") {
    return Promise.reject(new Error("插件 View SDK 未就绪"));
  }
  return view.apiGet(endpoint, params || {});
}

function apiPost(endpoint, body) {
  const view = window.AstrBotPluginView;
  if (!view || typeof view.apiPost !== "function") {
    return Promise.reject(new Error("插件 View SDK 未就绪"));
  }
  return view.apiPost(endpoint, body || {});
}

/** 拼接素材的完整可访问 URL。
 *
 * 后端返回的是相对路径（/api/v1/plugins/extensions/...），
 * 直接给 <img src> 用即可，同源访问。
 */
function assetUrl(item) {
  if (!item || !item.url) return "";
  if (/^https?:\/\//i.test(item.url)) return item.url;
  return item.url;
}

function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function formatTime(seconds) {
  if (!seconds) return "—";
  const date = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

let toastTimer = null;
function toast(message) {
  el.toast.textContent = message;
  el.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.toast.hidden = true;
  }, 2400);
}

function sourceLabel(source) {
  return { image: "图片", face: "系统表情", mface: "商城表情包" }[source] || source || "未知";
}

/* ------------------------------------------------------------------ */
/* 渲染                                                                */
/* ------------------------------------------------------------------ */
function renderStats(data) {
  if (!data) {
    el.stats.innerHTML = "";
    return;
  }
  const targets = (data.targets || []).join(", ") || "未设置";
  const cards = [
    { k: "素材总数", v: data.count, unit: "张" },
    { k: "动画表情", v: data.animated_count, unit: "张" },
    { k: "占用空间", v: formatBytes(data.total_bytes), unit: "" },
    { k: "监听 QQ", v: targets, unit: "" },
  ];
  el.stats.innerHTML = cards
    .map(
      (card) =>
        `<div class="stat"><div class="k">${escapeHtml(card.k)}</div>` +
        `<div class="v">${escapeHtml(String(card.v))}` +
        (card.unit ? `<small>${escapeHtml(card.unit)}</small>` : "") +
        `</div></div>`,
    )
    .join("");
}

function escapeHtml(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function renderGrid(items) {
  state.items = items || [];
  if (!state.items.length) {
    el.grid.innerHTML = "";
    el.empty.hidden = false;
    const hasFilter =
      state.keyword || state.source || state.sender || state.animated || state.staticOnly;
    el.emptyText.textContent = hasFilter ? "没有匹配的素材" : "还没有采集到任何素材";
    // 有筛选条件时，空状态不该再提示去配置 QQ 号
    el.emptyHint.textContent = hasFilter
      ? "换个关键词或筛选条件试试。"
      : "在插件配置里填上要监听的 QQ 号，然后让那个用户发张图片试试。";
    return;
  }
  el.empty.hidden = true;

  el.grid.innerHTML = state.items
    .map((item, index) => {
      const url = assetUrl(item);
      const sizeText = item.width && item.height ? `${item.width}×${item.height}` : formatBytes(item.size);
      // 视觉模型标注出的情绪 / 场景
      const tags = [];
      if (item.emotion && item.emotion !== "system") {
        tags.push(`<span class="tag-emotion">${escapeHtml(item.emotion)}</span>`);
      }
      if (item.scene) {
        tags.push(`<span class="tag-scene">${escapeHtml(item.scene)}</span>`);
      }
      const caption = item.caption
        ? `<div class="card-caption" title="${escapeHtml(item.caption)}">${escapeHtml(item.caption)}</div>`
        : "";
      return `
        <div class="card" data-index="${index}">
          <div class="thumb">
            <img src="${escapeHtml(url)}" alt="${escapeHtml(item.filename)}" loading="lazy">
          </div>
          <div class="card-info">
            <div class="card-name" title="${escapeHtml(item.filename)}">${escapeHtml(item.filename)}</div>
            ${caption}
            <div class="card-sub">
              <span class="badge ${escapeHtml(item.source)}">${escapeHtml(sourceLabel(item.source))}</span>
              <span>${escapeHtml(sizeText)}</span>
            </div>
            ${tags.length ? `<div class="card-tags">${tags.join("")}</div>` : ""}
          </div>
          <button class="del-btn" data-del="${item.id}" title="删除">✕</button>
        </div>`;
    })
    .join("");
}

function renderPager() {
  const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
  el.pager.hidden = totalPages <= 1;
  el.pageInfo.textContent = `第 ${state.page} / ${totalPages} 页 · 共 ${state.total} 张`;
  $("btn-prev").disabled = state.page <= 1;
  $("btn-next").disabled = state.page >= totalPages;
}

/* ------------------------------------------------------------------ */
/* 数据加载                                                            */
/* ------------------------------------------------------------------ */
function currentParams() {
  return {
    page: state.page,
    page_size: state.pageSize,
    keyword: state.keyword,
    source: state.source,
    order: state.order,
    sender_id: state.sender,
    emotion: state.emotion,
    animated: state.animated ? "1" : "",
    static: state.staticOnly ? "1" : "",
  };
}

async function loadAssets() {
  el.loading.hidden = false;
  try {
    const data = await apiGet(`${API}/assets`, currentParams());
    const payload = (data && data.data) || {};
    state.total = payload.total || 0;
    renderGrid(payload.items || []);
    renderPager();
  } catch (error) {
    console.error("加载素材失败", error);
    toast("加载失败：" + (error && error.message ? error.message : "未知错误"));
    renderGrid([]);
  } finally {
    el.loading.hidden = true;
  }
}

async function loadStats() {
  try {
    const data = await apiGet(`${API}/stats`);
    const payload = (data && data.data) || {};
    renderStats(payload);
    el.subtitle.textContent = payload.targets && payload.targets.length
      ? `正在监听 QQ：${payload.targets.join(", ")}`
      : "尚未设置监听 QQ（可在插件配置页填写）";
  } catch (error) {
    console.error("加载统计失败", error);
    el.subtitle.textContent = "统计信息加载失败";
  }
}

async function loadSenders() {
  try {
    const data = await apiGet(`${API}/senders`);
    const senders = ((data && data.data) || {}).senders || [];
    const current = el.sender.value;
    el.sender.innerHTML =
      '<option value="">全部发送者</option>' +
      senders
        .map(
          (item) =>
            `<option value="${escapeHtml(item.sender_id)}">` +
            `${escapeHtml(item.sender_name || item.sender_id)} (${item.count})</option>`,
        )
        .join("");
    el.sender.value = current;
  } catch (error) {
    console.error("加载发送者失败", error);
  }
}

async function loadEmotions() {
  try {
    const data = await apiGet(`${API}/emotions`);
    const payload = (data && data.data) || {};
    const emotions = payload.emotions || [];
    const current = el.emotion.value;
    el.emotion.innerHTML =
      '<option value="">全部情绪</option>' +
      emotions
        .map(
          (item) =>
            `<option value="${escapeHtml(item.emotion)}">` +
            `${escapeHtml(item.emotion)} (${item.count})</option>`,
        )
        .join("");
    el.emotion.value = current;
  } catch (error) {
    console.error("加载情绪标签失败", error);
  }
}

/* ------------------------------------------------------------------ */
/* 交互                                                                */
/* ------------------------------------------------------------------ */
function openLightbox(index) {
  const item = state.items[index];
  if (!item) return;
  state.lbIndex = index;
  el.lbImg.src = assetUrl(item);
  const aiTags = [];
  if (item.emotion) {
    aiTags.push(`情绪：${item.emotion}`);
  }
  if (item.scene) {
    aiTags.push(`场景：${item.scene}`);
  }
  if (item.keywords && item.keywords.length) {
    aiTags.push(`关键词：${item.keywords.join("、")}`);
  }
  el.lbMeta.innerHTML =
    `<div><strong>${escapeHtml(item.filename)}</strong></div>` +
    `<div>${escapeHtml(sourceLabel(item.source))} · ` +
    `${item.width && item.height ? escapeHtml(item.width + "×" + item.height) + " · " : ""}` +
    `${escapeHtml(formatBytes(item.size))} · ${escapeHtml(formatTime(item.created_at))}</div>` +
    `<div>发送者：${escapeHtml(item.sender_name || "未知")} (${escapeHtml(item.sender_id)})</div>` +
    (aiTags.length
      ? `<div class="lb-ai">${escapeHtml(aiTags.join(" · "))}</div>`
      : "");
  el.lightbox.hidden = false;
}

function closeLightbox() {
  el.lightbox.hidden = true;
  state.lbIndex = -1;
}

function stepLightbox(delta) {
  if (state.lbIndex < 0) return;
  const next = state.lbIndex + delta;
  if (next < 0 || next >= state.items.length) return;
  openLightbox(next);
}

async function deleteAsset(id, event) {
  if (event) event.stopPropagation();
  if (!confirm("确定删除这条素材吗？此操作不可撤销。")) return;
  try {
    const data = await apiPost(`${API}/delete`, { id: id });
    if (data && data.status === "error") {
      toast(data.message || "删除失败");
      return;
    }
    toast("已删除");
    await loadAssets();
    await loadStats();
    await loadSenders();
  } catch (error) {
    console.error("删除失败", error);
    toast("删除失败：" + (error && error.message ? error.message : "未知错误"));
  }
}

async function clearAll() {
  if (!confirm("确定清空整个图库吗？所有素材都会被删除，且不可恢复。")) return;
  try {
    const data = await apiPost(`${API}/clear`, {});
    toast((data && data.message) || "已清空");
    state.page = 1;
    await loadAssets();
    await loadStats();
    await loadSenders();
  } catch (error) {
    console.error("清空失败", error);
    toast("清空失败：" + (error && error.message ? error.message : "未知错误"));
  }
}

function bindEvents() {
  $("btn-refresh").addEventListener("click", async () => {
    await loadAssets();
    await loadStats();
    await loadSenders();
    toast("已刷新");
  });

  $("btn-clear").addEventListener("click", clearAll);

  $("btn-search").addEventListener("click", doSearch);
  el.keyword.addEventListener("keydown", (event) => {
    if (event.key === "Enter") doSearch();
  });

  el.source.addEventListener("change", doSearch);
  el.order.addEventListener("change", doSearch);
  el.sender.addEventListener("change", doSearch);
  el.emotion.addEventListener("change", doSearch);
  el.animated.addEventListener("change", doSearch);
  el.static.addEventListener("change", doSearch);

  $("btn-prev").addEventListener("click", () => {
    if (state.page > 1) {
      state.page -= 1;
      loadAssets();
    }
  });
  $("btn-next").addEventListener("click", () => {
    state.page += 1;
    loadAssets();
  });

  el.grid.addEventListener("click", (event) => {
    const delBtn = event.target.closest("[data-del]");
    if (delBtn) {
      deleteAsset(Number(delBtn.getAttribute("data-del")), event);
      return;
    }
    const card = event.target.closest(".card");
    if (card) {
      openLightbox(Number(card.getAttribute("data-index")));
    }
  });

  $("lb-close").addEventListener("click", closeLightbox);
  $("lb-prev").addEventListener("click", () => stepLightbox(-1));
  $("lb-next").addEventListener("click", () => stepLightbox(1));
  el.lightbox.addEventListener("click", (event) => {
    if (event.target === el.lightbox) closeLightbox();
  });
  document.addEventListener("keydown", (event) => {
    if (el.lightbox.hidden) return;
    if (event.key === "Escape") closeLightbox();
    if (event.key === "ArrowLeft") stepLightbox(-1);
    if (event.key === "ArrowRight") stepLightbox(1);
  });
}

function doSearch() {
  state.keyword = el.keyword.value.trim();
  state.source = el.source.value;
  state.order = el.order.value;
  state.sender = el.sender.value;
  state.emotion = el.emotion.value;
  state.animated = el.animated.checked;
  state.staticOnly = el.static.checked;
  state.page = 1;
  loadAssets();
}

/* ------------------------------------------------------------------ */
/* 启动                                                                */
/* ------------------------------------------------------------------ */
async function boot() {
  bindEvents();
  const view = window.AstrBotPluginView;
  if (view && typeof view.ready === "function") {
    try {
      await view.ready();
    } catch (error) {
      console.error("等待 View 上下文失败", error);
    }
  }
  await loadStats();
  await loadSenders();
  await loadEmotions();
  await loadAssets();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
