"use strict";

const $ = (id) => document.getElementById(id);
const PAGE = 200;
// Sent with every request that changes something (the server refuses them without it).
const APP_HEADERS = { "X-Photo-Sync": "1" };

const state = {
  udid: null,          // which phone's backup is shown
  devices: [],
  items: [],           // loaded so far, in display order
  total: 0,
  loading: false,
  done: false,
  generation: 0,       // bumps on every new search so stale responses are ignored
  viewerIndex: -1,
  viewerList: null,    // when set, the viewer steps through this list (a map place) instead of the grid
  indexVersion: null,  // which index version the gallery shows
};

// ------------------------------------------------------------------ formatting

function fmtBytes(n) {
  if (n == null) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (Math.abs(n) >= 1000 && i < units.length - 1) { n /= 1000; i++; }
  return i === 0 ? `${n} B` : `${n.toFixed(1)} ${units[i]}`;
}
function fmtDuration(s) {
  if (s == null) return "";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
}
function fmtNum(n) { return (n ?? 0).toLocaleString(); }
function parseTaken(t) { return t ? new Date(t.replace(" ", "T")) : null; }
function fmtDate(t, opts = { dateStyle: "medium", timeStyle: "short" }) {
  const d = parseTaken(t);
  return d ? d.toLocaleString(undefined, opts) : "";
}
function fmtAgo(iso) {
  if (!iso) return "never";
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)} min ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)} h ago`;
  return new Date(iso).toLocaleDateString(undefined, { dateStyle: "medium" });
}
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null) node.append(c);
  return node;
}

// ------------------------------------------------------------------ filters <-> URL

function filters() {
  return {
    ext: $("ext").value,
    kind: document.querySelector("#kind-tabs .active").dataset.kind,
    from: $("from").value,
    to: $("to").value,
    camera: $("camera").value,
    sort: $("sort").value,
    live_clips: $("live-clips").checked ? "1" : "",
  };
}
// Phones fold the filters away; the button shows how many are in use.
function updateFiltersButton() {
  const f = filters();
  const n = [f.ext, f.from, f.to, f.camera, f.live_clips, f.sort !== "taken_desc"].filter(Boolean).length;
  $("filters-btn").replaceChildren("Filters", ...(n ? [el("span", { class: "count" }, String(n))] : []));
}
function saveFiltersToUrl() {
  updateFiltersButton();
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(filters())) if (v && !(k === "sort" && v === "taken_desc")) p.set(k, v);
  if (state.udid && state.devices.length > 1) p.set("phone", state.udid);
  if (currentView() === "map") p.set("view", "map");
  history.replaceState(null, "", p.toString() ? `?${p}` : location.pathname);
}
function loadFiltersFromUrl() {
  const p = new URLSearchParams(location.search);
  $("from").value = p.get("from") || "";
  $("to").value = p.get("to") || "";
  $("sort").value = p.get("sort") || "taken_desc";
  $("live-clips").checked = p.get("live_clips") === "1";
  setKind(p.get("kind") || "");
  setView(p.get("view") === "map" ? "map" : "grid", { load: false });
  return { camera: p.get("camera") || "", ext: p.get("ext") || "", phone: p.get("phone") };
}
function setKind(kind) {
  for (const b of document.querySelectorAll("#kind-tabs button")) b.classList.toggle("active", b.dataset.kind === kind);
}

// ------------------------------------------------------------------ gallery

function resetGallery() {
  state.generation++;
  state.items = [];
  state.total = 0;
  state.done = false;
  state.loading = false;
  resetThumbs();
  $("gallery").replaceChildren();
  $("empty").hidden = true;
  $("result-count").textContent = "";
  saveFiltersToUrl();
  loadMore();
}

async function loadMore() {
  if (!state.udid || state.loading || state.done) return;
  state.loading = true;
  const gen = state.generation;
  const params = new URLSearchParams({ ...filters(), offset: state.items.length, limit: PAGE });
  try {
    const res = await fetch(`/api/${state.udid}/media?${params}`);
    const data = await res.json();
    if (gen !== state.generation) return;
    const start = state.items.length;
    state.items.push(...data.items);
    state.total = data.total;
    state.done = state.items.length >= data.total || data.items.length === 0;
    $("result-count").textContent = `${fmtNum(data.total)} item${data.total === 1 ? "" : "s"} · ${fmtBytes(data.total_bytes)}`;
    renderItems(data.items, start);
    $("empty").hidden = data.total > 0;
    if (!data.total) {
      const f = filters();
      const filtered = f.ext || f.kind || f.from || f.to || f.camera;
      $("empty").textContent = filtered ? "Nothing matches these filters." : "No photos indexed yet.";
    }
  } finally {
    if (gen === state.generation) state.loading = false;
  }
  // Keep filling until the sentinel is off screen.
  if (!state.done && isSentinelVisible()) loadMore();
}

function isSentinelVisible() {
  return $("sentinel").getBoundingClientRect().top < window.innerHeight + 800;
}

function groupKey(item) {
  const sort = $("sort").value;
  if (!sort.startsWith("taken")) return null;
  const d = parseTaken(item.taken_at);
  return d ? d.toLocaleDateString(undefined, { year: "numeric", month: "long" }) : "Unknown date";
}

function renderItems(items, startIndex) {
  const gallery = $("gallery");
  let grid = gallery.lastElementChild?.classList.contains("grid") ? gallery.lastElementChild : null;
  let currentGroup = grid?.dataset.group ?? undefined;
  items.forEach((item, i) => {
    const key = groupKey(item);
    if (!grid || (key !== null && key !== currentGroup)) {
      if (key !== null) gallery.append(el("h3", { class: "group-title" }, key));
      grid = el("div", { class: "grid", "data-group": key ?? "" });
      gallery.append(grid);
      currentGroup = key;
    }
    grid.append(tile(item, startIndex + i));
  });
}

function tile(item, index, open = (i) => openViewer(i, null)) {
  const img = el("img", { decoding: "async", alt: item.name });
  const btn = el("button", { class: "tile", title: `${item.name}\n${fmtDate(item.taken_at)}`, onclick: () => open(index) }, img);
  btn.dataset.thumb = `/thumb/${state.udid}/${item.id}?v=${item.inode}`;
  img.addEventListener("load", () => img.classList.add("loaded"));
  if (item.kind === "video") btn.append(el("span", { class: "badge" }, fmtDuration(item.duration) || "VIDEO"));
  if (item.live_video_id) btn.append(el("span", { class: "badge live" }, "LIVE"));
  thumbs.observer.observe(btn);
  return btn;
}

// ------------------------------------------------------------------ thumbnail loader
// Thumbnails are made on demand and can be slow, and the browser only opens a few connections
// to the app at once. So we load a few at a time, on-screen tiles first, and cancel requests
// for tiles that have been scrolled away, instead of letting them block everything behind them.

const thumbs = {
  max: 4,               // leaves connections free for the media list and status requests
  active: new Map(),    // tile -> AbortController for thumbnails being fetched
  near: new Set(),      // tiles on or near the screen that still need a thumbnail
  observer: new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting) thumbs.near.add(e.target);
      else { thumbs.near.delete(e.target); thumbs.active.get(e.target)?.abort(); }
    }
    pumpThumbs();
  }, { rootMargin: "400px 0px" }),
};

function resetThumbs() {
  for (const ctrl of thumbs.active.values()) ctrl.abort();
  thumbs.near.clear();
  thumbs.observer.disconnect();
}

// Stop loading thumbnails for tiles that are about to be removed from the page.
function forgetTiles(container) {
  for (const t of container.querySelectorAll(".tile")) {
    thumbs.observer.unobserve(t);
    thumbs.near.delete(t);
    thumbs.active.get(t)?.abort();
  }
}

function pumpThumbs() {
  if (thumbs.active.size >= thumbs.max) return;
  const waiting = [...thumbs.near].filter((t) => !thumbs.active.has(t));
  if (!waiting.length) return;
  // On-screen tiles first (top to bottom), then the closest ones just off screen.
  const vh = window.innerHeight;
  const rank = new Map(waiting.map((t) => {
    const r = t.getBoundingClientRect();
    const onScreen = r.bottom > 0 && r.top < vh;
    return [t, onScreen ? r.top : 1e6 + (r.top < 0 ? -r.bottom : r.top - vh)];
  }));
  waiting.sort((a, b) => rank.get(a) - rank.get(b));
  for (const t of waiting) {
    if (thumbs.active.size >= thumbs.max) break;
    loadThumb(t);
  }
}

async function loadThumb(tile) {
  const ctrl = new AbortController();
  thumbs.active.set(tile, ctrl);
  try {
    const res = await fetch(tile.dataset.thumb, { signal: ctrl.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const url = URL.createObjectURL(await res.blob());
    const img = tile.querySelector("img");
    img.addEventListener("load", () => URL.revokeObjectURL(url), { once: true });
    img.src = url;
    done(tile);
  } catch (e) {
    if (e.name !== "AbortError") {
      done(tile);
      tile.append(el("span", { class: "broken" }, tile.title.split("\n")[0].split(".").pop()));
    }
  } finally {
    thumbs.active.delete(tile);
    pumpThumbs();
  }
  function done(t) { thumbs.near.delete(t); thumbs.observer.unobserve(t); }
}

new IntersectionObserver((entries) => {
  if (entries.some((e) => e.isIntersecting)) loadMore();
}, { rootMargin: "800px" }).observe($("sentinel"));

// ------------------------------------------------------------------ viewer

function videoSource(item, forceCompat = false) {
  const probe = document.createElement("video");
  const hevc = item.codec === "hevc" && !probe.canPlayType('video/mp4; codecs="hvc1"');
  return `/video/${state.udid}/${item.id}?v=${item.inode}${forceCompat || hevc ? "&compat=1" : ""}`;
}

function playVideo(stage, item) {
  const video = el("video", { controls: "", autoplay: "", playsinline: "" });
  let usingCompat = videoSource(item).includes("compat");
  const note = el("div", { class: "note" }, usingCompat ? "Converting video for this browser…" : "Loading…");
  video.src = videoSource(item);
  video.addEventListener("loadeddata", () => note.remove());
  video.addEventListener("error", () => {
    if (!usingCompat) {
      usingCompat = true;
      note.textContent = "Converting video for this browser…";
      if (!note.isConnected) stage.append(note);
      video.src = videoSource(item, true);
    } else {
      note.textContent = "This video can't be played here. Use Download original.";
    }
  });
  stage.replaceChildren(video, note);
}

function viewerItems() { return state.viewerList || state.items; }
function viewerTotal() { return state.viewerList ? state.viewerList.length : state.total; }

// list: the items to step through (null for the grid); leave it out to keep the current one.
async function openViewer(index, list) {
  if (list !== undefined) state.viewerList = list;
  if (index < 0) return;
  if (index >= viewerItems().length) {
    if (state.viewerList || state.done) return;
    await loadMore();
    if (index >= state.items.length) return;
  }
  state.viewerIndex = index;
  const item = viewerItems()[index];
  const stage = $("v-stage");
  $("viewer").hidden = false;
  document.body.style.overflow = "hidden";

  if (item.kind === "video") {
    playVideo(stage, item);
  } else {
    const img = el("img", { alt: item.name, src: `/view/${state.udid}/${item.id}?v=${item.inode}` });
    const note = el("div", { class: "note" }, "Loading…");
    img.addEventListener("load", () => note.remove());
    img.addEventListener("error", () => { note.textContent = "This image can't be shown here. Use Download original."; });
    stage.replaceChildren(img, note);
  }
  renderInfo(item);
  $("v-prev").style.visibility = index > 0 ? "visible" : "hidden";
  $("v-next").style.visibility = index < viewerTotal() - 1 ? "visible" : "hidden";
  // Warm the cache for the next image.
  const next = viewerItems()[index + 1];
  if (next && next.kind !== "video") new Image().src = `/view/${state.udid}/${next.id}?v=${next.inode}`;
}

function renderInfo(item) {
  const rows = [
    ["Taken", fmtDate(item.taken_at, { dateStyle: "full", timeStyle: "short" }) +
      (item.taken_source === "file" ? " (from file date)" : "")],
    ["Type", { photo: "Photo", video: "Video", screenshot: "Screenshot / PNG" }[item.kind] +
      (item.live_video_id ? " (Live Photo)" : "") + (item.is_live_video ? " (Live Photo clip)" : "")],
    ["Size", fmtBytes(item.size)],
    ["Dimensions", item.width ? `${item.width} × ${item.height}` : null],
    ["Duration", item.duration ? fmtDuration(item.duration) : null],
    ["Codec", item.codec],
    ["Camera", item.camera],
    ["Location", item.lat != null ? `${item.lat.toFixed(5)}, ${item.lon.toFixed(5)}` : item.has_location ? "Has GPS location" : null],
    ["Path", item.path],
  ].filter(([, v]) => v);
  const actions = el("div", { class: "actions" },
    el("a", { class: "btn small", href: `/original/${state.udid}/${item.id}` }, "Download original"));
  if (item.lat != null) actions.append(el("button", { class: "btn small", onclick: () => showOnMap(item) }, "Show on map"));
  if (item.live_video_id) {
    actions.prepend(el("button", {
      class: "btn small",
      onclick: () => playVideo($("v-stage"), { id: item.live_video_id, codec: item.live_codec, inode: item.live_inode, kind: "video" }),
    }, "Play Live Photo"));
  }
  $("v-info").replaceChildren(
    el("h2", {}, item.name),
    el("div", { style: "color:#8b93a1" }, `${fmtNum(state.viewerIndex + 1)} of ${fmtNum(viewerTotal())}`),
    el("dl", {}, rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)])),
    actions,
  );
}

function closeViewer() {
  $("viewer").hidden = true;
  $("v-stage").replaceChildren();
  document.body.style.overflow = "";
  state.viewerIndex = -1;
  state.viewerList = null;
  $("viewer").classList.remove("show-info");
}

$("v-close").onclick = closeViewer;
$("v-prev").onclick = () => openViewer(state.viewerIndex - 1);
$("v-next").onclick = () => openViewer(state.viewerIndex + 1);
$("v-stage").addEventListener("click", (e) => { if (e.target === $("v-stage")) closeViewer(); });
$("v-info-btn").onclick = () => $("viewer").classList.toggle("show-info");

// Phones: swipe left/right for the next/previous photo, swipe down to close.
let swipe = null;
$("v-stage").addEventListener("touchstart", (e) => {
  const zoomed = window.visualViewport && window.visualViewport.scale > 1.01;
  swipe = e.touches.length === 1 && !zoomed ? { x: e.touches[0].clientX, y: e.touches[0].clientY } : null;
}, { passive: true });
$("v-stage").addEventListener("touchend", (e) => {
  if (!swipe || e.changedTouches.length !== 1) return;
  const dx = e.changedTouches[0].clientX - swipe.x, dy = e.changedTouches[0].clientY - swipe.y;
  swipe = null;
  if (Math.abs(dx) > 50 && Math.abs(dx) > 1.5 * Math.abs(dy)) openViewer(state.viewerIndex + (dx < 0 ? 1 : -1));
  else if (dy > 90 && dy > 1.5 * Math.abs(dx)) closeViewer();
}, { passive: true });
document.addEventListener("keydown", (e) => {
  if ($("viewer").hidden) return;
  if (e.key === "Escape") closeViewer();
  else if (e.key === "ArrowLeft") openViewer(state.viewerIndex - 1);
  else if (e.key === "ArrowRight") openViewer(state.viewerIndex + 1);
});

// ------------------------------------------------------------------ map view

const places = {
  map: null,          // Leaflet map, made the first time the map is shown
  cluster: null,      // groups nearby markers into one pin with a count
  generation: 0,      // ignore responses to older requests
  loadedKey: null,    // filters the markers on the map were loaded for
};
const PANEL_LIMIT = 500;  // most items listed for one place (newest first)

function currentView() { return document.querySelector("#view-tabs .active")?.dataset.view || "grid"; }

function setView(view, { load = true } = {}) {
  for (const b of document.querySelectorAll("#view-tabs button")) b.classList.toggle("active", b.dataset.view === view);
  const isMap = view === "map";
  document.body.classList.toggle("map-mode", isMap);
  $("map-view").hidden = !isMap;
  if (load) refreshView();
}

// Show the current filters in whichever view is active.
function refreshView({ fit = true } = {}) {
  if (currentView() === "map") loadPlaces({ fit });
  else resetGallery();
}

function ensureMap() {
  if (places.map) return;
  places.map = L.map("map", { worldCopyJump: true, minZoom: 2 }).setView([25, 0], 2);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(places.map);
  places.cluster = L.markerClusterGroup({
    chunkedLoading: true, showCoverageOnHover: false, maxClusterRadius: 70, iconCreateFunction: clusterIcon,
    // Clicking a group zooms in until it splits; photos taken at the very same spot can never
    // split, so those fan out around the pin instead.
    zoomToBoundsOnClick: true, spiderfyOnMaxZoom: true,
  });
  // A group also lists all of its photos beside the map; a single photo opens straight away.
  places.cluster.on("clusterclick", (e) => showPlace(e.layer.getAllChildMarkers()));
  places.cluster.on("click", (e) => openPin(e.layer));
  places.map.addLayer(places.cluster);
  $("place-close").onclick = hidePlace;
}

function pinIcon(marker, count) {
  const { mediaId, inode } = marker.options;
  const size = count > 1 ? 56 : 46;
  return L.divIcon({
    className: "map-pin", iconSize: [size, size],
    html: `<div class="ph"><img loading="lazy" alt="" src="/thumb/${state.udid}/${mediaId}?v=${inode}">` +
          (count > 1 ? `<span class="n">${fmtNum(count)}</span>` : "") + "</div>",
  });
}

function clusterIcon(cluster) {
  // Show the newest photo in the group (markers are numbered newest first).
  let newest = null;
  for (const m of cluster.getAllChildMarkers()) if (!newest || m.options.order < newest.options.order) newest = m;
  return pinIcon(newest, cluster.getChildCount());
}

async function loadPlaces({ fit = true } = {}) {
  if (!state.udid) return;
  ensureMap();
  saveFiltersToUrl();
  places.map.invalidateSize();  // the map may have just become visible
  const params = new URLSearchParams(filters());
  params.delete("sort");
  const key = `${state.udid}?${params}`;
  const gen = ++places.generation;
  $("result-count").textContent = "Loading map…";
  const data = await (await fetch(`/api/${state.udid}/places?${params}`)).json();
  if (gen !== places.generation) return;

  places.cluster.clearLayers();
  if (key !== places.loadedKey) hidePlace();
  places.loadedKey = key;
  const markers = data.points.map(([id, lat, lon, inode], order) => {
    const m = L.marker([lat, lon], { mediaId: id, inode, order });
    m.setIcon(pinIcon(m, 1));
    return m;
  });
  places.cluster.addLayers(markers);
  const n = data.points.length;
  $("result-count").textContent = (n ? `${fmtNum(n)} item${n === 1 ? "" : "s"} with a location` :
    "Nothing with a location matches these filters.") +
    (data.pending ? ` · reading locations for ${fmtNum(data.pending)} more…` : "");
  if (fit && n) places.map.fitBounds(places.cluster.getBounds(), { padding: [40, 40], maxZoom: 15 });
}

async function showPlace(markers) {
  markers = [...markers].sort((a, b) => a.options.order - b.options.order);
  const ids = markers.slice(0, PANEL_LIMIT).map((m) => m.options.mediaId);
  const gen = places.generation;
  $("place-panel").hidden = false;
  $("place-title").textContent = `${fmtNum(markers.length)} item${markers.length === 1 ? "" : "s"}`;
  $("place-note").textContent = markers.length > PANEL_LIMIT ? `Showing the newest ${PANEL_LIMIT}. Zoom in to see the rest.` : "Newest first";
  forgetTiles($("place-grid"));
  $("place-grid").replaceChildren();
  const res = await fetch(`/api/${state.udid}/items?ids=${ids.join(",")}`);
  const { items } = await res.json();
  if (gen !== places.generation) return;
  $("place-grid").replaceChildren(...items.map((item, i) => tile(item, i, (j) => openViewer(j, items))));
}

async function openPin(marker) {
  const res = await fetch(`/api/${state.udid}/items?ids=${marker.options.mediaId}`);
  const { items } = await res.json();
  if (items.length) openViewer(0, items);
}

function hidePlace() {
  forgetTiles($("place-grid"));
  $("place-panel").hidden = true;
  $("place-grid").replaceChildren();
}

function showOnMap(item) {
  closeViewer();
  if (currentView() !== "map") {
    setView("map", { load: false });
    loadPlaces({ fit: false });
  }
  places.map.invalidateSize();
  places.map.setView([item.lat, item.lon], 16);
}

$("view-tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (b && b.dataset.view !== currentView()) setView(b.dataset.view);
});

// ------------------------------------------------------------------ facets

// preferred: dropdown choices to restore once their options exist, e.g. from the URL.
async function loadFacets(preferred = {}) {
  if (!state.udid) return;
  const f = await (await fetch(`/api/${state.udid}/facets`)).json();
  for (const b of document.querySelectorAll("#kind-tabs button")) {
    b.querySelector(".count")?.remove();
    const n = f.kinds[b.dataset.kind];
    if (b.dataset.kind && n != null) b.append(el("span", { class: "count" }, fmtNum(n)));
  }
  fillSelect($("camera"), "All cameras", f.cameras, preferred.camera);
  fillSelect($("ext"), "All file types", f.exts, preferred.ext);
  if (f.first) { $("from").min = $("to").min = f.first.slice(0, 10); }
  if (f.last) { $("from").max = $("to").max = f.last.slice(0, 10); }
}

function fillSelect(select, allLabel, options, preferred) {
  const current = preferred || select.value;
  select.replaceChildren(el("option", { value: "" }, allLabel),
    ...options.map((o) => el("option", { value: o.name }, `${o.name} (${fmtNum(o.count)})`)));
  select.value = options.some((o) => o.name === current) ? current : "";
}

// ------------------------------------------------------------------ status

function renderSummary(device) {
  const box = $("sync-summary");
  if (!device) { box.replaceChildren(); return; }
  const bad = (s) => s && !["success", "no_changes", "in_progress"].includes(s);
  const status = device.last_sync_status;
  box.replaceChildren(el("div", {}, "Last backup ", el("span", { class: bad(status) ? "bad" : "" },
    status === "in_progress" ? "running now" : fmtAgo(device.last_sync_at) + (bad(status) ? ` (${status})` : ""))));
}

function renderDevices(devices) {
  const sel = $("device-select");
  sel.hidden = devices.length < 2;
  const known = [...sel.options].map((o) => o.value).join();
  if (known !== devices.map((d) => d.udid).join()) {
    sel.replaceChildren(...devices.map((d) => el("option", { value: d.udid }, d.name || d.udid)));
  }
  sel.value = state.udid || "";
}

function renderIndexing(device) {
  const ix = device?.indexing;
  const banner = $("indexing");
  if (ix && ix.running && ix.total) {
    banner.hidden = false;
    banner.textContent = `Indexing new files… ${fmtNum(ix.done)} / ${fmtNum(ix.total)}. They'll appear as they're ready.`;
  } else if (ix && ix.error) {
    banner.hidden = false;
    banner.textContent = `Indexing failed: ${ix.error}`;
  } else {
    banner.hidden = true;
  }
  // Reload the gallery only when a finished refresh actually changed the index.
  const running = !!(ix && ix.running);
  const version = ix ? `${device.udid}:${ix.version}` : null;
  if (!running && version !== null) {
    if (state.indexVersion != null && state.indexVersion !== version && state.indexVersion.startsWith(device.udid + ":")) {
      loadFacets(); refreshView({ fit: false });
    }
    state.indexVersion = version;
  }
}

async function poll() {
  let s;
  try {
    s = await (await fetch("/api/status")).json();
  } catch {
    return setTimeout(poll, 3000);
  }
  state.devices = s.devices;
  if (!state.udid && s.devices.length) {
    state.udid = s.devices[0].udid;
    await loadFacets(state.pendingFilters);
    refreshView();
  }
  if (!s.devices.length) {
    $("empty").hidden = false;
    $("empty").textContent = "No photos yet. Back up a phone with the Photo Sync desktop app, or press Back up photos.";
  }
  renderDevices(s.devices);
  const device = s.devices.find((d) => d.udid === state.udid);
  renderSummary(device);
  renderIndexing(device);

  $("upload-btn").disabled = !state.udid;
  if (state.udid && state.uploadInfoFor !== state.udid) refreshUploadInfo();
  setTimeout(poll, 2500);
}

// ------------------------------------------------------------------ backing up from this device
//
// Photos picked here are sent to the server in pieces, so a dropped connection or a locked
// screen resumes instead of starting over. The server skips photos it already has.

let up = { running: false };

function newUploadRun() {
  return { running: true, stopped: false, udid: state.udid, all: [], queue: [], xhrs: new Set(),
           startedAt: Date.now(), wakeLock: null, waiting: false };
}

// Must match uploads.quick_fingerprint on the server: SHA-256 of the size, the first 64 KiB
// and the last 64 KiB.
async function fingerprint(file) {
  const Q = 64 * 1024;
  const parts = [new TextEncoder().encode(String(file.size)),
                 new Uint8Array(await file.slice(0, Q).arrayBuffer()),
                 new Uint8Array(await file.slice(Math.max(0, file.size - Q)).arrayBuffer())];
  const all = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) { all.set(p, at); at += p.length; }
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", all));
  return [...digest].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function httpError(status, data) {
  const e = new Error((data && data.error) || `Server error ${status}`);
  e.status = status;
  e.data = data || {};
  return e;
}

async function apiJSON(method, url, body) {
  let res;
  try {
    res = await fetch(url, { method, headers: { ...APP_HEADERS, "Content-Type": "application/json" },
                             body: body ? JSON.stringify(body) : undefined });
  } catch {
    const e = new Error("Connection lost");
    e.network = true;
    throw e;
  }
  let data = {};
  try { data = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) throw httpError(res.status, data);
  return data;
}

// XMLHttpRequest rather than fetch, because it reports upload progress within a piece.
function putPiece(url, blob, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    up.xhrs.add(xhr);
    const done = () => up.xhrs.delete(xhr);
    xhr.open("PUT", url);
    for (const [k, v] of Object.entries(APP_HEADERS)) xhr.setRequestHeader(k, v);
    xhr.upload.onprogress = (e) => onProgress(e.loaded);
    xhr.onload = () => {
      done();
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data); else reject(httpError(xhr.status, data));
    };
    xhr.onerror = xhr.ontimeout = () => { done(); const e = new Error("Connection lost"); e.network = true; reject(e); };
    xhr.onabort = () => { done(); const e = new Error("Stopped"); e.stopped = true; reject(e); };
    xhr.send(blob);
  });
}

// Connection problems and server hiccups are worth waiting out; a refusal (bad file type,
// out of space, ...) is not.
function retryable(e) {
  return e.network || !e.status || e.status >= 500 && e.status !== 507 || e.status === 408 || e.status === 429;
}

async function waitToRetry(entry) {
  entry.failures = (entry.failures || 0) + 1;
  if (entry.failures > 40) throw new Error("Gave up after the connection kept failing");
  up.waiting = true;
  renderUpload();
  const delay = Math.min(30000, 1000 * 2 ** Math.min(entry.failures, 5));
  await new Promise((resolve) => {
    const t = setTimeout(resolve, delay);
    if (!navigator.onLine) window.addEventListener("online", () => { clearTimeout(t); resolve(); }, { once: true });
  });
  up.waiting = false;
  if (up.stopped) { const e = new Error("Stopped"); e.stopped = true; throw e; }
}

async function withRetry(entry, fn) {
  for (;;) {
    try { return await fn(); } catch (e) {
      if (up.stopped || e.stopped || !retryable(e)) throw e;
      await waitToRetry(entry);
    }
  }
}

async function uploadFile(entry, restarts = 0) {
  const f = entry.file;
  entry.state = "checking";
  renderUpload();
  const base = `/api/${up.udid}/upload`;
  const fp = await fingerprint(f);
  const start = await withRetry(entry, () => apiJSON("POST", `${base}/start`,
    { name: f.name, size: f.size, modified: f.lastModified, fingerprint: fp }));
  if (start.status === "exists") { entry.state = "skipped"; entry.sent = f.size; return; }

  entry.state = "sending";
  let offset = start.offset;
  entry.sent = offset;
  while (offset < f.size) {
    if (up.stopped) { const e = new Error("Stopped"); e.stopped = true; throw e; }
    const end = Math.min(f.size, offset + start.chunk);
    try {
      const r = await putPiece(`${base}/${start.id}?offset=${offset}`, f.slice(offset, end),
                               (loaded) => { entry.sent = offset + loaded; renderUpload(); });
      offset = r.offset;
      entry.failures = 0;
    } catch (e) {
      if (e.stopped || up.stopped) throw e;
      if (e.status === 404 && restarts < 1) return uploadFile(entry, restarts + 1);  // server forgot it: start over
      if (e.data && e.data.offset != null) offset = e.data.offset;  // continue from what the server has
      if (e.status === 409) continue;
      if (!retryable(e) && e.status !== 400) throw e;
      await waitToRetry(entry);
    }
    entry.sent = offset;
  }
  try {
    const done = await withRetry(entry, () => apiJSON("POST", `${base}/${start.id}/finish`));
    entry.state = done.status === "exists" ? "skipped" : "saved";
  } catch (e) {
    if (e.status === 422 && restarts < 1) return uploadFile(entry, restarts + 1);  // arrived damaged: send again
    throw e;
  }
}

async function uploadWorker() {
  while (up.queue.length && !up.stopped) {
    const entry = up.queue.shift();
    try {
      await uploadFile(entry);
    } catch (e) {
      entry.state = e.stopped || up.stopped ? "stopped" : "failed";
      entry.error = e.message;
    }
    renderUpload();
  }
}

async function keepScreenOn() {
  try { up.wakeLock = await navigator.wakeLock?.request("screen"); } catch { /* not supported or refused */ }
}

async function startUpload(files) {
  const entries = [...files].map((file) => ({ file, state: "waiting", sent: 0 }));
  if (!entries.length || !state.udid) return;
  if (up.running) {  // add to the run in progress
    up.all.push(...entries);
    up.queue.push(...entries);
    renderUpload();
    return;
  }
  up = newUploadRun();
  up.all = entries;
  up.queue = [...entries];
  $("upload-panel").hidden = false;
  $("upload-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
  await keepScreenOn();
  renderUpload();
  await Promise.all([uploadWorker(), uploadWorker()]);
  up.running = false;
  try { await up.wakeLock?.release(); } catch { /* already released */ }
  renderUpload();
  refreshUploadInfo();
}

let uploadRenderQueued = false;
function renderUpload() {
  if (uploadRenderQueued) return;
  uploadRenderQueued = true;
  requestAnimationFrame(() => { uploadRenderQueued = false; drawUpload(); });
}

function drawUpload() {
  const all = up.all || [];
  const count = (s) => all.filter((e) => e.state === s).length;
  const saved = count("saved"), skipped = count("skipped"), failed = count("failed");
  const total = all.length;
  const totalBytes = all.reduce((n, e) => n + e.file.size, 0);
  const doneBytes = all.reduce((n, e) => n + (e.state === "failed" ? 0 : Math.min(e.sent || 0, e.file.size)), 0);
  const word = (n) => `${fmtNum(n)} ${n === 1 ? "photo" : "photos"}`;

  $("upload-title").textContent = up.running ? `Backing up ${word(total)}…`
    : up.stopped ? "Backup stopped"
    : failed ? `Backed up ${word(saved + skipped)}; ${fmtNum(failed)} couldn't be sent`
    : `All ${word(total)} are backed up`;
  $("upload-fill").style.width = `${totalBytes ? (doneBytes / totalBytes * 100).toFixed(1) : 100}%`;
  const parts = [`${fmtNum(saved + skipped)} of ${fmtNum(total)} done`];
  if (skipped) parts.push(`${fmtNum(skipped)} ${skipped === 1 ? "was" : "were"} already backed up`);
  parts.push(`${fmtBytes(doneBytes)} of ${fmtBytes(totalBytes)}`);
  if (up.running) {
    const secs = (Date.now() - up.startedAt) / 1000;
    const speed = doneBytes / Math.max(secs, 1);
    if (secs > 5 && speed > 0) parts.push(`${fmtBytes(speed)}/s`, `about ${fmtDuration((totalBytes - doneBytes) / speed)} left`);
  }
  $("upload-numbers").textContent = parts.join("  ·  ");

  const note = $("upload-note");
  note.classList.toggle("warn", up.running && up.waiting);
  note.textContent = !up.running ? (failed ? "Pick them again to retry. Photos already backed up are skipped." :
                                   up.stopped ? "Pick the photos again to continue where it stopped." :
                                   "Your photos are saved on the server.")
    : up.waiting ? "Connection lost. It will keep trying, and carry on where it left off."
    : "Keep this page open until it finishes. Switching apps or locking the phone pauses it.";
  $("upload-stop").hidden = !up.running;
  $("upload-dismiss").hidden = up.running;

  const problems = all.filter((e) => e.state === "failed");
  $("upload-log").replaceChildren(...problems.slice(-20).map((e) =>
    el("li", { class: "error" }, `${e.file.name}: ${e.error}`)));
}

async function refreshUploadInfo() {
  if (!state.udid) return;
  state.uploadInfoFor = state.udid;
  try {
    const info = await (await fetch(`/api/${state.udid}/upload/info`)).json();
    const text = info.last_upload_at ? `Last backup ${fmtAgo(info.last_upload_at)}` : "";
    $("upload-last").textContent = text;
    $("upload-btn").title = text || "Pick photos and videos to save on the server";
  } catch { /* not important */ }
}

$("upload-btn").onclick = () => $("upload-input").click();
$("upload-input").addEventListener("change", (e) => {
  startUpload(e.target.files);
  e.target.value = "";  // so picking the same photos again still triggers this
});
$("upload-stop").onclick = () => {
  up.stopped = true;
  for (const xhr of up.xhrs) xhr.abort();
};
$("upload-dismiss").onclick = () => { $("upload-panel").hidden = true; };
// The screen lock is released when the page is hidden; take it again when it's back.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && up.running) keepScreenOn();
});
window.addEventListener("beforeunload", (e) => {
  if (up.running) { e.preventDefault(); e.returnValue = ""; }
});

// ------------------------------------------------------------------ filter events

$("filters-btn").onclick = () => {
  const open = document.body.classList.toggle("filters-open");
  $("filters-btn").setAttribute("aria-expanded", String(open));
};

for (const id of ["ext", "from", "to", "camera", "sort", "live-clips"]) $(id).addEventListener("change", () => refreshView());
$("kind-tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  setKind(b.dataset.kind);
  if (b.dataset.kind === "video" && $("sort").value.startsWith("size")) $("sort").value = "duration_desc";
  refreshView();
});
$("clear-btn").onclick = () => {
  $("ext").value = $("from").value = $("to").value = $("camera").value = "";
  $("sort").value = "taken_desc";
  $("live-clips").checked = false;
  setKind("");
  refreshView();
};
$("device-select").addEventListener("change", async (e) => {
  state.udid = e.target.value;
  await loadFacets();
  refreshView();
});

// ------------------------------------------------------------------ start

const initial = loadFiltersFromUrl();
state.pendingFilters = { camera: initial.camera, ext: initial.ext };
if (initial.phone) state.udid = initial.phone;
if (state.udid) loadFacets(state.pendingFilters).then(() => refreshView());
poll();
