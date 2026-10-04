"use strict";

// Public searches are bookmarkable. Private searches stay in memory.
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const FORMAT = {
  ebook: ["Read", "Calibre-Web"],
  audiobook: ["Listen", "Audiobookshelf"],
  readalong: ["Read-along", "Storyteller"],
  movie: ["Watch", "Jellyfin"],
  series: ["Watch", "Jellyfin"],
  comic: ["Read", "Komga"],
  game: ["Play / open in RomM", "RomM"],
  scene: ["Watch in Stash", "Stash"],
};
const BADGE = { ebook: "Ebook", audiobook: "Audio", readalong: "Read-along", movie: "Movie", series: "TV", comic: "Comic", game: "Game", scene: "Scene" };
const KINDS = [["", "All"], ["book", "Books"], ["movie", "Movies"], ["show", "TV"], ["comic", "Comics"], ["game", "Games"]];
const STATUS = { unread: "Not started", in_progress: "In progress", finished: "Finished" };
const AVAILABILITY = { in_library: "In library", partial: "Partial", coming: "On its way" };
const SOURCE = { calibre: "Calibre", abs: "Audiobookshelf", storyteller: "Storyteller", jellyfin: "Jellyfin", komga: "Komga", romm: "RomM", stash: "Stash" };
const HAS = { ebook: "Read", audiobook: "Listen", readalong: "Read along", movie: "Movie", series: "TV", comic: "Comic", game: "Play" };
const TEXT_PARAMS = ["author", "narrator", "series", "universe", "year_min", "year_max", "dur_min", "dur_max", "rating_min", "added_days"];
const BOOL_PARAMS = ["in_series", "linked", "hidden"];
const LIST_PARAMS = ["kind", "status", "has", "genre", "library", "source", "format", "availability"];
const PAGE = 60;
const HOME_ROWS = [
  { title: "Continue", note: "Pick up where you left off", params: { status: "in_progress", sort: "recent", order: "desc" } },
  { title: "Recently added", note: "Fresh arrivals across every app", params: { sort: "added", order: "desc" } },
  { title: "On its way", note: "Movies and shows coming to your library", params: { kind: "movie,show", availability: "coming", sort: "added", order: "desc" } },
  { title: "Read-alongs", note: "Words and narration, perfectly in sync", params: { has: "readalong", sort: "added", order: "desc" } },
  { title: "Movies & TV", note: "Your screen library", params: { kind: "movie,show", sort: "added", order: "desc" } },
  { title: "Games", note: "Another world to play in", params: { kind: "game", sort: "added", order: "desc" }, optional: true },
];

function publicParams(hash) {
  if (String(hash) === "settings") hash = "view=settings";
  const clean = new URLSearchParams(hash);
  for (const key of [...clean.keys()]) {
    if (![...TEXT_PARAMS, ...BOOL_PARAMS, ...LIST_PARAMS, "q", "sort", "order", "view"].includes(key)) clean.delete(key);
  }
  if (!["activity", "settings"].includes(clean.get("view"))) clean.delete("view");
  for (const key of ["kind", "has", "format", "source"]) {
    const value = (clean.get(key) || "").split(",").filter((item) => item && !["scene", "stash"].includes(item.toLowerCase())).join(",");
    if (value) clean.set(key, value); else clean.delete(key);
  }
  return clean;
}
let params = publicParams(location.hash.slice(1));
let offset = 0;
let runSequence = 0;
let setupMode = false;
let inviteToken = new URLSearchParams(location.hash.slice(1)).get("invite") || "";
let inviteReady = false;
let currentUser = null;
let permissions = {};
const isAdmin = () => currentUser?.role === "admin";
const canRequest = () => isAdmin() || permissions.can_request === true;
const canSubmitRequest = () => canRequest() || permissions.can_ask === true;
const requestLabel = (label = "Request") => canRequest() ? label : "Ask for it";
const requestResultLabel = (result) => result.queued ? "Sent for approval" : "Requested";
const requestResultMessage = (result, fallback) => result.message || (result.queued ? "Sent to an admin for approval" : fallback);
const REQUEST_HINT = "Your account can browse and play. Ask an admin to request things.";
const requestHint = () => `<p class="hint permission-hint">${REQUEST_HINT}</p>`;
let privateMode = false;
let publicState = new URLSearchParams(params);
let viewEpoch = 0;
let adultStatus = { enabled: false, pin_set: false, unlocked: false, until: 0 };
let adultEnabled = false;
let adultStatusSequence = 0;
let expiryTimer;
let pinSetup = false;
let gateSequence = 0;
let kindFacets = {};

function privateUnlocked() {
  return adultStatus.enabled && adultStatus.unlocked && Number(adultStatus.until) * 1000 > Date.now();
}

function isPrivateWork(work) {
  return Boolean(work.adult || work.kind === "scene" || work.formats?.includes("scene") || String(work.cover || "").startsWith("stash:") || work.editions?.some((edition) => edition.format === "scene" || edition.source === "stash"));
}

function allowedWork(work) {
  return privateMode ? privateUnlocked() && isPrivateWork(work) : !isPrivateWork(work);
}

function scopedQuery(values) {
  const query = new URLSearchParams(values);
  query.delete("private");
  query.set("adult", privateMode ? "only" : "exclude");
  return query;
}

function privateNotice(message = "") {
  $("#private-notice").textContent = message;
  $("#private-notice").hidden = !message;
}

function clearView() {
  ++viewEpoch;
  ++runSequence;
  clearTimeout(queryTimer);
  detailController?.abort();
  stopActivity();
  liveCache.clear();
  $("#activity-body").replaceChildren();
  activityData = null;
  approvalData = null;
  approvalError = "";
  approvalNotes.clear();
  approvalMessages.clear();
  kindFacets = {};
  updateActivityBadge();
  if ($("#detail").open) $("#detail").close();
  $("#detail-body").replaceChildren();
  clearRequestSearch();
  for (const id of ["grid", "home-rows", "count", "active-filters", "results-title"]) $(`#${id}`).replaceChildren();
  $("#more").hidden = true;
  closeFilters();
}

function updatePrivateChrome() {
  $("#private-toggle").hidden = !adultStatus.enabled || !adultStatus.allowed;
  $("#private-toggle").setAttribute("aria-pressed", String(privateMode));
  $("#private-banner").hidden = !privateMode;
  document.body.classList.toggle("private-mode", privateMode);
  $("#author-label").textContent = privateMode ? "Performer" : "Author / company";
  $("#author").placeholder = privateMode ? "Any performer" : "Any author or company";
  if (privateMode) $("#private-until").textContent = `Private · locks at ${new Date(Number(adultStatus.until) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
  clearTimeout(expiryTimer);
  if (privateMode) expiryTimer = setTimeout(() => {
    adultStatus.unlocked = false;
    leavePrivate("Private collection locked.");
  }, Math.min(2147483647, Math.max(0, Number(adultStatus.until) * 1000 - Date.now())));
}

function leavePrivate(message = "", navigate = true) {
  window.OmnarrReader?.closeAll();
  clearView();
  privateMode = false;
  params = new URLSearchParams(publicState);
  offset = 0;
  updatePrivateChrome();
  renderFacets();
  syncControls();
  privateNotice(message);
  if (navigate) { writeHistory(true); run(); }
}

function enterPrivate() {
  if (!privateUnlocked()) return;
  if (!privateMode) {
    window.OmnarrReader?.closeAll();
    publicState = new URLSearchParams(params);
    clearView();
    privateMode = true;
    params = new URLSearchParams();
    offset = 0;
  }
  $("#private-dialog").close();
  privateNotice();
  updatePrivateChrome();
  renderFacets();
  writeHistory();
  syncControls();
  run();
}

// PIN failures belong to this dialog, never to the main login screen.
async function adultApi(action, pin) {
  try {
    return await api(`api/adult/${action}`, {
      method: action === "status" ? "GET" : "POST", cache: "no-store", authRequest: true,
      signal: AbortSignal.timeout(10000),
      ...(pin === undefined ? {} : { body: JSON.stringify({ pin }) }),
    });
  } catch (error) {
    if (action === "status" && error.status === 404) return { enabled: false, allowed: false };
    throw error;
  }
}

async function checkAdultStatus() {
  const sequence = ++adultStatusSequence;
  try {
    const status = await adultApi("status");
    if (sequence !== adultStatusSequence) return false;
    adultStatus = { ...status, enabled: adultEnabled && Boolean(status.enabled) && Boolean(status.allowed) };
    if (privateMode && !privateUnlocked()) leavePrivate(status.enabled ? "Private collection locked." : "");
    updatePrivateChrome();
    if (!adultStatus.enabled) { $("#private-dialog").close(); privateNotice(); }
    return true;
  } catch {
    if (sequence !== adultStatusSequence) return false;
    adultStatus.unlocked = false;
    if (privateMode) leavePrivate("Couldn’t verify access. Open Private to try again.");
    return false;
  }
}

async function openPrivate() {
  const sequence = ++gateSequence;
  $("#private-toggle").disabled = true;
  try {
    const checked = await checkAdultStatus();
    if (sequence !== gateSequence || $("#app").hidden) return;
    if (!checked) { privateNotice("Couldn’t check Private access. Try again."); return; }
    if (!adultStatus.enabled) return;
    if (privateUnlocked()) { enterPrivate(); return; }
    pinSetup = !adultStatus.pin_set;
    $("#pin-form").reset();
    $("#pin-error").textContent = "";
    $("#pin-title").textContent = pinSetup ? "Choose a PIN" : "Unlock Private";
    $("#pin-copy").textContent = pinSetup ? "Choose 4–12 digits for your private collection." : "Enter your PIN to open your private collection.";
    $("#pin-confirm-field").hidden = !pinSetup;
    $("#pin-confirm").required = pinSetup;
    $("#pin-submit").textContent = pinSetup ? "Set PIN & open" : "Unlock";
    $("#private-dialog").showModal();
    $("#pin").focus();
  } finally { $("#private-toggle").disabled = false; }
}

$("#private-toggle").addEventListener("click", openPrivate);
$("#private-exit").addEventListener("click", () => leavePrivate());
$("#pin-close").addEventListener("click", () => $("#private-dialog").close());
$("#private-dialog").addEventListener("close", () => { ++gateSequence; $("#pin-form").reset(); });
$("#pin-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if ($("#pin-submit").disabled) return;
  const pin = $("#pin").value;
  if (!/^[0-9]{4,12}$/.test(pin)) { $("#pin-error").textContent = "Use 4–12 digits."; return; }
  if (pinSetup && pin !== $("#pin-confirm").value) { $("#pin-error").textContent = "Those PINs don’t match."; return; }
  const sequence = gateSequence;
  $("#pin-submit").disabled = true;
  $("#pin-error").textContent = "";
  try {
    await adultApi(pinSetup ? "setup" : "unlock", pin);
    if (sequence !== gateSequence) return;
    if (await checkAdultStatus() && sequence === gateSequence && privateUnlocked()) enterPrivate();
    else if ($("#private-dialog").open) $("#pin-error").textContent = "Couldn’t verify access. Close and try again.";
  } catch (error) {
    if (sequence === gateSequence) { $("#pin-error").textContent = error.message; $("#pin").value = ""; $("#pin").focus(); }
  } finally { $("#pin-submit").disabled = false; }
});
$("#private-lock").addEventListener("click", async () => {
  ++adultStatusSequence;
  adultStatus.unlocked = false;
  leavePrivate("Locking private collection…");
  $("#private-toggle").disabled = true;
  try {
    await adultApi("lock");
    privateNotice("Private collection locked.");
  } catch (error) { privateNotice(`Private content hidden. Lock could not be confirmed: ${error.message} Open Private to retry.`); }
  finally { $("#private-toggle").disabled = false; }
});
setInterval(() => { if (privateMode && !document.hidden) checkAdultStatus(); }, 15000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && privateMode) {
    if (!privateUnlocked()) leavePrivate("Private collection locked.");
    else checkAdultStatus();
  }
});
window.addEventListener("focus", () => { if (privateMode) checkAdultStatus(); });

const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
const hours = (seconds) => {
  if (!seconds) return "";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  const mins = Math.round((seconds % 3600) / 60);
  return `${Math.floor(seconds / 3600)} hr${mins ? ` ${mins} min` : ""}`;
};
const safeUrl = (value) => {
  try {
    const url = new URL(value, location.href);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch { return ""; }
};

async function api(path, options = {}) {
  const { privateRequest = false, authRequest = false, ...fetchOptions } = options;
  const epoch = viewEpoch;
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: options.body instanceof FormData ? {} : { "Content-Type": "application/json" },
    ...fetchOptions,
  });
  if (response.status === 401 && !authRequest) {
    if (privateRequest) {
      if (epoch === viewEpoch && privateMode) {
        adultStatus.unlocked = false;
        leavePrivate("Private collection locked. Open Private to unlock it again.");
      }
      throw new Error("Private collection locked.");
    }
    showAuth(false);
    throw new Error("login");
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.message || body.detail || body.error || response.statusText || "Something went wrong");
    error.status = response.status;
    throw error;
  }
  return body;
}

function setupPasswordSection(status) {
  const viaHA = Boolean(status.ha_ingress);
  const hasPassword = Boolean(status.user?.has_password ?? status.has_password);
  // Inside Home Assistant you're already signed in, so the current password is never asked for.
  $("#settings-password-current-field").hidden = viaHA || !hasPassword;
  $("#settings-password-help").textContent = viaHA
    ? (hasPassword ? "Used only if you open Omnarr's own port directly instead of through Home Assistant."
                   : "Only needed if you open Omnarr's own port directly (add-on Network settings). Until you set one, direct sign-in stays closed.")
    : "The password for signing in to Omnarr.";
  const form = $("#settings-password-form");
  form.dataset.viaHa = String(viaHA);
  if (form.dataset.bound) return;
  form.dataset.bound = "1";
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = $("#settings-password-message");
    const next = $("#settings-password-new").value;
    if (next !== $("#settings-password-repeat").value) {
      message.textContent = "The two new passwords don't match.";
      message.className = "connection-message is-error";
      return;
    }
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    message.textContent = "Saving…";
    message.className = "connection-message is-busy";
    try {
      await api("api/auth/password", { method: "POST", body: JSON.stringify({ current: $("#settings-password-current").value, new: next }) });
      form.reset();
      $("#settings-password-current-field").hidden = form.dataset.viaHa === "true";
      message.textContent = "Password saved.";
      message.className = "connection-message is-success";
    } catch (error) {
      if (error.message === "login") return;
      message.textContent = error.message;
      message.className = "connection-message is-error";
    } finally {
      button.disabled = false;
    }
  });
}

async function boot() {
  try {
    if (inviteToken) return await showInvitation();
    const status = await api("api/auth/status", { authRequest: true, cache: "no-store" });
    if (!status.logged_in) return showAuth(status.setup_needed);
    currentUser = status.user;
    permissions = status.permissions || {};
    adultStatus.allowed = Boolean(status.adult_allowed);
    applyAccountChrome();
    $("#auth").hidden = true;
    $("#app").hidden = false;
    $("#logout").hidden = Boolean(status.ha_ingress);          // signed in through Home Assistant
    setupPasswordSection(status);
    applyAdultOption(Boolean(status.adult_enabled));
    settingsWelcome = isAdmin() && Boolean(status.connections_needed);
    if (settingsWelcome) params = new URLSearchParams({ view: "settings" });
    const wantsPrivate = new URLSearchParams(location.hash.slice(1)).get("private") === "1";
    await checkAdultStatus();
    writeHistory(true);
    syncControls();
    run();
    refreshStatus();
    if (!activityVisible()) refreshActivity(false);
    if (settingsWelcome) $("#settings-title").focus();
    else if (wantsPrivate && adultStatus.enabled) openPrivate();
  } catch {
    showAuth(false, "Omnarr could not be reached. Try again in a moment.");
  }
}

function showAuth(setup, error = "") {
  stopSettings();
  currentUser = null;
  permissions = {};
  resetUpload();
  adultEnabled = false;
  window.OmnarrPlayer?.closeAll();
  window.OmnarrReader?.closeAll();
  ++gateSequence;
  ++adultStatusSequence;
  if (privateMode) leavePrivate("", false);
  else clearView();
  adultStatus = { enabled: false, unlocked: false, until: 0 };
  updatePrivateChrome();
  $("#private-dialog").close();
  if (!inviteToken) writeHistory(true);
  setupMode = Boolean(setup);
  if ($("#detail")?.open) $("#detail").close();
  closeFilters();
  $("#app").hidden = true;
  $("#auth").hidden = false;
  const creating = setupMode || Boolean(inviteToken);
  $("#auth-confirm").hidden = !creating;
  $("#auth-pw2").required = creating;
  $("#auth-pw").autocomplete = creating ? "new-password" : "current-password";
  $("#auth-pw").minLength = creating ? 8 : 1;
  if (setupMode) $("#auth-username").value = "admin";
  $("#auth-msg").textContent = setupMode ? "First run: create your Omnarr admin account." : "Sign in to browse your library.";
  $("#auth-btn").textContent = creating ? "Create account" : "Sign in";
  $("#auth-btn").disabled = false;
  $("#auth-err").textContent = error;
  $("#auth-username").focus();
}

async function showInvitation() {
  showAuth(false);
  inviteReady = false;
  $("#auth-btn").disabled = true;
  $("#auth-msg").textContent = "Checking your invitation…";
  try {
    const invite = await api(`api/auth/invite/${encodeURIComponent(inviteToken)}`, { authRequest: true, cache: "no-store" });
    $("#auth-msg").textContent = `${invite.invited_by} invited you to Omnarr. Create your account to browse and play. Invitation expires ${new Date(invite.expires * 1000).toLocaleString()}.`;
    inviteReady = true;
    $("#auth-btn").disabled = false;
  } catch (error) {
    $("#auth-msg").textContent = "This invitation could not be opened.";
    $("#auth-err").textContent = error.message;
  }
}

$("#auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (inviteToken && !inviteReady) return;
  const password = $("#auth-pw").value;
  $("#auth-err").textContent = "";
  if ((setupMode || inviteToken) && password !== $("#auth-pw2").value) {
    $("#auth-err").textContent = "Those passwords don’t match.";
    return;
  }
  $("#auth-btn").disabled = true;
  try {
    const path = inviteToken ? `api/auth/invite/${encodeURIComponent(inviteToken)}` : setupMode ? "api/auth/setup" : "api/auth/login";
    await api(path, { method: "POST", authRequest: true, body: JSON.stringify({ username: $("#auth-username").value.trim(), password }) });
    if (inviteToken) {
      inviteToken = "";
      params = new URLSearchParams();
      history.replaceState(null, "", `${location.pathname}${location.search}`);
    }
    $("#auth-pw").value = "";
    $("#auth-pw2").value = "";
    await boot();
  } catch (error) {
    $("#auth-err").textContent = error.message;
  } finally {
    $("#auth-btn").disabled = false;
  }
});

function writeHistory(replace = false) {
  if (inviteToken) return;
  const hash = privateMode ? "private=1" : params.toString();
  const url = hash ? `#${hash}` : `${location.pathname}${location.search}`;
  if (location.hash === (hash ? `#${hash}` : "")) return;
  history[replace ? "replaceState" : "pushState"](null, "", url);
}

function setParam(key, value, options = {}) {
  if (key !== "view") params.delete("view");
  if (value === "" || value == null) params.delete(key);
  else params.set(key, value);
  offset = 0;
  writeHistory(Boolean(options.replace));
  syncControls();
  run();
}

function toggleList(key, value) {
  const values = (params.get(key) || "").split(",").filter(Boolean);
  const at = values.indexOf(value);
  if (at >= 0) values.splice(at, 1);
  else values.push(value);
  setParam(key, values.join(","));
}

function clearAll() {
  params = new URLSearchParams();
  offset = 0;
  writeHistory();
  syncControls();
  closeFilters();
  run();
}

function syncControls() {
  $("#q").value = params.get("q") || "";
  $("#sort").value = params.get("sort") || "";
  for (const key of TEXT_PARAMS) {
    const control = $(`#${key}`);
    if (control) control.value = params.get(key) || "";
  }
  for (const key of BOOL_PARAMS) $(`#${key}`).checked = params.get(key) === "1";
  const order = params.get("order") || "";
  $("#order").dataset.order = order;
  $("#order").title = order ? `Sorted ${order === "asc" ? "ascending" : "descending"}; reverse order` : "Reverse sort order";
}

function hasDiscoveryState() {
  return [...params.keys()].length > 0;
}

let queryTimer;
$("#q").addEventListener("input", (event) => {
  clearTimeout(queryTimer);
  queryTimer = setTimeout(() => setParam("q", event.target.value.trim(), { replace: Boolean(params.get("q")) }), 180);
});
$("#sort").addEventListener("change", (event) => setParam("sort", event.target.value));
$("#order").addEventListener("click", () => setParam("order", params.get("order") === "asc" ? "desc" : "asc"));
for (const key of TEXT_PARAMS) {
  const control = $(`#${key}`);
  if (!control) continue;
  let timer;
  control.addEventListener(["added_days", "rating_min"].includes(key) ? "change" : "input", (event) => {
    clearTimeout(timer);
    const epoch = viewEpoch;
    timer = setTimeout(() => { if (epoch === viewEpoch) setParam(key, event.target.value.trim()); }, 300);
  });
}
for (const key of BOOL_PARAMS) $(`#${key}`).addEventListener("change", (event) => setParam(key, event.target.checked ? "1" : ""));
$("#clear").addEventListener("click", clearAll);
$("#home").addEventListener("click", (event) => { event.preventDefault(); if (privateMode) leavePrivate(); else clearAll(); });
$("#more").addEventListener("click", () => { offset += PAGE; runResults(true); });
$("#logout").addEventListener("click", async () => { window.OmnarrPlayer?.closeAll(); window.OmnarrReader?.closeAll(); await api("api/auth/logout", { method: "POST" }); location.reload(); });
$("#reindex").addEventListener("click", async () => {
  $("#reindex").disabled = true;
  try {
    await api("api/reindex", { method: "POST" });
    $("#index-status").textContent = "Refreshing your library…";
    setTimeout(refreshStatus, 3000);
  } catch (error) {
    if (error.message !== "login") $("#index-status").textContent = error.message;
  } finally { $("#reindex").disabled = false; }
});

window.addEventListener("popstate", () => {
  const hash = new URLSearchParams(location.hash.slice(1));
  if (hash.has("invite") || inviteToken) {
    inviteToken = hash.get("invite") || "";
    boot();
    return;
  }
  ++gateSequence;
  $("#private-dialog").close();
  if (privateMode) leavePrivate("", false);
  else clearView();
  params = publicParams(location.hash.slice(1));
  offset = 0;
  writeHistory(true);
  syncControls();
  run();
  if (hash.get("private") === "1") openPrivate();
});
document.addEventListener("keydown", (event) => {
  if ($("#video-player")?.open || $("#audio-sheet")?.open || $("#upload-dialog").open) return;
  if (event.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {
    event.preventDefault();
    $("#q").focus();
  }
  if (event.key === "Escape" && document.body.classList.contains("filters-open")) closeFilters();
});

const mobileFilters = matchMedia("(max-width: 900px)");
function openFilters() {
  if (!mobileFilters.matches) return;
  document.body.classList.add("filters-open");
  $("#filter-scrim").hidden = false;
  $("#filters-toggle").setAttribute("aria-expanded", "true");
  $("#filters").setAttribute("aria-hidden", "false");
  $("#filters-close").focus();
}
function closeFilters() {
  document.body.classList.remove("filters-open");
  $("#filter-scrim").hidden = true;
  $("#filters-toggle").setAttribute("aria-expanded", "false");
  $("#filters").setAttribute("aria-hidden", mobileFilters.matches ? "true" : "false");
}
$("#filters-toggle").addEventListener("click", openFilters);
$("#filters-close").addEventListener("click", closeFilters);
$("#filter-scrim").addEventListener("click", closeFilters);
mobileFilters.addEventListener("change", closeFilters);
closeFilters();

function renderChips(element, key, counts, labels = {}) {
  const selected = (params.get(key) || "").split(",").filter(Boolean);
  const keys = Object.keys(counts || {});
  for (const value of selected) if (!keys.includes(value)) keys.unshift(value);
  element.innerHTML = keys.length ? keys.map((value) => {
    const active = selected.includes(value);
    return `<button class="chip${active ? " on" : ""}" type="button" data-v="${esc(value)}" aria-pressed="${active}"><span>${esc(labels[value] || value)}</span><span class="chip-count">${Number(counts?.[value] || 0).toLocaleString()}</span></button>`;
  }).join("") : `<span class="hint">No options</span>`;
  $$(".chip", element).forEach((button) => button.addEventListener("click", () => toggleList(key, button.dataset.v)));
}

function renderKinds(facets = kindFacets) {
  kindFacets = facets;
  const kinds = privateMode ? [["", "Private collection"]] : KINDS;
  const selected = (params.get("kind") || "").split(",").filter(Boolean);
  const total = Object.entries(facets.kind || {}).reduce((sum, [kind, value]) => sum + (!privateMode && kind === "scene" ? 0 : Number(value || 0)), 0);
  $("#kinds").innerHTML = kinds.map(([value, label]) => {
    const active = !params.has("view") && (value ? selected.length === 1 && selected[0] === value : selected.length === 0);
    const count = value ? facets.kind?.[value] || 0 : total;
    return `<button type="button" class="${active ? "on" : ""}" data-v="${value}" aria-pressed="${active}">${label}<span>${Number(count).toLocaleString()}</span></button>`;
  }).join("");
  $$("button", $("#kinds")).forEach((button) => button.addEventListener("click", () => setParam("kind", button.dataset.v)));
  if (!privateMode) {
    $("#kinds").insertAdjacentHTML("beforeend", `<button type="button" id="activity-nav" class="${params.get("view") === "activity" ? "on" : ""}" aria-pressed="${params.get("view") === "activity"}">Activity<span id="activity-badge" class="activity-badge" hidden></span></button>`);
    $("#activity-nav").addEventListener("click", () => { setParam("view", "activity"); $("#activity-title").focus(); });
    updateActivityBadge();
  }
  const settings = !privateMode && params.get("view") === "settings";
  $("#kinds").insertAdjacentHTML("beforeend", `<button type="button" id="settings-nav" class="settings-nav${settings ? " on" : ""}" aria-label="Settings" title="Settings" aria-controls="settings-view" aria-pressed="${settings}"><svg aria-hidden="true" viewBox="0 0 24 24"><path d="m10 3-.5 2.2-2 .9-2-.8-2 3.4 1.7 1.5v2.3L3.5 14l2 3.4 2-.7 2 .9L10 20h4l.5-2.4 2-.9 2 .7 2-3.4-1.7-1.5v-2.3l1.7-1.5-2-3.4-2 .8-2-.9L14 3Z"/><circle cx="12" cy="11.5" r="3"/></svg><span class="sr-only">Settings</span></button>`);
  $("#settings-nav").addEventListener("click", openSettings);
}

function renderHas(facets = {}) {
  const selected = (params.get("has") || "").split(",").filter(Boolean);
  $("#has").innerHTML = Object.entries(privateMode ? { scene: "Watch" } : HAS).map(([value, label]) => {
    const active = selected.includes(value);
    return `<button class="chip format-chip f-${value}${active ? " on" : ""}" type="button" data-v="${value}" aria-pressed="${active}"><span>${label}</span><span class="chip-count">${Number(facets.formats?.[value] || 0).toLocaleString()}</span></button>`;
  }).join("");
  $$(".chip", $("#has")).forEach((button) => button.addEventListener("click", () => toggleList("has", button.dataset.v)));
}

function renderFacets(facets = {}) {
  // Facets come from a scoped search. Fail closed if a mixed response ever arrives.
  if (!privateMode && (Number(facets.kind?.scene) > 0 || Number(facets.formats?.scene) > 0 || Number(facets.sources?.stash) > 0)) facets = {};
  if (!privateMode) facets = { ...facets, sources: Object.fromEntries(Object.entries(facets.sources || {}).filter(([source]) => source !== "stash")) };
  renderKinds(facets);
  renderHas(facets);
  renderChips($("#f-status"), "status", facets.status, STATUS);
  $("#availability-filter").hidden = privateMode;
  renderChips($("#f-availability"), "availability", { in_library: 0, partial: 0, coming: 0, ...facets.availability }, AVAILABILITY);
  renderChips($("#f-genre"), "genre", facets.genres);
  renderChips($("#f-library"), "library", facets.libraries);
  renderChips($("#f-source"), "source", facets.sources, SOURCE);

  const selectedMin = params.get("year_min");
  const selectedMax = params.get("year_max");
  $("#f-decade").innerHTML = Object.entries(facets.decade || {}).map(([year, count]) => {
    const active = selectedMin === year && selectedMax === String(Number(year) + 9);
    return `<button class="chip${active ? " on" : ""}" type="button" data-d="${esc(year)}" aria-pressed="${active}">${esc(year)}s<span class="chip-count">${Number(count).toLocaleString()}</span></button>`;
  }).join("");
  $$("[data-d]", $("#f-decade")).forEach((button) => button.addEventListener("click", () => {
    const year = button.dataset.d;
    const alreadyOn = params.get("year_min") === year && params.get("year_max") === String(Number(year) + 9);
    if (alreadyOn) {
      params.delete("year_min"); params.delete("year_max");
    } else {
      params.set("year_min", year); params.set("year_max", String(Number(year) + 9));
    }
    offset = 0; writeHistory(); syncControls(); run();
  }));
  renderActiveFilters();
}

function filterLabel(key, value) {
  const labels = {
    q: `Search: “${value}”`, kind: KINDS.find(([id]) => id === value)?.[1] || value,
    status: STATUS[value] || value, availability: AVAILABILITY[value] || value, has: HAS[value] || value, genre: value, library: value,
    source: SOURCE[value] || value, author: `${privateMode ? "Performer" : "Author / company"}: ${value}`, narrator: `Narrator: ${value}`,
    series: `Series: ${value}`, universe: `Universe: ${value}`, year_min: `From ${value}`, year_max: `To ${value}`,
    dur_min: `${value}+ hours`, dur_max: `Up to ${value} hours`, rating_min: `${value}+ rating`,
    added_days: `Added in ${value} days`, in_series: "In a series", linked: "Has adaptation", hidden: "Including not mine",
  };
  return labels[key] || `${key}: ${value}`;
}

function renderActiveFilters() {
  const active = [];
  for (const [key, raw] of params.entries()) {
    if (["sort", "order", "view"].includes(key)) continue;
    const values = LIST_PARAMS.includes(key) ? raw.split(",").filter(Boolean) : [raw];
    for (const value of values) active.push({ key, value, label: filterLabel(key, value) });
  }
  $("#active-filters").innerHTML = active.map(({ key, value, label }) => `<button class="active-pill" type="button" data-k="${esc(key)}" data-v="${esc(value)}" aria-label="Remove ${esc(label)}"><span>${esc(label)}</span><svg aria-hidden="true" viewBox="0 0 16 16"><path d="m4 4 8 8m0-8-8 8"/></svg></button>`).join("");
  $$(".active-pill", $("#active-filters")).forEach((button) => button.addEventListener("click", () => {
    if (LIST_PARAMS.includes(button.dataset.k)) toggleList(button.dataset.k, button.dataset.v);
    else setParam(button.dataset.k, "");
  }));
  $("#filter-total").hidden = active.length === 0;
  $("#filter-total").textContent = active.length;
}

function workSubtitle(work) {
  if (["game", "scene"].includes(work.kind)) return [work.authors?.join(", "), work.libraries?.join(", ") || work.library, work.year].filter(Boolean).join(" · ");
  if (["book", "comic"].includes(work.kind)) {
    return [work.authors?.[0], work.series ? `${work.series}${work.series_index != null ? ` #${Number(work.series_index)}` : ""}` : ""].filter(Boolean).join(" · ");
  }
  return [work.year, work.genres?.slice(0, 2).join(", ")].filter(Boolean).join(" · ");
}

function card(work, options = {}) {
  if (!allowedWork(work)) return "";
  const progress = work.status === "in_progress" && work.progress != null ? Math.round(work.progress * 100) : 0;
  const author = work.authors?.[0] || (work.kind === "show" ? "Television" : work.kind || "Omnarr");
  const formats = (work.formats || []).slice(0, 3);
  const moreFormats = Math.max(0, (work.formats || []).length - formats.length);
  return `<button class="card${work.kind === "game" ? " game-card" : ""}${options.mini ? " mini-card" : ""}${options.current ? " current-work" : ""}" type="button" data-id="${esc(work.id)}"${options.current ? ' aria-current="true"' : ""} aria-label="Open ${esc(work.title)}">
    <span class="cover-shell">
      <span class="cover-placeholder"><span class="placeholder-title">${esc(work.title)}</span><span class="placeholder-author">${esc(author)}</span></span>
      ${work.cover ? `<img class="cover-image" loading="lazy" src="api/cover/${encodeURIComponent(work.cover)}" alt="">` : ""}
      <span class="cover-shade" aria-hidden="true"></span>
      <span class="format-stack">${formats.map((format) => `<span class="format-badge f-${esc(format)}">${esc(BADGE[format] || format)}</span>`).join("")}${moreFormats ? `<span class="format-badge more-badge">+${moreFormats}</span>` : ""}</span>
      ${work.status === "finished" ? '<span class="finished-mark" aria-label="Finished"><svg aria-hidden="true" viewBox="0 0 20 20"><path d="m5 10 3 3 7-7"/></svg></span>' : ""}
      ${work.hidden ? '<span class="hidden-mark">Not mine</span>' : ""}
      ${progress ? `<span class="progress-track" aria-label="${progress}% complete"><span style="width:${progress}%"></span></span>` : ""}
    </span>
    <span class="card-title">${esc(work.title)}</span>
    <span class="card-subtitle">${esc(workSubtitle(work))}</span>
    ${availabilityChip(work)}
  </button>`;
}

function bindCards(root) {
  $$(".card[data-id]", root).forEach((element) => element.addEventListener("click", () => openWork(element.dataset.id)));
  $$(".cover-image", root).forEach((image) => image.addEventListener("error", () => image.remove(), { once: true }));
}

function skeletons(count = 10) {
  return Array.from({ length: count }, () => '<div class="card skeleton-card" aria-hidden="true"><span class="cover-shell skeleton"></span><span class="skeleton skeleton-line"></span><span class="skeleton skeleton-line short"></span></div>').join("");
}

function applyHomePreset(index) {
  params = new URLSearchParams(HOME_ROWS[index].params);
  offset = 0;
  writeHistory();
  syncControls();
  run();
}

async function runHome() {
  const sequence = ++runSequence;
  clearRequestSearch();
  $("#home-view").hidden = false;
  $("#results-view").hidden = true;
  $("#home-rows").innerHTML = HOME_ROWS.filter((row) => !row.optional).map((row) => `<section class="media-row"><div class="row-heading"><div><h2>${esc(row.title)}</h2><p>${esc(row.note)}</p></div></div><div class="rail">${skeletons(7)}</div></section>`).join("");
  try {
    const results = await Promise.all(HOME_ROWS.map((row) => {
      const query = scopedQuery(row.params);
      query.set("limit", "18");
      query.set("offset", "0");
      return api(`api/search?${query}`);
    }));
    if (sequence !== runSequence) return;
    renderFacets(results[1]?.facets || results[0]?.facets || {});
    $("#home-rows").innerHTML = results.map((result, index) => {
      const row = HOME_ROWS[index];
      const items = (result.items || []).filter(allowedWork);
      if (row.optional && !items.length) return "";
      const contents = items.length ? items.map((work) => card(work)).join("") : '<div class="row-empty">Nothing here yet.</div>';
      return `<section class="media-row"><div class="row-heading"><div><h2>${esc(row.title)}</h2><p>${esc(row.note)}</p></div>${result.total ? `<button class="row-link" type="button" data-home="${index}">View all <span aria-hidden="true">→</span></button>` : ""}</div><div class="rail">${contents}</div></section>`;
    }).join("");
    bindCards($("#home-rows"));
    $$("[data-home]", $("#home-rows")).forEach((button) => button.addEventListener("click", () => applyHomePreset(Number(button.dataset.home))));
  } catch (error) {
    if (error.message !== "login" && sequence === runSequence) $("#home-rows").innerHTML = `<div class="empty-state"><h2>Couldn’t load the library</h2><p>${esc(error.message)}</p><button class="secondary-button" type="button" id="retry-home">Try again</button></div>`;
    const retry = $("#retry-home");
    if (retry) retry.addEventListener("click", runHome);
  }
}

function resultsHeading() {
  const query = params.get("q");
  if (query) return `Results for “${query}”`;
  if (privateMode) return "Private collection";
  const selectedKinds = (params.get("kind") || "").split(",").filter(Boolean);
  if (selectedKinds.length === 1) return KINDS.find(([id]) => id === selectedKinds[0])?.[1] || "Browse everything";
  return "Browse everything";
}

async function runResults(append = false) {
  if (privateMode && !privateUnlocked()) { leavePrivate("Private collection locked."); return; }
  const sequence = ++runSequence;
  const wasPrivate = privateMode;
  $("#home-view").hidden = true;
  $("#results-view").hidden = false;
  $("#results-title").textContent = resultsHeading();
  if (!append) {
    $("#grid").innerHTML = skeletons(12);
    $("#more").hidden = true;
    renderRequestSearch();
  } else {
    $("#more").disabled = true;
  }
  const query = scopedQuery(params);
  query.set("limit", String(PAGE));
  query.set("offset", String(offset));
  try {
    const result = await api(`api/search?${query}`, { privateRequest: wasPrivate, cache: "no-store" });
    if (sequence !== runSequence) return;
    if (wasPrivate) {
      if (!privateUnlocked()) { leavePrivate("Private collection locked."); return; }
      // A locked server may return an empty search instead of a 401.
      if (!result.items?.length) {
        await checkAdultStatus();
        if (sequence !== runSequence || !privateUnlocked()) return;
      }
    }
    renderFacets(result.facets || {});
    const items = (result.items || []).filter(allowedWork);
    const contaminated = items.length !== (result.items || []).length || (!wasPrivate && (Number(result.facets?.kind?.scene) > 0 || Number(result.facets?.formats?.scene) > 0 || Number(result.facets?.sources?.stash) > 0));
    if (contaminated) renderFacets();
    $("#count").textContent = contaminated ? "" : `${Number(result.total).toLocaleString()} ${result.total === 1 ? "work" : "works"}`;
    const html = items.map((work) => card(work)).join("");
    if (append) $("#grid").insertAdjacentHTML("beforeend", html);
    else $("#grid").innerHTML = html || '<div class="empty-state"><h2>No matches found</h2><p>Try a broader search or remove one of the filters above.</p><button class="secondary-button" type="button" id="empty-clear">Clear filters</button></div>';
    bindCards($("#grid"));
    const emptyClear = $("#empty-clear");
    if (emptyClear) emptyClear.addEventListener("click", clearAll);
    if (!append && result.total === 0 && params.get("kind") === "game" && !wasPrivate) {
      const gameRequest = $("#search-game-request");
      if (gameRequest && emptyClear) {
        gameRequest.hidden = false;
        const gameToggle = $("[data-game-toggle]", $("#request-search"));
        gameToggle.hidden = true;
        gameToggle.setAttribute("aria-expanded", "true");
        $(".empty-state", $("#grid")).append(gameRequest);
      }
    }
    $("#more").hidden = offset + result.items.length >= result.total;
  } catch (error) {
    if (error.message !== "login" && sequence === runSequence) $("#grid").innerHTML = `<div class="empty-state"><h2>Couldn’t load results</h2><p>${esc(error.message)}</p></div>`;
  } finally {
    $("#more").disabled = false;
  }
}

function run() {
  if ($("#app").hidden) return;
  if (privateMode && !privateUnlocked()) { leavePrivate("Private collection locked."); return; }
  stopActivity();
  const activity = !privateMode && params.get("view") === "activity";
  const settings = !privateMode && params.get("view") === "settings";
  $("#activity-view").hidden = !activity;
  $("#settings-view").hidden = !settings;
  document.body.classList.toggle("activity-page", activity);
  document.body.classList.toggle("settings-page", settings);
  $("#filters-toggle").hidden = activity || settings;
  renderKinds();
  if (settings) {
    ++runSequence;
    clearRequestSearch();
    clearTimeout(queryTimer);
    closeFilters();
    $("#home-view").hidden = $("#results-view").hidden = true;
    $("#settings-welcome").hidden = !settingsWelcome;
    if (!settingsController) {
      settingsController = new AbortController();
      loadMyAccount();
      if (isAdmin()) { loadConnections(); loadAccounts(); loadInvites(); }
    }
    return;
  }
  stopSettings();
  if (activity) {
    ++runSequence;
    clearRequestSearch();
    closeFilters();
    $("#home-view").hidden = $("#results-view").hidden = true;
    startActivity();
    return;
  }
  renderActiveFilters();
  if (privateMode || hasDiscoveryState()) runResults();
  else runHome();
}

let uploadController;
let uploadConfig;
let uploadFiles = [];
let uploadBusy = false;
let uploadOptionsReady = false;

function resetUpload() {
  uploadController?.abort();
  uploadController = null;
  uploadConfig = null;
  uploadFiles = [];
  uploadBusy = false;
  $("#upload-dialog").close();
  $("#upload-form").reset();
  $("#upload-form").hidden = true;
  $("#upload-form").removeAttribute("aria-busy");
  $("#upload-files").disabled = false;
  $("#upload-submit").disabled = true;
  $("#upload-selection").replaceChildren();
  $("#upload-results").replaceChildren();
  $("#upload-drop").classList.remove("is-dragging");
  connectionMessage($("#upload-status"), "");
}

function uploadExtensions() {
  if (!uploadConfig) return [];
  return [...new Set([
    ...(uploadConfig.books_enabled ? uploadConfig.types?.book || [] : []),
    ...(uploadConfig.audio_enabled ? uploadConfig.types?.audio || [] : []),
  ].map((type) => String(type).toLowerCase()))];
}

function selectUploadFiles(files) {
  if (uploadBusy || !uploadConfig) return;
  uploadFiles = [];
  const types = uploadExtensions();
  const limit = Number(uploadConfig.max_mb) * 1024 * 1024;
  $("#upload-results").replaceChildren();
  $("#upload-selection").innerHTML = [...files].map((file) => {
    const extension = file.name.includes(".") ? file.name.split(".").pop().toLowerCase() : "";
    const error = !types.includes(extension) ? "This file type isn't enabled." : file.size > limit ? `Exceeds the ${uploadConfig.max_mb} MB limit.` : "";
    if (!error) uploadFiles.push(file);
    return `<li${error ? ' class="is-error"' : ""}><strong>${esc(file.name)}</strong><span>${esc(error || `${(file.size / 1024 / 1024).toFixed(1)} MB — ready`)}</span></li>`;
  }).join("");
  $("#upload-submit").disabled = !uploadFiles.length;
  connectionMessage($("#upload-status"), uploadFiles.length ? `${uploadFiles.length} file${uploadFiles.length === 1 ? "" : "s"} ready to upload.` : "Choose supported files within the size limit.");
}

async function openUpload() {
  if (permissions.can_upload !== true) return;
  const dialog = $("#upload-dialog");
  if (!dialog.open) dialog.showModal();
  if (uploadBusy) return;
  uploadController?.abort();
  const controller = uploadController = new AbortController();
  uploadConfig = null;
  uploadFiles = [];
  $("#upload-form").reset();
  $("#upload-form").hidden = true;
  $("#upload-submit").disabled = true;
  $("#upload-selection").replaceChildren();
  $("#upload-retry").hidden = true;
  connectionMessage($("#upload-status"), "Checking upload folders…", "busy");
  try {
    const config = await api("api/upload", { cache: "no-store", signal: AbortSignal.any([controller.signal, AbortSignal.timeout(30000)]) });
    if (controller !== uploadController) return;
    uploadConfig = config;
    if (!config.books_enabled && !config.audio_enabled) {
      connectionMessage($("#upload-status"), "Uploads aren't set up yet — ask an admin.");
      return;
    }
    const types = uploadExtensions();
    $("#upload-files").accept = types.map((type) => `.${type}`).join(",");
    $("#upload-limit").textContent = `Maximum size: ${config.max_mb} MB per file.`;
    $("#upload-types").textContent = `Accepted files: ${types.map((type) => type.toUpperCase()).join(", ")}.`;
    $("#upload-form").hidden = false;
    connectionMessage($("#upload-status"), "");
  } catch (error) {
    if (controller !== uploadController || error.message === "login") return;
    connectionMessage($("#upload-status"), error.message, "error");
    $("#upload-retry").hidden = false;
  }
}

$("#upload-open").addEventListener("click", openUpload);
$("#upload-retry").addEventListener("click", openUpload);
$("#upload-close").addEventListener("click", () => $("#upload-dialog").close());
$("#upload-files").addEventListener("change", (event) => selectUploadFiles(event.target.files));
for (const name of ["dragenter", "dragover", "dragleave", "drop"]) {
  $("#upload-drop").addEventListener(name, (event) => {
    event.preventDefault();
    $("#upload-drop").classList.toggle("is-dragging", !uploadBusy && ["dragenter", "dragover"].includes(name));
    if (name === "drop" && !uploadBusy) {
      $("#upload-files").value = "";
      selectUploadFiles(event.dataTransfer.files);
    }
  });
}
$("#upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (permissions.can_upload !== true || uploadBusy || !uploadFiles.length) return;
  const controller = uploadController;
  const body = new FormData();
  uploadFiles.forEach((file) => body.append("files", file));
  uploadBusy = true;
  $("#upload-files").disabled = true;
  $("#upload-submit").disabled = true;
  $("#upload-form").setAttribute("aria-busy", "true");
  $("#upload-results").replaceChildren();
  connectionMessage($("#upload-status"), "Uploading… You can close this dialog; keep Omnarr open until it finishes.", "busy");
  try {
    const result = await api("api/upload", { method: "POST", body, signal: controller.signal });
    if (controller !== uploadController) return;
    $("#upload-results").innerHTML = (result.results || []).map((item) => `<li class="${item.ok ? "is-success" : "is-error"}"><strong>${esc(item.file)}</strong><span>${item.ok ? "Uploaded" : "Failed"}: ${esc(item.message || "")}</span></li>`).join("");
    const success = result.ok && (result.results || []).every((item) => item.ok);
    connectionMessage($("#upload-status"), success ? "Upload complete." : "Some files could not be uploaded. Check the results below.", success ? "success" : "error");
    uploadFiles = [];
    $("#upload-form").reset();
    $("#upload-selection").replaceChildren();
  } catch (error) {
    if (controller !== uploadController || error.message === "login") return;
    connectionMessage($("#upload-status"), `${error.message}. Check the library before retrying; some files may have arrived.`, "error");
  } finally {
    if (controller === uploadController) {
      uploadBusy = false;
      $("#upload-files").disabled = false;
      $("#upload-submit").disabled = !uploadFiles.length;
      $("#upload-form").removeAttribute("aria-busy");
    }
  }
});

function fillUploadOptions(options) {
  const form = $("#upload-options-form");
  form.elements.upload_dir.value = options.upload_dir || "";
  form.elements.upload_audio_dir.value = options.upload_audio_dir || "";
  form.elements.upload_max_mb.value = options.upload_max_mb ?? 2048;
  form.elements.public_url.value = options.public_url || "";
}

$("#upload-options-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!isAdmin() || settingsBusy || !settingsController || !uploadOptionsReady) return;
  const form = event.currentTarget;
  const controller = settingsController;
  const values = { upload_dir: form.elements.upload_dir.value.trim(), upload_audio_dir: form.elements.upload_audio_dir.value.trim(), upload_max_mb: Number(form.elements.upload_max_mb.value), public_url: form.elements.public_url.value.trim() };
  settingsPending(true);
  connectionMessage($("#upload-options-message"), "Saving upload folders…", "busy");
  try {
    const result = await api("api/setup/options", { method: "POST", body: JSON.stringify(values), signal: AbortSignal.any([controller.signal, AbortSignal.timeout(30000)]) });
    if (controller !== settingsController) return;
    if (!result.ok) throw new Error("Upload folders were not saved.");
    fillUploadOptions(result.options || values);
    connectionMessage($("#upload-options-message"), "Upload settings saved.", "success");
  } catch (error) {
    if (controller !== settingsController || error.message === "login") return;
    connectionMessage($("#upload-options-message"), error.message, "error");
  } finally {
    if (controller === settingsController) settingsPending(false);
  }
});

// Connection drafts live only in these forms, never in navigation or storage.
function applyAccountChrome() {
  $("#signed-in-user").textContent = currentUser?.username || "";
  $("#signed-in-user").title = currentUser?.username || "";
  for (const id of ["settings-private", "settings-uploads", "settings-admin-connections", "settings-users", "reindex"]) $("#" + id).hidden = !isAdmin();
  $("#upload-open").hidden = permissions.can_upload !== true;
  if (permissions.can_upload !== true) resetUpload();
}

const ACCOUNT_PERMISSIONS = [
  ["can_request", "Can request downloads", "Searches and downloads on your server: Seerr, books, games, Sonarr/Radarr actions"],
  ["can_ask", "Can ask for things (needs approval)", "Their requests wait in Activity → Requests until an admin approves them"],
  ["can_download", "Can save files to their device", "Save original library files to their device"],
  ["can_upload", "Can upload files", "Add ebooks, comics, and audiobooks to configured folders"],
  ["adult_allowed", "Private section", "They set their own PIN"],
];

function permissionEditor(prefix, account = {}, presets = false) {
  return `${presets ? '<div class="connection-actions permission-presets"><button type="button" class="secondary-button" data-preset="family">Family</button><button type="button" class="secondary-button" data-preset="friend">Friend (asks first)</button><button type="button" class="secondary-button" data-preset="guest">Guest (view &amp; play only)</button></div>' : ""}
    <label class="field">Role<select name="role"><option value="member"${account.role !== "admin" ? " selected" : ""}>Member</option><option value="admin"${account.role === "admin" ? " selected" : ""}>Admin</option></select></label>
    <div class="permission-switches">${ACCOUNT_PERMISSIONS.map(([key, label, help]) => `<div><label class="check" for="${prefix}-${key}"><input id="${prefix}-${key}" name="${key}" type="checkbox" role="switch" aria-describedby="${prefix}-${key}-help"${account[key] ? " checked" : ""}><span>${label}</span></label><p id="${prefix}-${key}-help" class="hint">${help}</p></div>`).join("")}</div>
    <p class="hint admin-permissions-help"${account.role === "admin" ? "" : " hidden"}>Admins can request, save, and upload. Private access keeps its per-person setting.</p>`;
}

function syncPermissionEditor(root) {
  const admin = $('[name="role"]', root).value === "admin";
  ACCOUNT_PERMISSIONS.forEach(([key]) => {
    const input = $(`[name="${key}"]`, root);
    input.disabled = admin;
    if (admin && key !== "adult_allowed") input.checked = true;
  });
  const ask = $('[name="can_ask"]', root);
  const direct = $('[name="can_request"]', root).checked;
  ask.disabled = admin || direct;
  $(`#${ask.id}-help`, root).textContent = ACCOUNT_PERMISSIONS.find(([key]) => key === "can_ask")[2]
    + (direct ? ". Turn off Can request downloads to require approval." : "");
  $(".admin-permissions-help", root).hidden = !admin;
}

function bindPermissionEditor(root) {
  $('[name="role"]', root).addEventListener("change", () => syncPermissionEditor(root));
  $('[name="can_request"]', root).addEventListener("change", () => syncPermissionEditor(root));
  $$("[data-preset]", root).forEach((button) => button.addEventListener("click", () => {
    $('[name="role"]', root).value = "member";
    ACCOUNT_PERMISSIONS.forEach(([key]) => { $(`[name="${key}"]`, root).checked = (key === "can_request" && button.dataset.preset === "family") || (key === "can_ask" && button.dataset.preset === "friend"); });
    syncPermissionEditor(root);
  }));
  syncPermissionEditor(root);
}

function accountPermissions(root) {
  return { role: $('[name="role"]', root).value, ...Object.fromEntries(ACCOUNT_PERMISSIONS.map(([key]) => [key, $(`[name="${key}"]`, root).checked])) };
}

// Each settings visit owns its requests; leaving it discards unsaved credentials.
async function accountOperation(root, message, task) {
  const controller = settingsController;
  if (!controller || root.dataset.busy) return;
  root.dataset.busy = "1";
  const controls = $$("input, select, button", root).map((control) => [control, control.disabled]);
  controls.forEach(([control]) => { control.disabled = true; });
  connectionMessage(message, "Saving…", "busy");
  const current = () => settingsController === controller && root.isConnected;
  try { await task(controller.signal, current); }
  catch (error) { if (current() && error.message !== "login") connectionMessage(message, error.message, "error"); }
  finally {
    delete root.dataset.busy;
    controls.forEach(([control, disabled]) => { control.disabled = disabled; });
  }
}

async function loadMyAccount() {
  const controller = settingsController;
  if (!controller) return;
  const form = $("#my-account-form");
  $$("input, button[type=submit]", form).forEach((control) => { control.disabled = true; });
  $("#account-retry").hidden = true;
  connectionMessage($("#account-message"), "Loading your account…", "busy");
  try {
    const account = await api("api/me", { signal: controller.signal, cache: "no-store" });
    if (controller !== settingsController) return;
    $('[name="jellyfin_user"]', form).value = account.jellyfin_user || "";
    $('[name="abs_api_key"]', form).value = account.abs_api_key || "";
    form.elements.email.value = account.email || "";
    form.elements.notify_email.checked = account.notify_email === true;
    $("#account-identity").hidden = !isAdmin() || !account.uses_server_identity;
    $$("input, button[type=submit]", form).forEach((control) => { control.disabled = false; });
    connectionMessage($("#account-message"), "");
  } catch (error) {
    if (controller !== settingsController) return;
    connectionMessage($("#account-message"), error.message, "error");
    $("#account-retry").hidden = false;
  }
}

$("#account-retry").addEventListener("click", loadMyAccount);
$("#my-account-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = { jellyfin_user: $('[name="jellyfin_user"]', form).value.trim(), abs_api_key: $('[name="abs_api_key"]', form).value.trim(), email: form.elements.email.value.trim(), notify_email: form.elements.notify_email.checked };
  accountOperation(form, $("#account-message"), async (signal, current) => {
    const result = await api("api/me", { method: "POST", body: JSON.stringify(payload), signal });
    if (!current()) return;
    $('[name="abs_api_key"]', form).value = result.account.abs_api_key || "";
    $('[name="jellyfin_user"]', form).value = result.account.jellyfin_user || "";
    await loadMyAccount();
    if (current()) connectionMessage($("#account-message"), result.message || "Account saved.", "success");
  });
});

async function loadAccounts() {
  const controller = settingsController;
  if (!controller || !isAdmin()) return;
  connectionMessage($("#users-message"), "Loading users…", "busy");
  try {
    const data = await api("api/accounts", { signal: controller.signal, cache: "no-store" });
    if (controller !== settingsController) return;
    const list = $("#accounts-list");
    list.innerHTML = (data.accounts || []).map((account) => `<article class="account-card"><h3>${esc(account.username)}${account.id === currentUser.id ? " (you)" : ""}</h3><form class="account-form" data-account-id="${esc(account.id)}">
      <label class="field">Username<input name="username" value="${esc(account.username)}" autocomplete="off" required></label>
      ${permissionEditor(`account-${account.id}`, account)}
      <div class="connection-actions"><button class="primary-button" type="submit">Save changes</button><button class="secondary-button" type="button" data-account-action="password">Reset password</button><button class="secondary-button" type="button" data-account-action="pin"${account.pin_set ? "" : " disabled"}>Clear PIN</button><button class="text-button" type="button" data-account-action="delete"${account.id === currentUser.id ? ' disabled title="You cannot delete your own account"' : ""}>Delete</button></div>
      <p class="connection-message" role="status" aria-live="polite"></p></form></article>`).join("");
    $$("form", list).forEach((form) => {
      bindPermissionEditor(form);
      const account = data.accounts.find((item) => String(item.id) === form.dataset.accountId);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        saveAccount(form, account, { username: $('[name="username"]', form).value.trim(), ...accountPermissions(form) });
      });
      $$("[data-account-action]", form).forEach((button) => button.addEventListener("click", () => {
        const action = button.dataset.accountAction;
        if (action === "delete") {
          if (confirm(`Delete ${account.username}'s account? They will no longer be able to sign in.`)) saveAccount(form, account, null);
        } else if (action === "pin") {
          saveAccount(form, account, { clear_pin: true });
        } else {
          const password = prompt(`New password for ${account.username} (8+ characters):`);
          if (password === null) return;
          if (password.length < 8) { connectionMessage($(".connection-message", form), "Use a password with at least 8 characters.", "error"); return; }
          saveAccount(form, account, { password });
        }
      }));
    });
    connectionMessage($("#users-message"), "");
  } catch (error) { if (controller === settingsController) connectionMessage($("#users-message"), error.message, "error"); }
}

function saveAccount(form, account, payload) {
  accountOperation(form, $(".connection-message", form), async (signal, current) => {
    const result = await api(`api/accounts/${encodeURIComponent(account.id)}`, { method: payload === null ? "DELETE" : "PATCH", ...(payload === null ? {} : { body: JSON.stringify(payload) }), signal });
    if (!current()) return;
    if (account.id === currentUser.id) {
      currentUser = result.account;
      permissions = result.account;
      adultStatus.allowed = Boolean(currentUser.adult_allowed);
      applyAccountChrome();
      await checkAdultStatus();
      if (!isAdmin()) { stopSettings(); run(); return; }
    }
    await loadAccounts();
    if (settingsController?.signal === signal) {
      connectionMessage($("#users-message"), payload === null ? "User deleted." : "User saved.", "success");
      $("#users-refresh").focus();
    }
  });
}

async function loadInvites() {
  const controller = settingsController;
  if (!controller || !isAdmin()) return;
  connectionMessage($("#invites-message"), "Loading invitations…", "busy");
  try {
    const data = await api("api/accounts/invites", { signal: controller.signal, cache: "no-store" });
    if (controller !== settingsController) return;
    $("#invite-email-help").hidden = Boolean(data.email_enabled);
    const list = $("#invites-list");
    list.innerHTML = (data.invites || []).map((invite) => `<li class="invite-row"><div><strong>${esc(invite.note || invite.email || "Invitation")}</strong>${invite.note && invite.email ? `<p class="hint">${esc(invite.email)}</p>` : ""}<p class="hint">${esc(invite.status)} · Expires ${esc(new Date(invite.expires * 1000).toLocaleString())}${invite.used_by != null ? ` · Used by ${esc(invite.used_by)}` : ""}</p></div>${invite.status === "pending" ? `<button class="text-button" type="button" data-revoke="${esc(invite.id)}" aria-label="Revoke invitation ${esc(invite.note || invite.email || invite.id)}">Revoke</button>` : ""}</li>`).join("") || '<li class="hint">No invitations yet.</li>';
    $$("[data-revoke]", list).forEach((button) => button.addEventListener("click", () => {
      accountOperation(button.closest("li"), $("#invites-message"), async (signal, current) => {
        await api(`api/accounts/invites/${encodeURIComponent(button.dataset.revoke)}`, { method: "DELETE", signal });
        if (current()) { await loadInvites(); $("#invites-refresh").focus(); }
      });
    }));
    connectionMessage($("#invites-message"), "");
  } catch (error) { if (controller === settingsController) connectionMessage($("#invites-message"), error.message, "error"); }
}

$$("[data-permission-editor]").forEach((root) => {
  root.innerHTML = permissionEditor(root.dataset.permissionEditor, { role: "member", can_request: true }, true);
  bindPermissionEditor(root);
});
$("#users-refresh").addEventListener("click", loadAccounts);
$("#invites-refresh").addEventListener("click", loadInvites);
$("#create-account-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = { username: $('[name="username"]', form).value.trim(), password: $('[name="password"]', form).value, ...accountPermissions(form) };
  accountOperation(form, $(".connection-message", form), async (signal, current) => {
    await api("api/accounts", { method: "POST", body: JSON.stringify(payload), signal });
    if (!current()) return;
    form.reset();
    connectionMessage($(".connection-message", form), "User created.", "success");
    await loadAccounts();
  }).then(() => syncPermissionEditor(form));
});
$("#create-invite-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = { ...accountPermissions(form), days: Number($('[name="days"]', form).value), email: $('[name="email"]', form).value.trim() || null, note: $('[name="note"]', form).value.trim() || null, link_base: location.href.split("#")[0] };
  accountOperation(form, $(".connection-message", form), async (signal, current) => {
    const result = await api("api/accounts/invites", { method: "POST", body: JSON.stringify(payload), signal });
    if (!current()) return;
    $("#invite-link").value = result.url;
    $("#invite-result").hidden = false;
    $("#invite-copy-message").textContent = "";
    connectionMessage($(".connection-message", form), result.message || (result.emailed ? "Invitation emailed." : "Invitation created. Copy the link to share it."), "success");
    $("#invite-link").focus();
    await loadInvites();
  });
});
$("#invite-copy").addEventListener("click", async () => {
  const input = $("#invite-link");
  try {
    await navigator.clipboard.writeText(input.value);
    $("#invite-copy-message").textContent = "Link copied.";
  } catch {
    input.focus();
    input.select();
    input.setSelectionRange(0, input.value.length);
    $("#invite-copy-message").textContent = "Link selected. Use Copy from your device's menu or press Ctrl+C (⌘C on Mac).";
  }
});

let settingsController;
let settingsApps = [];
let settingsBusy = false;
let settingsWelcome = false;

function applyAdultOption(enabled) {
  const wasEnabled = adultEnabled;
  adultEnabled = enabled;
  ++adultStatusSequence;
  adultStatus.enabled = enabled && Boolean(adultStatus.allowed);
  if (!enabled) {
    adultStatus.unlocked = false;
    ++gateSequence;
    $("#private-dialog").close();
    if (privateMode) leavePrivate("", false);
    privateNotice();
    if (wasEnabled) { window.OmnarrPlayer?.closeAll(); window.OmnarrReader?.closeAll(); }
  }
  updatePrivateChrome();
  $("#settings-adult").checked = enabled;
  $$(".connection-group[data-private]").forEach((group) => {
    group.hidden = !enabled;
    if (!enabled) $$(".connection-card", group).forEach(closeConnection);
  });
}

function openSettings() {
  if (privateMode) leavePrivate("", false);
  if ($("#detail").open) $("#detail").close();
  setParam("view", "settings");
  $("#settings-title").focus();
}

function stopSettings() {
  settingsController?.abort();
  settingsController = null;
  settingsApps = [];
  settingsBusy = false;
  uploadOptionsReady = false;
  $("#upload-options-form").reset();
  $$("input, button", $("#upload-options-form")).forEach((control) => { control.disabled = true; });
  connectionMessage($("#upload-options-message"), "");
  $("#settings-connections").replaceChildren();
  $("#settings-status").textContent = "";
  $("#settings-option-message").textContent = "";
  $("#settings-adult").disabled = true;
  $("#my-account-form").reset();
  $("#settings-password-form").reset();
  $("#accounts-list").replaceChildren();
  $("#invites-list").replaceChildren();
  $("#create-account-form").reset();
  $("#create-invite-form").reset();
  $$("[data-permission-editor]").forEach(syncPermissionEditor);
  $$("#settings-users .connection-message, #settings-password-message, #account-message").forEach((message) => { message.textContent = ""; });
  $("#invite-link").value = "";
  $("#invite-result").hidden = true;
}

function connectionMessage(element, message, state = "") {
  element.textContent = message;
  element.className = `connection-message${state ? ` is-${state}` : ""}`;
}

function settingsPending(pending) {
  settingsBusy = pending;
  $("#settings-adult").disabled = pending;
  $$("input, button", $("#upload-options-form")).forEach((control) => { control.disabled = pending || !uploadOptionsReady; });
  $$("input, button", $("#settings-connections")).forEach((control) => { control.disabled = pending; });
}

async function loadConnections(message = "", focusKey) {
  if (!isAdmin()) return;
  if (!settingsController) settingsController = new AbortController();
  const controller = settingsController;
  settingsPending(true);
  $("#settings-retry").hidden = true;
  connectionMessage($("#settings-status"), message || "Loading connections…", "busy");
  try {
    const data = await api("api/setup/apps", { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(30000)]), cache: "no-store" });
    if (controller !== settingsController) return;
    settingsApps = data.apps || [];
    // The contract returns masked secrets. Discard any unexpected plaintext.
    for (const app of settingsApps) {
      for (const field of app.fields) {
        if (field.type === "secret" && app.values?.[field.key] && !/^[*•●?…]{3,}.{0,4}$/u.test(app.values[field.key])) app.values[field.key] = "";
      }
    }
    applyAdultOption(Boolean(data.options?.adult_enabled));
    fillUploadOptions(data.options || {});
    uploadOptionsReady = true;
    renderConnections();
    connectionMessage($("#settings-status"), message || (settingsApps.length ? "" : "No apps are available to connect."), message ? "success" : "");
    if (focusKey) $$(".connection-card").find((card) => card.dataset.key === focusKey)?.querySelector(".connection-edit").focus();
  } catch (error) {
    if (controller !== settingsController || error.message === "login") return;
    connectionMessage($("#settings-status"), error.status === 403 ? error.message : `${message ? `${message} ` : ""}Couldn’t load connections. Try again.`, "error");
    $("#settings-retry").hidden = false;
  } finally {
    if (controller === settingsController) {
      settingsPending(false);
      if (!settingsApps.length) $("#settings-adult").disabled = true;
    }
  }
}

function renderConnections() {
  const root = $("#settings-connections");
  root.replaceChildren();
  const groups = new Map();
  settingsApps.forEach((app, index) => {
    if (!groups.has(app.category)) {
      const group = document.createElement("section");
      group.className = "connection-group";
      group.setAttribute("aria-labelledby", `connection-category-${index}`);
      group.innerHTML = `<h3 id="connection-category-${index}">${esc(app.category)}</h3><div class="connection-cards"></div>`;
      if (app.category === "Private") { group.dataset.private = ""; group.hidden = !adultEnabled; }
      root.append(group);
      groups.set(app.category, $(".connection-cards", group));
    }
    const card = document.createElement("article");
    card.className = "connection-card";
    card.dataset.key = app.key;
    const status = app.connected ? (app.enabled ? "Connected" : "Off") : "Not set up";
    card.innerHTML = `<div class="connection-heading"><div><h4 id="connection-title-${index}">${esc(app.label)}</h4><span class="state-chip${app.connected && app.enabled ? " state-available" : ""}">${status}</span></div><button type="button" class="secondary-button connection-edit" aria-label="${app.connected ? "Edit" : "Set up"} ${esc(app.label)}" aria-expanded="false" aria-controls="connection-form-${index}">${app.connected ? "Edit" : "Set up"}</button></div><p class="hint">${esc(app.about)}</p>${app.mode ? `<p class="hint connection-mode">${esc(app.mode)}</p>` : ""}<div id="connection-form-${index}" class="connection-editor" hidden></div>`;
    card.setAttribute("aria-labelledby", `connection-title-${index}`);
    $(".connection-edit", card).addEventListener("click", () => {
      if (settingsBusy) return;
      if (!$(".connection-editor", card).hidden) closeConnection(card);
      else editConnection(app, index, card);
    });
    groups.get(app.category).append(card);
  });
}

function closeConnection(card) {
  const editor = $(".connection-editor", card);
  editor.replaceChildren();
  editor.hidden = true;
  const button = $(".connection-edit", card);
  button.setAttribute("aria-expanded", "false");
  const app = settingsApps.find((item) => item.key === card.dataset.key);
  button.textContent = app?.connected ? "Edit" : "Set up";
  button.setAttribute("aria-label", `${button.textContent} ${app?.label || "connection"}`);
}

function editConnection(app, index, card) {
  const editor = $(".connection-editor", card);
  editor.hidden = false;
  const edit = $(".connection-edit", card);
  edit.textContent = "Cancel";
  edit.setAttribute("aria-label", `Cancel editing ${app.label}`);
  edit.setAttribute("aria-expanded", "true");
  editor.innerHTML = `<form autocomplete="off" aria-labelledby="connection-title-${index}"><div class="field-stack connection-fields"></div>${app.connected ? `<label class="check" for="connection-enabled-${index}"><input id="connection-enabled-${index}" class="connection-enabled" type="checkbox" role="switch" ${app.enabled ? "checked" : ""}><span>Enabled</span></label><p class="hint">Turn off and Save to pause this connection while keeping its settings.</p>` : ""}<p class="connection-message" role="status" aria-live="polite" aria-atomic="true"></p><div class="connection-actions"><button class="secondary-button connection-test" type="button">Test</button><button class="primary-button connection-save" type="submit">Save</button><button class="secondary-button connection-force" type="button" hidden>Save anyway</button>${app.connected ? '<button class="text-button connection-disconnect" type="button">Disconnect</button>' : ""}</div></form>`;
  const form = $("form", editor);
  const inputs = app.fields.map((field, fieldIndex) => {
    const id = `connection-${index}-${fieldIndex}`;
    const wrapper = document.createElement("div");
    wrapper.className = "field";
    wrapper.innerHTML = `<label for="${id}">${esc(field.label)}${field.required ? ' <span class="required-mark" aria-hidden="true">*</span><span class="sr-only"> (required)</span>' : ' <span class="hint">(optional)</span>'}</label><div class="connection-input"><input id="${id}" type="${field.type === "secret" ? "password" : field.type === "url" ? "url" : "text"}" ${field.required ? "required" : ""} autocomplete="off" spellcheck="false" autocapitalize="none" aria-describedby="${id}-help">${field.type === "secret" ? `<button type="button" class="secondary-button secret-toggle" aria-label="Show ${esc(field.label)}" aria-controls="${id}" aria-pressed="false">Show</button>` : ""}</div><p id="${id}-help" class="hint">${esc(field.help || "")}${field.type === "list" ? " Separate values with commas." : ""}${field.type === "secret" && app.values?.[field.key] ? " Leave the masked value unchanged to keep the stored secret." : ""}</p>`;
    const input = $("input", wrapper);
    input.placeholder = field.placeholder || "";
    const value = app.values?.[field.key];
    input.value = Array.isArray(value) ? value.join(", ") : value ?? "";
    $(".secret-toggle", wrapper)?.addEventListener("click", (event) => {
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      event.currentTarget.textContent = show ? "Hide" : "Show";
      event.currentTarget.setAttribute("aria-label", `${show ? "Hide" : "Show"} ${field.label}`);
      event.currentTarget.setAttribute("aria-pressed", String(show));
    });
    $(".connection-fields", form).append(wrapper);
    return input;
  });
  const message = $(".connection-message", form);
  const forceButton = $(".connection-force", form);
  // Remember edited secret variants only while this form exists, to redact echoes.
  const secrets = new Set();
  const redact = (value) => {
    let text = String(value || "No message returned.");
    for (const secret of [...secrets].sort((a, b) => b.length - a.length)) text = text.split(secret).join("[redacted]");
    return text;
  };
  form.addEventListener("input", () => {
    forceButton.hidden = true;
    connectionMessage(message, "");
  });
  const perform = async (action, force = false) => {
    if (settingsBusy || !settingsController) return;
    if (action !== "disconnect" && !form.reportValidity()) return;
    if (action === "disconnect" && !confirm(`Disconnect ${app.label}? Its saved connection settings will be removed.`)) return;
    const values = Object.fromEntries(app.fields.map((field, fieldIndex) => {
      let value = inputs[fieldIndex].value;
      if (field.type === "secret" && value) {
        secrets.add(value);
        secrets.add(encodeURIComponent(value));
        secrets.add(JSON.stringify(value).slice(1, -1));
      }
      if (field.type !== "secret") value = value.trim();
      if (field.type === "list") value = value.split(",").map((item) => item.trim()).filter(Boolean);
      return [field.key, value];
    }));
    const controller = settingsController;
    const current = () => controller === settingsController && form.isConnected;
    const enabled = $(".connection-enabled", form)?.checked ?? true;
    forceButton.hidden = true;
    settingsPending(true);
    form.setAttribute("aria-busy", "true");
    connectionMessage(message, action === "test" ? "Testing connection… This can take up to 20 seconds." : action === "disconnect" ? "Disconnecting…" : "Saving connection… This can take up to 20 seconds.", "busy");
    try {
      const path = `api/setup/apps/${encodeURIComponent(app.key)}${action === "test" ? "/test" : ""}`;
      const result = await api(path, {
        method: action === "disconnect" ? "DELETE" : "POST",
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(45000)]),
        ...(action === "disconnect" ? {} : { body: JSON.stringify(action === "test" ? { values } : { values, enabled, force }) }),
      });
      if (!current()) return;
      if (action === "test" || !result.ok) {
        connectionMessage(message, redact(result.message), result.ok ? "success" : "error");
      } else {
        const feedback = action === "disconnect" ? `${app.label} disconnected.` : `Saved. ${redact(result.message || `${app.label} connection updated.`)} Library refresh started automatically.`;
        closeConnection(card);
        await loadConnections(feedback, app.key);
        refreshStatus();
      }
    } catch (error) {
      if (!current() || error.message === "login") return;
      const timedOut = error.name === "TimeoutError" || error.name === "AbortError";
      connectionMessage(message, timedOut ? (action === "test" ? "The connection test timed out. Try again." : "The request timed out. Your change may still finish; reopen Settings to check before retrying.") : redact(error.message), "error");
      forceButton.hidden = !(action === "save" && error.status === 400 && !force);
      if (!forceButton.hidden) message.append(document.createTextNode(" Not saved. Check the fields or choose Save anyway to keep these settings despite the failed test."));
    } finally {
      if (controller === settingsController) {
        settingsPending(false);
        form.removeAttribute("aria-busy");
      }
    }
  };
  form.addEventListener("submit", (event) => { event.preventDefault(); perform("save"); });
  $(".connection-test", form).addEventListener("click", () => perform("test"));
  forceButton.addEventListener("click", () => perform("save", true));
  $(".connection-disconnect", form)?.addEventListener("click", () => perform("disconnect"));
  (inputs[0] || $(".connection-save", form)).focus();
}

$("#settings-done").addEventListener("click", () => { settingsWelcome = false; clearAll(); $("#home").focus(); });
$("#settings-retry").addEventListener("click", () => { if (!settingsBusy) loadConnections(); });
$("#settings-adult").addEventListener("change", async (event) => {
  if (settingsBusy || !settingsController) return;
  const enabled = event.target.checked;
  const previous = adultEnabled;
  const controller = settingsController;
  settingsPending(true);
  connectionMessage($("#settings-option-message"), "Saving preference…", "busy");
  try {
    const result = await api("api/setup/options", { method: "POST", body: JSON.stringify({ adult_enabled: enabled }), signal: AbortSignal.any([controller.signal, AbortSignal.timeout(30000)]) });
    if (controller !== settingsController) return;
    if (!result.ok) throw new Error("Preference not saved");
    applyAdultOption(Boolean(result.options.adult_enabled));
    connectionMessage($("#settings-option-message"), adultEnabled ? "Private is available and stays PIN-locked." : "Private is hidden. Saved connections are kept.", "success");
  } catch (error) {
    if (controller !== settingsController || error.message === "login") return;
    $("#settings-adult").checked = previous;
    connectionMessage($("#settings-option-message"), error.status === 403 ? error.message : "Couldn’t confirm the preference was saved. Reopen Settings to check and try again.", "error");
  } finally {
    if (controller === settingsController) settingsPending(false);
  }
});

function downloadSection(work) {
  if (permissions.can_download !== true || isPrivateWork(work)) return "";
  const labels = { calibre: "Save ebook", storyteller: "Save read-along", abs: "Save audiobook", komga: "Save comic", jellyfin: "Save movie", romm: "Save game" };
  const editions = (work.editions || []).filter((edition) => !edition.hidden && edition.key && labels[edition.source] && (edition.source !== "jellyfin" || work.kind === "movie"));
  if (!editions.length) return "";
  const rows = editions.map((edition) => {
    const label = labels[edition.source];
    const url = `api/download/${encodeURIComponent(edition.key)}`;
    const formats = edition.source === "calibre" && Array.isArray(edition.extra?.formats)
      ? [...new Set(edition.extra.formats.filter((format) => typeof format === "string" && format.trim()).map((format) => format.trim().toUpperCase()))] : [];
    const control = formats.length > 1
      ? `<details class="save-formats"><summary>${label}</summary><div class="save-format-links">${formats.map((format) => `<a class="secondary-button" href="${esc(url)}?format=${encodeURIComponent(format.toLowerCase())}" download aria-label="Save ebook as ${esc(format)}">${esc(format)}</a>`).join("")}</div></details>`
      : `<a class="secondary-button" href="${esc(url)}" download>${label}</a>`;
    return `<div class="save-edition"><div class="save-edition-copy"><strong>${esc(edition.title || work.title)}</strong><small>${esc([SOURCE[edition.source], edition.library, formats.join(", ")].filter(Boolean).join(" · "))}</small></div>${control}</div>`;
  }).join("");
  return `<section class="detail-section"><div class="section-title"><h3>Save to device</h3></div><div class="save-editions">${rows}</div></section>`;
}

function miniSection(title, items, currentId, className = "") {
  items = (items || []).filter(allowedWork);
  if (!items?.length) return "";
  return `<section class="detail-section ${className}"><div class="section-title"><h3>${esc(title)}</h3><span>${items.length}</span></div><div class="detail-rail">${items.map((item) => card(item, { mini: true, current: String(item.id) === String(currentId) })).join("")}</div></section>`;
}

// Request cards share the same actions in detail views and external search results.
function requestPoster(item) {
  const title = item.label || item.title || "Untitled";
  const poster = item.poster ? safeUrl(item.poster) : "";
  return `<span class="cover-shell"><span class="cover-placeholder"><span class="placeholder-title">${esc(title)}</span></span>${poster ? `<img class="cover-image" loading="lazy" src="${esc(poster)}" alt="">` : ""}</span>`;
}

let gamePlatformsPromise;
let gamePickerSequence = 0;
function gamePlatforms() {
  // Share even an in-flight fetch across the footer and all adaptation cards.
  return gamePlatformsPromise ||= api("api/request/game/platforms");
}

function hintedGamePlatform(title, platforms) {
  const aliases = { snes: "snes", nes: "nes", n64: "n64", gba: "gba", gbc: "gbc", ps1: "psx", ps2: "ps2", ps3: "ps3", psp: "psp", pc: "win", windows: "win", gamecube: "ngc", dreamcast: "dc" };
  // Only explicit annotations such as "(SNES)" or "for Nintendo Switch" count.
  const hints = [...title.matchAll(/\(([^)]+)\)|\[([^\]]+)\]|\b(?:for|on)\s+(.+)$/gi)]
    .map((match) => (match[1] || match[2] || match[3]).trim().toLowerCase());
  const matches = platforms.filter((platform) => hints.some((hint) =>
    hint === platform.name.toLowerCase() || aliases[hint] === platform.slug));
  return matches.length === 1 ? matches[0].slug : "";
}

function renderGameRequest(root, title, { editable = false, onSuccess } = {}) {
  if (!canSubmitRequest()) { root.innerHTML = requestHint(); return; }
  const messageId = `game-request-message-${++gamePickerSequence}`;
  root.innerHTML = `<form class="game-request-form" aria-label="Request a game" aria-describedby="${messageId}">
    ${editable ? `<label class="game-title-field">Game title<input name="game" required value="${esc(title)}" autocomplete="off"></label>` : ""}
    <label>Filter platforms<input type="search" name="platform-filter" placeholder="Type to filter" autocomplete="off" disabled></label>
    <label>Platform<select name="platform" required disabled><option value="">Choose a platform</option></select></label>
    <button class="secondary-button" type="submit" disabled>${requestLabel("Request a game")}</button>
    <p class="request-message hint" id="${messageId}" aria-live="polite" aria-atomic="true" tabindex="-1">Loading platforms…</p>
  </form>`;
  const form = $("form", root);
  const input = $('[name="game"]', form);
  const filter = $('[name="platform-filter"]', form);
  const select = $("select", form);
  const submit = $('[type="submit"]', form);
  const message = $(".request-message", form);
  let platforms = [];
  let busy = false;
  let requested = false;
  const updateSubmit = () => {
    const platform = platforms.find((item) => item.slug === select.value);
    submit.textContent = requestLabel(platform ? `Request for ${platform.name}` : "Request a game");
    submit.disabled = busy || requested || !platform || !(input ? input.value : title).trim();
  };
  const showPlatforms = () => {
    const selected = select.value;
    const query = filter.value.trim().toLowerCase();
    const visible = platforms.filter((platform) => platform.name.toLowerCase().includes(query) || platform.slug.toLowerCase().includes(query));
    select.innerHTML = `<option value="">${visible.length ? "Choose a platform" : "No matching platforms"}</option>` + visible.map((platform) => `<option value="${esc(platform.slug)}">${esc(platform.name)}</option>`).join("");
    select.value = visible.some((platform) => platform.slug === selected) ? selected : "";
    updateSubmit();
    return visible.length;
  };
  filter.addEventListener("input", () => { message.textContent = `${showPlatforms()} platforms match.`; });
  select.addEventListener("change", updateSubmit);
  input?.addEventListener("input", updateSubmit);
  gamePlatforms().then((data) => {
    if (!root.isConnected) return;
    if (!data.enabled || !data.platforms?.length) {
      message.textContent = data.enabled ? "No platforms are available to request." : "Game requests are unavailable. Connect ROMarr to request games.";
      return;
    }
    platforms = data.platforms;
    showPlatforms();
    select.value = hintedGamePlatform(title, platforms);
    filter.disabled = select.disabled = false;
    updateSubmit();
    if (document.activeElement === message) filter.focus();
    message.textContent = "";
  }).catch((error) => {
    if (root.isConnected) message.textContent = error.message === "login" ? "Sign in to request games." : error.message;
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || requested || submit.disabled || !form.reportValidity()) return;
    const game = (input ? input.value : title).trim();
    const platform = platforms.find((item) => item.slug === select.value);
    if (!game || !platform) return;
    const hadFocus = form.contains(document.activeElement);
    busy = true;
    $$("input, select, button", form).forEach((control) => { control.disabled = true; });
    submit.textContent = "Requesting…";
    message.textContent = "Submitting request…";
    if (hadFocus) message.focus();
    try {
      const result = await api("api/request/game", { method: "POST", body: JSON.stringify({ game, platform: platform.slug }) });
      requested = true;
      if (!root.isConnected) return;
      submit.textContent = requestResultLabel(result);
      submit.className = "request-pill";
      message.textContent = requestResultMessage(result, `${game} requested for ${platform.name}.`);
      if (onSuccess) onSuccess(message.textContent, hadFocus && form.contains(document.activeElement), result);
      else if (hadFocus && form.contains(document.activeElement)) message.focus();
    } catch (error) {
      if (!root.isConnected) return;
      message.textContent = error.message === "login" ? "Sign in to request this game." : error.message;
      $$("input, select, button", form).forEach((control) => { control.disabled = false; });
    } finally {
      busy = false;
      if (!requested) updateSubmit();
    }
  });
}

function renderRequestCards(root, items, seerrEnabled = true, search = false, romarrEnabled = false) {
  items = items.filter((item) => !isPrivateWork(item));
  root.innerHTML = items.map((item) => {
    const title = item.label || item.title || "Untitled";
    const kind = { movie: "Movie", tv: "TV", game: "Game", book: "Book", comic: "Comic" }[item.kind] || item.kind;
    const url = item.url ? safeUrl(item.url) : "";
    let action;
    if (item.status === "available") {
      action = item.in_library ? `<button class="secondary-button" type="button" data-open aria-label="Open ${esc(title)}">Open</button>` : `<span class="request-pill">${search ? "Available" : "In Jellyfin"}</span>`;
    } else if (!canSubmitRequest()) {
      action = '<span class="hint">Not available to play yet</span>';
    } else if (["requested", "partial", "pending"].includes(item.status)) {
      action = `<button class="request-pill" type="button" disabled>${item.status === "pending" ? "Sent for approval" : item.status === "partial" ? "Partly available" : "Requested"}</button>`;
    } else if (["book", "comic"].includes(item.kind) && item.status === "not_requested") {
      action = `<button class="secondary-button" type="button" data-book-request aria-expanded="false" aria-label="${requestLabel()} · ${esc(title)}">${requestLabel()}</button>`;
    } else if (["book", "comic"].includes(item.kind)) {
      action = '<span class="request-pill">Not in your library</span>';
    } else if (item.kind === "game" && item.status === "no_requester") {
      action = '<span class="hint">Games can\'t be requested automatically</span>';
    } else if (item.kind === "game" && item.status === "not_requested" && romarrEnabled) {
      action = `<button class="secondary-button" type="button" data-game-request aria-expanded="false" aria-label="${requestLabel()} · ${esc(title)}">${requestLabel()}</button>`;
    } else if (item.status === "not_requested" && seerrEnabled && item.tmdb && ["movie", "tv"].includes(item.kind)) {
      action = `<button class="secondary-button" type="button" data-request aria-label="${requestLabel()} · ${esc(title)}">${requestLabel()}</button>`;
    } else {
      const label = item.status === "blocked" ? "Request blocked" : item.status === "unknown" ? "Availability unknown" : "Requests unavailable";
      action = `<span class="request-pill">${label}</span>`;
    }
    return `<article class="request-card">${requestPoster(item)}<h4 class="card-title">${esc(title)}</h4><p class="card-subtitle">${esc([item.year, kind].filter(Boolean).join(" · "))}</p><div class="request-card-actions">${action}${url ? `<a class="request-details" href="${esc(url)}" target="_blank" rel="noopener noreferrer" aria-label="${item.kind === "game" ? "View on IGDB" : "Details"} for ${esc(title)} (opens in a new tab)">${item.kind === "game" ? "View on IGDB" : "Details ↗"}</a>` : ""}</div><p class="request-message hint" aria-live="polite" aria-atomic="true"></p></article>`;
  }).join("");
  $$(".cover-image", root).forEach((image) => image.addEventListener("error", () => image.remove(), { once: true }));
  $$(".request-card", root).forEach((card, index) => {
    const item = items[index];
    $("[data-open]", card)?.addEventListener("click", () => openWork(item.in_library));
    const bookButton = $("[data-book-request]", card);
    if (bookButton) bindRelatedBookRequest(card, bookButton, item);
    const gameButton = $("[data-game-request]", card);
    if (gameButton) {
      const picker = document.createElement("div");
      picker.id = `game-picker-${++gamePickerSequence}`;
      picker.className = "game-request";
      picker.hidden = true;
      card.append(picker);
      gameButton.setAttribute("aria-controls", picker.id);
      gameButton.addEventListener("click", () => {
        picker.hidden = !picker.hidden;
        gameButton.setAttribute("aria-expanded", String(!picker.hidden));
        if (picker.hidden) return;
        if (!picker.hasChildNodes()) renderGameRequest(picker, item.label || item.title || "", {
          onSuccess: (text, focus, result) => {
            item.status = result.queued ? "pending" : "requested";
            gameButton.textContent = requestResultLabel(result);
            gameButton.className = "request-pill";
            gameButton.disabled = true;
            gameButton.setAttribute("aria-label", `${requestResultLabel(result)} · ${item.label || item.title}`);
            gameButton.setAttribute("aria-expanded", "false");
            picker.hidden = true;
            const message = $(".request-message", card);
            message.textContent = text;
            message.tabIndex = -1;
            if (focus) message.focus();
          },
        });
        const target = $("input:not(:disabled)", picker) || $(".request-message", picker);
        target.focus();
      });
    }
    const button = $("[data-request]", card);
    button?.addEventListener("click", async () => {
      if (button.disabled) return;
      const message = $(".request-message", card);
      button.disabled = true;
      button.textContent = "Requesting…";
      message.textContent = "";
      try {
        const result = await api("api/request/screen", { method: "POST", body: JSON.stringify({ kind: item.kind, tmdb: item.tmdb, title: item.title || item.label || "" }) });
        item.status = result.queued ? "pending" : "requested";
        button.textContent = requestResultLabel(result);
        button.className = "request-pill";
        button.setAttribute("aria-label", `${requestResultLabel(result)} · ${item.label || item.title}`);
        message.textContent = requestResultMessage(result, "Request sent.");
      } catch (error) {
        button.disabled = false;
        button.textContent = requestLabel();
        message.textContent = error.message === "login" ? "Sign in to request this title." : error.message;
      }
    });
  });
}

// Request a related book or comic that isn't in the library (Shelfmark): pick the format,
// then let Omnarr keep looking or choose a copy by hand.
function bindRelatedBookRequest(card, button, item) {
  const panel = document.createElement("div");
  panel.className = "related-book-request";
  panel.hidden = true;
  panel.id = `related-book-${++gamePickerSequence}`;
  card.append(panel);
  button.setAttribute("aria-controls", panel.id);
  const title = item.label || item.title || "";
  const target = { title, author: (item.authors || [])[0] || "" };
  const message = $(".request-message", card);
  const done = (text, result) => {
    item.status = result.queued ? "pending" : "requested";
    button.textContent = requestResultLabel(result);
    button.setAttribute("aria-label", `${requestResultLabel(result)} · ${title}`);
    button.className = "request-pill";
    button.disabled = true;
    button.setAttribute("aria-expanded", "false");
    message.textContent = requestResultMessage(result, text);
    if (result.queued) {
      const hadFocus = panel.contains(document.activeElement) || document.activeElement === button;
      panel.hidden = true;
      message.tabIndex = -1;
      if (hadFocus) message.focus();
    }
  };
  // One click, like movies and shows: Request -> Omnarr keeps looking until it finds a good copy
  // (books ask ebook or audiobook first). "Choose a copy myself" is there for picking by hand.
  const LABEL = { ebook: "ebook", audiobook: "audiobook", comic: "comic" };
  const manualLink = (format) => {
    const link = document.createElement("button");
    link.type = "button";
    link.className = "text-button";
    link.textContent = "Choose a copy myself";
    link.setAttribute("aria-expanded", "false");
    const picker = document.createElement("div");
    picker.className = "request-picker";
    picker.hidden = true;
    link.addEventListener("click", () => {
      panel.hidden = false;
      openBookPicker(picker, target, format, link, new AbortController().signal, () => request(format),
        (result) => done(`Your chosen copy of the ${LABEL[format]} has been requested. If it fails, Omnarr finds another.`, result));
    });
    return [link, picker];
  };
  const request = async (format) => {
    button.disabled = true;
    $$("button", panel).forEach((b) => { b.disabled = true; });
    button.textContent = "Requesting…";
    message.textContent = "";
    try {
      const result = await api("api/wanted", { method: "POST", body: JSON.stringify({ ...target, format }) });
      done(`Requested. Omnarr keeps looking for the ${LABEL[format]} until it finds a good copy.`, result);
      if (result.queued) return;
      const [link, picker] = manualLink(format);
      panel.replaceChildren(link, picker);
      panel.hidden = false;
    } catch (error) {
      button.disabled = false;
      button.textContent = requestLabel();
      $$("button", panel).forEach((b) => { b.disabled = false; });
      message.textContent = error.message === "login" ? "Sign in to request this." : error.message;
    }
  };
  button.addEventListener("click", () => {
    if (item.kind === "comic") return request("comic");
    panel.hidden = !panel.hidden;
    button.setAttribute("aria-expanded", String(!panel.hidden));
    if (panel.hidden) return;
    panel.innerHTML = `<p class="hint">Which format? ${canRequest() ? "Omnarr keeps looking until it finds a good copy." : "An admin will review your request."}</p><div class="request-card-actions"><button class="secondary-button" type="button" data-format="ebook">Ebook</button><button class="secondary-button" type="button" data-format="audiobook">Audiobook</button></div>`;
    $$("[data-format]", panel).forEach((b) => b.addEventListener("click", () => request(b.dataset.format)));
    $("[data-format]", panel).focus();
  });
}

let requestSearchController;
function clearRequestSearch() {
  requestSearchController?.abort();
  $("#search-game-request")?.remove();
  $("#request-search")?.remove();
}

function renderRequestSearch() {
  clearRequestSearch();
  if (privateMode) return;
  const query = (params.get("q") || "").trim();
  if (!query) return;
  const controller = new AbortController();
  requestSearchController = controller;
  const section = document.createElement("section");
  section.id = "request-search";
  section.className = "request-search";
  section.setAttribute("aria-label", "Search titles to request");
  if (!canSubmitRequest()) {
    section.innerHTML = requestHint();
    $("#results-view").append(section);
    return;
  }
  section.innerHTML = '<div class="request-search-heading"><h2>Not in your library?</h2><div class="request-search-actions"><button class="secondary-button" type="button" data-screen-search>Search movies &amp; TV to request</button><button class="secondary-button" type="button" data-game-toggle aria-expanded="false" aria-controls="search-game-request">Request a game</button></div></div><div class="game-request" id="search-game-request" hidden><h3>Request a game</h3><div data-game-form></div></div><p class="hint" data-message aria-live="polite" aria-atomic="true"></p><div class="request-grid"></div>';
  $("#results-view").append(section);
  const gameRequest = $("#search-game-request", section);
  renderGameRequest($("[data-game-form]", gameRequest), query, { editable: true });
  const gameToggle = $("[data-game-toggle]", section);
  gameToggle.textContent = requestLabel("Request a game");
  gameToggle.addEventListener("click", () => {
    gameRequest.hidden = !gameRequest.hidden;
    gameToggle.setAttribute("aria-expanded", String(!gameRequest.hidden));
    if (!gameRequest.hidden) $("input", gameRequest).focus();
  });
  const button = $("[data-screen-search]", section);
  const message = $("[data-message]", section);
  button.addEventListener("click", async () => {
    button.disabled = true;
    message.textContent = "Searching movies & TV…";
    try {
      const data = await api(`api/request/search?${new URLSearchParams({ q: query })}`, { signal: controller.signal });
      if (controller.signal.aborted || !section.isConnected) return;
      const results = data.results || [];
      renderRequestCards($(".request-grid", section), results, true, true);
      message.textContent = results.length ? `${results.length} titles found for “${query}”.` : `No movies or TV found for “${query}”.`;
    } catch (error) {
      if (controller.signal.aborted || !section.isConnected) return;
      message.textContent = error.message === "login" ? "Sign in to search for requests." : error.message;
    } finally {
      button.disabled = false;
    }
  });
}

function releaseDetails(release) {
  const present = (value) => value != null && value !== "";
  return [release.format, present(release.size) ? `Size: ${release.size}` : "", release.language,
    release.source_display_name || release.source, present(release.seeders) ? `${release.seeders} seeders` : "",
    release.extra?.narrator ? `Narrator: ${release.extra.narrator}` : "",
    present(release.extra?.duration) ? `Duration: ${release.extra.duration}` : ""].filter(present).join(" · ");
}

function wantedStatus(item) {
  const label = { searching: "Searching", downloading: "Downloading", done: "Found" }[item.status] || "Searching";
  const tone = item.status === "done" ? " state-available" : item.status === "downloading" ? " state-coming" : "";
  let next = "";
  if (item.status !== "done" && item.next_search != null && Number.isFinite(Number(item.next_search))) {
    const seconds = Number(item.next_search) - Date.now() / 1000;
    const [unit, divisor] = seconds >= 86400 ? ["day", 86400] : seconds >= 3600 ? ["hour", 3600] : ["minute", 60];
    const relative = seconds <= 0 ? "now" : new Intl.RelativeTimeFormat("en", { numeric: "always" }).format(Math.max(1, Math.round(seconds / divisor)), unit);
    next = `<p class="hint">Next try ${esc(relative)}</p>`;
  }
  const format = item.format === "audiobook" ? "audiobook" : "ebook";
  return `<div class="wanted-chips"><span class="state-chip format-chip f-${format}">${format === "audiobook" ? "Audiobook" : "Ebook"}</span><span class="state-chip${tone}">${label}</span></div>${item.note ? `<p class="hint wanted-note">${esc(item.note)}</p>` : ""}${next}`;
}

function findWanted(items, workId, title, format) {
  const rows = items.filter((item) => item.format === format);
  const normalized = String(title || "").trim().toLowerCase();
  return rows.find((item) => item.work_id != null && String(item.work_id) === String(workId))
    || rows.find((item) => !item.work_id && normalized && String(item.title || "").trim().toLowerCase() === normalized);
}

function openBookPicker(root, workId, format, opener, detailSignal, keepLooking, onDownloaded) {
  if (!canSubmitRequest()) { root.innerHTML = requestHint(); return; }
  // workId is a library work id, or { title, author } for a book/comic that isn't in the library
  const target = typeof workId === "string" ? { work: workId } : { title: workId.title || "", author: workId.author || "" };
  let controller;
  let step = 0;
  let candidates = [];
  let query = "";
  const active = () => root.isConnected && !detailSignal.aborted;
  const cancel = () => { ++step; controller?.abort(); };
  detailSignal.addEventListener("abort", cancel, { once: true });
  root.hidden = false;
  opener.setAttribute("aria-expanded", "true");
  root.innerHTML = `<div class="request-picker-head"><h4 tabindex="-1">Request the ${esc(format)}</h4><button class="text-button" type="button" data-back>Cancel</button></div><p class="hint" data-progress aria-live="polite" aria-atomic="true"></p><div class="request-options"></div>`;
  const progress = $("[data-progress]", root);
  const options = $(".request-options", root);
  const back = $("[data-back]", root);
  $("h4", root).focus();

  function close() {
    cancel();
    root.hidden = true;
    root.replaceChildren();
    opener.setAttribute("aria-expanded", "false");
    const holder = opener.closest(".book-request");             // absent for related-work requests
    const auto = holder && $("[data-auto]", holder);
    if (auto) auto.disabled = false;
    opener.focus();
    detailSignal.removeEventListener("abort", cancel);
  }
  function retry(message, action) {
    progress.textContent = message;
    options.innerHTML = '<button class="secondary-button" type="button">Try again</button>';
    $("button", options).addEventListener("click", action);
  }
  function showCandidates(focus = false) {
    cancel();
    back.textContent = "Cancel";
    back.onclick = close;
    progress.textContent = candidates.length ? `Choose the right book${query ? ` for “${query}”` : ""}.` : "No matching books found. Try again or go back.";
    if (!candidates.length) {
      retry(progress.textContent, loadCandidates);
      return;
    }
    options.innerHTML = candidates.map((candidate) => {
      const cover = candidate.cover ? safeUrl(candidate.cover) : "";
      return `<button class="request-option" type="button">${cover ? `<img class="candidate-cover" src="${esc(cover)}" alt="" loading="lazy">` : '<span class="candidate-cover candidate-placeholder" aria-hidden="true">B</span>'}<span class="request-option-copy"><strong>${esc(candidate.title || "Untitled")}</strong><small>${esc([candidate.authors?.join(", "), candidate.year, candidate.provider].filter(Boolean).join(" · "))}</small></span><span class="request-option-verb">Choose</span></button>`;
    }).join("");
    $$("img", options).forEach((image) => image.addEventListener("error", () => image.remove(), { once: true }));
    $$("button", options).forEach((button, index) => button.addEventListener("click", () => loadReleases(candidates[index])));
    if (focus) $("button", options)?.focus();
  }
  async function loadCandidates() {
    cancel();
    const current = step;
    controller = new AbortController();
    back.textContent = "Cancel";
    back.onclick = close;
    options.replaceChildren();
    progress.textContent = "Finding matching books…";
    try {
      const data = await api(`api/request/book/candidates?${new URLSearchParams({ ...target, format })}`, { signal: controller.signal });
      if (!active() || current !== step) return;
      candidates = data.candidates || [];
      query = data.query || "";
      showCandidates();
    } catch (error) {
      if (active() && current === step) retry(error.message, loadCandidates);
    }
  }
  async function loadReleases(candidate) {
    cancel();
    const current = step;
    controller = new AbortController();
    back.textContent = "Back to books";
    back.onclick = () => showCandidates(true);
    back.focus();
    options.replaceChildren();
    progress.textContent = "Searching sources…";
    try {
      const data = await api(`api/request/book/releases?${new URLSearchParams({ provider: candidate.provider, book_id: candidate.book_id, format })}`, { signal: controller.signal });
      if (!active() || current !== step) return;
      const releases = data.releases || [];
      if (!releases.length) {
        retry("No releases found. Try again, choose another book, or let Omnarr keep looking.", () => loadReleases(candidate));
        const keep = document.createElement("button");
        keep.type = "button";
        keep.className = "primary-button";
        keep.textContent = requestLabel("Keep looking for me");
        keep.addEventListener("click", keepLooking);
        options.prepend(keep);
        return;
      }
      progress.textContent = "Choose a release to request.";
      options.innerHTML = releases.map((release) => `<button class="request-option" type="button"><span class="request-option-copy"><strong>${esc(release.title || candidate.title || "Untitled release")}</strong><small>${esc(releaseDetails(release))}</small></span><span class="request-option-verb">${requestLabel()}</span></button>`).join("");
      $$("button", options).forEach((button, index) => button.addEventListener("click", async () => {
        if (button.disabled) return;
        $$("button", options).forEach((item) => { item.disabled = true; });
        back.disabled = true;
        progress.textContent = "Submitting request…";
        try {
          // Keep the opaque Shelfmark release intact, including all provider fields.
          const result = await api("api/request/book/download", { method: "POST", body: JSON.stringify({ release: releases[index], ...target, format, provider: candidate.provider, book_id: candidate.book_id }) });
          if (!active() || current !== step) return;
          onDownloaded(result);
        } catch (error) {
          if (!active() || current !== step) return;
          progress.textContent = error.message;
          $$("button", options).forEach((item) => { item.disabled = false; });
        } finally {
          back.disabled = false;
        }
      }));
    } catch (error) {
      if (active() && current === step) retry(error.message, () => loadReleases(candidate));
    }
  }
  loadCandidates();
}

async function loadWorkRequests(root, workId, result, signal, work) {
  const { data, error } = await result;
  if (signal.aborted || !root.isConnected) return;
  const message = $("[data-request-status]", root);
  if (error) {
    message.textContent = "Request options couldn’t be loaded right now.";
    return;
  }
  const formats = (canSubmitRequest() ? data.missing_formats || [] : []).filter((format) => ["ebook", "audiobook"].includes(format));
  const adaptations = [...(data.adaptations || [])].sort((a, b) => (Number(a.year) || Infinity) - (Number(b.year) || Infinity));
  const screens = adaptations.filter((item) => ["movie", "tv", "game"].includes(item.kind));
  const reading = adaptations.filter((item) => ["book", "comic"].includes(item.kind));
  message.textContent = !canSubmitRequest() ? REQUEST_HINT : !formats.length && !adaptations.length ? "Nothing related found yet." : "";
  const content = $("[data-request-content]", root);
  content.innerHTML = `${formats.length ? `<div class="request-group"><h4>Also available to request</h4>${!data.shelfmark_enabled ? '<p class="hint" id="shelfmark-hint">Connect Shelfmark in config to request books</p>' : ""}${formats.map((format) => `<div class="book-request" data-book-format="${format}"><h5>Request the ${format}</h5><div data-book-options><p class="hint">Checking wanted status…</p></div><p class="hint request-message" data-book-message role="status" tabindex="-1"></p></div>`).join("")}</div>` : ""}${screens.length ? '<div class="request-group"><h4>On screen &amp; in games</h4><div class="request-grid" data-related="screens"></div></div>' : ""}${reading.length ? '<div class="request-group"><h4>Books &amp; comics</h4><div class="request-grid" data-related="reading"></div></div>' : ""}`;
  if (screens.length) renderRequestCards($('[data-related="screens"]', content), screens, data.seerr_enabled, false, data.romarr_enabled);
  if (reading.length) renderRequestCards($('[data-related="reading"]', content), reading, data.seerr_enabled, false, data.romarr_enabled);
  if (!formats.length) return;
  async function loadWanted() {
    let items;
    try {
      items = (await api("api/wanted", { signal, cache: "no-store" })).items || [];
    } catch (error) {
      if (signal.aborted || !root.isConnected) return;
      $$("[data-book-options]", content).forEach((options) => {
        options.innerHTML = '<p class="hint" role="status">Couldn’t check wanted status.</p><button class="secondary-button" type="button">Try again</button>';
        $("button", options).onclick = () => {
          $$("[data-book-options] button", content).forEach((button) => { button.disabled = true; });
          loadWanted();
        };
      });
      return;
    }
    if (signal.aborted || !root.isConnected) return;
    $$("[data-book-format]", content).forEach((row) => {
      const format = row.dataset.bookFormat;
      const options = $("[data-book-options]", row);
      const status = $("[data-book-message]", row);
      const showQueued = (result) => {
        options.innerHTML = '<button class="request-pill" type="button" disabled>Sent for approval</button>';
        status.textContent = requestResultMessage(result, "");
        status.focus();
      };
      const showStatus = (item, focus = false) => {
        options.innerHTML = wantedStatus(item);
        if (focus) {
          status.textContent = item.status === "downloading" ? "Copy requested. Omnarr will try another copy if it fails." : "Added to books we’re looking for.";
          status.focus();
        }
      };
      const existing = findWanted(items, workId, work.title, format);
      if (existing) { showStatus(existing); return; }
      const disabled = data.shelfmark_enabled ? "" : " disabled";
      const description = `wanted-help-${format}${data.shelfmark_enabled ? "" : " shelfmark-hint"}`;
      options.innerHTML = `<div class="book-request-actions"><button class="primary-button" type="button" data-auto aria-describedby="${description}"${disabled}>${requestLabel("Get it for me")}</button><button class="secondary-button" type="button" data-manual aria-expanded="false" aria-controls="picker-${format}" aria-describedby="${description}"${disabled}>Choose a copy myself</button></div><p class="hint" id="wanted-help-${format}">${canRequest() ? "Omnarr picks a good copy and keeps trying every 3 days until it arrives" : "Your request waits in Activity → Requests until an admin approves it."}</p><div class="request-picker" id="picker-${format}" hidden></div>`;
      let submitting = false;
      const keepLooking = async () => {
        if (submitting || signal.aborted || !root.isConnected) return;
        submitting = true;
        const buttons = $$("button", options).map((button) => [button, button.disabled]);
        buttons.forEach(([button]) => { button.disabled = true; });
        status.textContent = "Adding to books we’re looking for…";
        try {
          const result = await api("api/wanted", { method: "POST", body: JSON.stringify({ work: workId, format }) });
          if (signal.aborted || !root.isConnected) return;
          if (result.queued) { showQueued(result); return; }
          showStatus({ format, status: "searching", note: "Omnarr is looking for a good copy." }, true);
        } catch (error) {
          if (signal.aborted || !root.isConnected) return;
          status.textContent = error.message;
          buttons.forEach(([button, disabled]) => { button.disabled = disabled; });
        } finally {
          submitting = false;
        }
      };
      $("[data-auto]", options).onclick = keepLooking;
      const manual = $("[data-manual]", options);
      manual.onclick = () => {
        const picker = $(".request-picker", options);
        if (!picker.hidden) { $("h4", picker).focus(); return; }
        $("[data-auto]", options).disabled = true;
        openBookPicker(picker, workId, format, manual, signal, keepLooking, (result) => result.queued ? showQueued(result) : showStatus({ format, status: "downloading", note: "Your chosen copy has been requested." }, true));
      };
    });
  }
  await loadWanted();
}

// Live information is independent of the indexed work and its existing app links.
const liveCache = new Map();

// Keep the full episode order so the player can advance without more live requests.
function playbackEpisodes(data) {
  return (data.seasons || []).flatMap((season) => season.episodes || []).sort((a, b) =>
    (a.season === 0 ? Infinity : a.season) - (b.season === 0 ? Infinity : b.season) || a.episode - b.episode);
}

function playButton(type, id, label, context = "") {
  return `<button type="button" class="primary-button live-button" data-play-${type}="${esc(id)}"${context ? ` aria-label="${esc(label)} — ${esc(context)}"` : ""}>${esc(label)}</button>`;
}

function playbackActions(work) {
  const reading = (work.editions || []).filter((edition) => !edition.hidden && (edition.key || edition.source_id != null) &&
    (edition.source === "komga" || (edition.source === "calibre" && (edition.extra?.formats || []).some((format) => /^(EPUB|KEPUB)$/i.test(format)))));
  const readButtons = reading.map((edition) => {
    const key = edition.key || `${edition.source}:${edition.source_id}`;
    const continuing = work.status === "in_progress" || (!edition.finished && edition.progress > 0);
    const source = reading.length > 1 ? ` · ${SOURCE[edition.source] || edition.source}` : "";
    return `<button type="button" class="primary-button live-button" data-read="${esc(key)}" data-read-source="${esc(source)}" data-read-continuing="${continuing}" aria-label="${esc((continuing ? "Continue reading" : "Read") + source + " — " + work.title)}">${continuing ? "Continue reading" : "Read"}${esc(source)}</button>`;
  }).join("");
  return (work.editions || []).filter((edition) => !edition.hidden && edition.source_id).map((edition) => {
    if (work.kind === "movie" && edition.source === "jellyfin") return playButton("video", edition.source_id, "Play", work.title);
    if (work.kind === "book" && edition.source === "abs") return playButton("audio", edition.source_id, "Listen here", work.title);
    return "";
  }).join("") + readButtons;
}

function bindPlayback(root, episodes = []) {
  $$("[data-play-video]", root).forEach((button) => button.addEventListener("click", () => window.OmnarrPlayer.openVideo(button.dataset.playVideo, episodes)));
  $$("[data-play-audio]", root).forEach((button) => button.addEventListener("click", () => window.OmnarrPlayer.openAudio(button.dataset.playAudio)));
}

function bindReading(root, signal) {
  $$("[data-read]", root).forEach((button) => {
    button.addEventListener("click", () => window.OmnarrReader.open(button.dataset.read, button));
    // Only the open work requests resume details, sharing its cancellation lifetime.
    void window.OmnarrReader.readInfo(button.dataset.read, signal).then((info) => {
      if (signal.aborted || !button.isConnected) return;
      const resume = info.resume || {};
      const continuing = !resume.finished && (resume.page > 1 || resume.fraction > 0 || resume.locator || button.dataset.readContinuing === "true");
      const position = info.mode === "pages" ? `page ${resume.page || 1} of ${info.pages.length}` : `${Math.round(percent((resume.fraction || 0) * 100))}%`;
      button.textContent = `${continuing ? "Continue reading" : "Read"}${button.dataset.readSource}${continuing ? ` · ${position}` : ""}`;
      button.setAttribute("aria-label", `${button.textContent} — ${info.title}`);
    }).catch(() => { /* Read remains available; opening it shows the endpoint's error. */ });
  });
}

async function updatePlaybackLive(data, signal) {
  const root = $("#detail-body");
  if (data.kind === "movie") {
    const resume = Number(data.resume ?? data.position) || 0;
    $$(".detail-playback [data-play-video]", root).forEach((button) => {
      button.textContent = resume > 60 ? `Resume from ${timestamp(resume)}` : "Play";
      button.setAttribute("aria-label", button.textContent);
    });
  }
  if (data.kind === "show") {
    const episodes = playbackEpisodes(data);
    const available = episodes.filter((episode) => episode.jellyfin_id && episode.has_file && !episode.watched);
    const next = available.find((episode) => Number(episode.position ?? episode.resume) > 0) || available[0];
    const actions = $(".detail-playback", root);
    if (actions) {
      actions.innerHTML = next ? playButton("video", next.jellyfin_id, "Play next", `S${next.season}E${next.episode} ${next.title || ""}`) : "";
      bindPlayback(actions, episodes);
    }
    bindPlayback($("[data-live-content]", root), episodes);
  }
  if (data.kind === "book" && data.positions?.length) {
    // The live panel has percentages; the play endpoint has the exact ABS resume.
    await Promise.all($$(".detail-playback [data-play-audio]", root).map(async (button) => {
      try {
        const info = await window.OmnarrPlayer.audioInfo(button.dataset.playAudio, signal);
        if (signal.aborted || !button.isConnected || !(info.resume > 0)) return;
        const chapters = info.chapters?.length ? info.chapters : data.chapters || [];
        const index = chapters.findLastIndex((chapter) => Number(chapter.start) <= info.resume);
        const label = index < 0 ? `Resume from ${timestamp(info.resume)}` : `Resume at ${chapters[index].title || `Chapter ${index + 1}`}`;
        button.textContent = `Listen here · ${label}`;
        button.setAttribute("aria-label", button.textContent);
      } catch { /* Keep Listen here available if resume information is unavailable. */ }
    }));
  }
}
const percent = (value) => Math.min(100, Math.max(0, Number(value) || 0));
const countText = (value) => Math.max(0, Number(value) || 0).toLocaleString();
const sizeText = (value) => {
  const bytes = Number(value) || 0;
  if (bytes >= 1e12) return `${(bytes / 1e12).toFixed(1)} TB`;
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`;
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(1)} MB`;
  return `${Math.max(0, bytes).toLocaleString()} B`;
};
const timestamp = (value) => {
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  return `${Math.floor(seconds / 3600)}:${String(Math.floor(seconds / 60) % 60).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
};

function availabilityChip(work) {
  if (!["show", "movie"].includes(work.kind)) return "";
  const info = work.info || {};
  const label = info.availability === "partial" ? `Partial ${countText(info.have)}/${countText(info.total ?? info.aired)}` : AVAILABILITY[info.availability];
  return label ? `<span class="availability-chip availability-${esc(info.availability)}">${esc(label)}</span>` : "";
}

function liveSkeleton() {
  return '<div class="live-skeleton" role="status"><span class="sr-only">Loading live details…</span><span class="skeleton skeleton-line"></span><span class="skeleton skeleton-line short"></span><span class="skeleton skeleton-line"></span></div>';
}

function progressBar(value, label) {
  const progress = percent(value);
  return `<progress class="live-progress" max="100" value="${progress}" aria-label="${esc(label)}">${progress}%</progress>`;
}

function actionButton(action, payload, label, options = {}) {
  if (!canRequest()) return "";
  const data = { action, ...payload };
  const key = JSON.stringify(data);
  return `<button type="button" class="secondary-button live-button${options.monitor ? " monitor-button" : ""}" data-live-action="${esc(key)}"${options.monitor ? ` aria-pressed="${Boolean(payload.monitored === false)}"` : ""}${options.context ? ` aria-label="${esc(label)} — ${esc(options.context)}"` : ""}${options.title ? ` title="${esc(options.title)}"` : ""}>${esc(label)}</button>`;
}

function searchHarderButton(payload, context) {
  return actionButton("search_harder", payload, "Search harder", {
    context,
    title: "Searches every indexer directly — useful for older shows or when a newer spin-off crowds out results.",
  });
}

function searchHarderResult(result) {
  const summary = (item) => {
    if (item.grabbed) return `Found and downloading: ${item.grabbed}`;
    const reasons = Object.entries(item.rejected || {}).sort((a, b) => b[1] - a[1]);
    const topReasons = reasons.slice(0, 3).map(([reason, count]) => `${reason} (${countText(count)})`).join("; ");
    const more = reasons.length > 3 ? `; +${countText(reasons.length - 3)} more reasons` : "";
    return `Nothing new: ${countText(item.candidates)} candidates — ${topReasons || (item.candidates > 0 ? "No rejection reasons reported" : "No releases found")}${more}`;
  };
  if (Array.isArray(result.seasons)) {
    return result.seasons.map((season) => `${season.season === 0 ? "Specials" : `Season ${season.season}`}: ${summary(season)}`).join("\n") || "Nothing new: no seasons searched.";
  }
  return summary(result);
}

function monitorButton(action, payload, monitored, context) {
  return actionButton(action, { ...payload, monitored: !monitored }, monitored ? "✓ Monitored" : "Not monitored", { monitor: true, context });
}

function downloadStatus(record, label = "Download") {
  const warning = ["warning", "error"].includes(String(record.tracked).toLowerCase()) || ["warning", "failed", "error"].includes(String(record.status).toLowerCase());
  return `<div class="download-status${warning ? " has-warning" : ""}"><div class="progress-caption"><span>${esc(record.status || "Downloading")} · ${Math.round(percent(record.progress))}%</span>${record.eta ? `<span>ETA ${esc(record.eta)}</span>` : ""}</div>${progressBar(record.progress, label)}${record.state ? `<p class="hint">${esc(record.state)}</p>` : ""}${warning ? '<p class="warning-label">Needs attention</p>' : ""}${Array.isArray(record.messages) && record.messages.length ? `<ul class="download-messages">${record.messages.map((message) => `<li>${esc(message)}</li>`).join("")}</ul>` : ""}</div>`;
}

function episodeRow(episode, canManage) {
  const code = `S${String(episode.season).padStart(2, "0")}E${String(episode.episode).padStart(2, "0")}`;
  const context = `${code} ${episode.title || ""}`;
  const state = episode.downloading ? `Downloading ${Math.round(percent(episode.downloading.progress))}%` : episode.has_file ? `On disk${episode.quality ? ` · ${episode.quality}` : ""}` : !episode.aired ? "Not aired" : episode.monitored === false ? "Not monitored" : "Missing";
  const tone = episode.downloading ? "coming" : episode.has_file ? "available" : episode.aired && episode.monitored !== false ? "missing" : "neutral";
  const url = episode.url ? safeUrl(episode.url) : "";
  const actionable = canManage && episode.sonarr_episode_id != null;
  return `<li class="episode-row"><span class="episode-number">${esc(code)}</span><div class="episode-copy"><h4>${esc(episode.title || "Untitled episode")}</h4><p class="hint">${esc(episode.air_date || "Air date to be announced")}${episode.size ? ` · ${sizeText(episode.size)}` : ""}</p><div class="episode-flags"><span class="state-chip state-${tone}">${esc(state)}</span>${episode.watched ? '<span class="watched-mark">✓ Watched</span>' : episode.position > 0 ? `<span class="hint">Resume at ${timestamp(episode.position)}</span>` : ""}</div>${episode.downloading ? downloadStatus(episode.downloading, `${context} download`) : ""}${episode.overview ? `<details class="episode-overview"><summary>Synopsis</summary><p>${esc(episode.overview)}</p></details>` : ""}</div><div class="episode-actions">${episode.jellyfin_id ? playButton("video", episode.jellyfin_id, "Play", context) : ""}${url ? `<a class="secondary-button live-button" href="${esc(url)}" target="_blank" rel="noopener noreferrer" aria-label="Play ${esc(context)} in Jellyfin (opens in a new tab)">Open in Jellyfin ↗</a>` : ""}${actionable ? actionButton("search_episodes", { episode_ids: [episode.sonarr_episode_id] }, "Search", { context }) + monitorButton("monitor_episodes", { episode_ids: [episode.sonarr_episode_id] }, episode.monitored, context) : ""}</div></li>`;
}

function showLive(data) {
  const canManage = canRequest() && data.source === "sonarr" && data.series_id != null;
  const seasons = [...(data.seasons || [])].sort((a, b) => (a.season === 0 ? Infinity : a.season) - (b.season === 0 ? Infinity : b.season));
  return `<div class="section-title"><h3>Episodes</h3><span>${esc(data.source === "sonarr" ? "Sonarr + Jellyfin" : "Jellyfin")}</span></div><div class="live-toolbar"><p class="hint">${countText(data.have)} on disk · ${countText(data.total)} total${typeof data.monitored === "boolean" ? ` · Series ${data.monitored ? "monitored" : "not monitored"}` : ""}</p>${canManage ? `<div class="live-controls">${actionButton("search_missing", { series_id: data.series_id }, "Search all missing")}${searchHarderButton({ series_id: data.series_id }, "All seasons")}</div>` : canRequest() ? '<p class="hint">Search and monitoring controls appear for shows tracked in Sonarr.</p>' : ""}</div><div class="seasons">${seasons.map((season, index) => {
    const title = season.season === 0 ? "Specials" : `Season ${season.season}`;
    return `<details class="season-panel" data-disclosure="season-${esc(season.season)}"${index === 0 && season.season !== 0 ? " open" : ""}><summary><strong>${esc(title)}</strong><span>${countText(season.have)}/${countText(season.aired)} aired · ${countText(season.missing)} missing · ${countText(season.watched)} watched</span></summary><div class="season-tools"><span class="hint">${countText(season.total)} episodes${typeof season.monitored === "boolean" ? ` · ${season.monitored ? "Monitored" : "Not monitored"}` : ""}</span><div class="live-controls">${canManage ? monitorButton("monitor_season", { series_id: data.series_id, season: season.season }, season.monitored, title) + actionButton("search_season", { series_id: data.series_id, season: season.season }, "Search missing", { context: title }) + (season.missing > 0 ? searchHarderButton({ series_id: data.series_id, season: season.season }, title) : "") : ""}</div></div><ol class="episode-list">${(season.episodes || []).map((episode) => episodeRow(episode, canManage)).join("") || '<li class="hint empty-live">No episode details available.</li>'}</ol></details>`;
  }).join("") || '<p class="hint">No seasons available yet.</p>'}</div>`;
}

function movieLive(data) {
  const canManage = canRequest() && data.radarr_id != null;
  const playback = data.watched ? "✓ Watched" : data.position > 0 ? `Continue at ${timestamp(data.position)}${data.runtime ? ` of ${timestamp(data.runtime)}` : ""}` : "Not watched";
  return `<div class="section-title"><h3>Movie file</h3><span class="state-chip state-${data.has_file ? "available" : "neutral"}">${data.has_file ? "On disk" : data.available === false ? "Not yet available" : "Missing"}</span></div><dl class="file-facts">${[["Quality", data.quality], ["Size", data.has_file ? sizeText(data.size) : null], ["Video", data.video], ["Audio", data.audio]].map(([label, value]) => `<div><dt>${label}</dt><dd>${esc(value || "—")}</dd></div>`).join("")}</dl><div class="movie-playback"><p>${esc(playback)}</p>${data.runtime > 0 ? progressBar(data.watched ? 100 : data.position / data.runtime * 100, "Movie watched") : ""}</div><div class="live-controls">${canManage ? monitorButton("monitor_movie", { movie_id: data.radarr_id }, data.monitored, "Movie") + actionButton("search_movie", { movie_id: data.radarr_id }, data.has_file ? "Search for better" : "Search") + (!data.has_file ? searchHarderButton({ movie_id: data.radarr_id }, "Movie") : "") : canRequest() ? '<p class="hint">Search and monitoring controls appear for movies tracked in Radarr.</p>' : ""}</div>${(data.downloading || []).map((record) => downloadStatus(record, "Movie download")).join("")}`;
}

function bookLive(data) {
  const positions = [...(data.positions || [])].sort((a, b) => String(b.updated || "").localeCompare(String(a.updated || "")));
  return `<div class="section-title"><h3>Where you are</h3><span>Most recent first</span></div><div class="reading-positions">${positions.map((position) => `<div class="reading-position"><div class="progress-caption"><strong>${esc(position.app || position.client)}</strong><span>${percent(position.percent).toFixed(1)}%</span></div>${progressBar(position.percent, `${position.app || position.client} reading progress`)}<p class="hint">${position.updated ? `Updated ${esc(position.updated)}` : "Update time unavailable"}</p></div>`).join("") || '<p class="hint">No reading positions reported yet.</p>'}</div>${data.chapters?.length ? `<details class="chapter-panel" data-disclosure="chapters"><summary>Chapters <span>${data.chapters.length} · Audiobookshelf</span></summary><ol class="chapter-list">${data.chapters.map((chapter) => `<li><span>${esc(chapter.title || "Untitled chapter")}</span><span>${timestamp(chapter.start)} – ${timestamp(chapter.end)}</span></li>`).join("")}</ol></details>` : '<p class="hint">No Audiobookshelf chapters reported.</p>'}`;
}

async function loadLive(root, id, signal, force = false) {
  const content = $("[data-live-content]", root);
  const header = $("[data-live-header]", $("#detail-body"));
  const current = () => root.isConnected && !signal.aborted;
  const disclosures = new Map($$("[data-disclosure]", content).map((node) => [node.dataset.disclosure, node.open]));
  const focusKey = content.contains(document.activeElement) ? document.activeElement.dataset.liveAction : null;
  const focusIndex = $$("[data-live-action]", content).indexOf(document.activeElement);
  content.setAttribute("aria-busy", "true");
  try {
    const cached = liveCache.get(String(id));
    const data = !force && cached && Date.now() - cached.time < 30000 ? cached.data : await api(`api/work/${encodeURIComponent(id)}/live`, { signal, cache: "no-store" });
    if (!current()) return;
    liveCache.set(String(id), { data, time: Date.now() });
    if (liveCache.size > 100) liveCache.delete(liveCache.keys().next().value);
    content.innerHTML = data.kind === "show" ? showLive(data) : data.kind === "movie" ? movieLive(data) : data.kind === "book" ? bookLive(data) : '<p class="hint">Live details are not available for this work.</p>';
    void updatePlaybackLive(data, signal);
    if (header) header.textContent = data.kind === "show" ? [`${countText(data.have)} of ${countText(data.aired)} aired episodes`, sizeText(data.size), data.status ? data.status.charAt(0).toUpperCase() + data.status.slice(1) : "", data.network].filter(Boolean).join(" · ") : "";
    $$("[data-disclosure]", content).forEach((node) => { if (disclosures.has(node.dataset.disclosure)) node.open = disclosures.get(node.dataset.disclosure); });
    $$("[data-live-action]", content).forEach((button) => button.addEventListener("click", () => performLiveAction(root, id, signal, button)));
    if (focusKey) {
      const controls = $$("[data-live-action]", content);
      (controls.find((button) => button.dataset.liveAction === focusKey) || controls[focusIndex])?.focus({ preventScroll: true });
    }
  } catch (error) {
    if (!current() || error.message === "login") return;
    const message = `<p class="hint live-error" role="status">Live details are temporarily unavailable. ${esc(error.message)}</p><button type="button" class="text-button" data-live-retry>Retry live details</button>`;
    // Keep previously loaded controls and season state if a refresh fails.
    $(".live-load-error", content)?.remove();
    if (!$("[data-live-action], .reading-positions, .seasons, .file-facts", content)) content.replaceChildren();
    content.insertAdjacentHTML("afterbegin", `<div class="live-load-error">${message}</div>`);
    $("[data-live-retry]", content).addEventListener("click", () => loadLive(root, id, signal, true));
  } finally { if (current()) content.setAttribute("aria-busy", "false"); }
}

async function performLiveAction(root, id, signal, button) {
  if (root.dataset.pending === "true") return;
  const payload = JSON.parse(button.dataset.liveAction);
  if (payload.monitored === false && !confirm(`Stop monitoring ${button.getAttribute("aria-label")?.split(" — ")[1] || "this item"}? Automatic downloads will stop for this selection.`)) return;
  root.dataset.pending = "true";
  const message = $(".live-action-message", root);
  const search = payload.action.startsWith("search_");
  const harder = payload.action === "search_harder";
  const label = button.textContent;
  message.textContent = harder ? "Searching all indexers… (up to a minute)" : search ? "Starting search…" : "Updating monitoring…";
  if (harder) button.textContent = message.textContent;
  const buttons = $$("[data-live-action], [data-live-retry]", root);
  buttons.forEach((node) => { node.disabled = true; });
  try {
    const result = await api("api/action", { method: "POST", body: JSON.stringify(payload) });
    if (!result.ok) throw new Error(result.detail || "The action could not be completed.");
    if (root.isConnected && !signal.aborted) message.textContent = harder ? searchHarderResult(result.result) : search ? "Searching… check Activity" : "Monitoring updated.";
  } catch (error) {
    if (root.isConnected && !signal.aborted && error.message !== "login") message.textContent = `Couldn’t complete action: ${error.message}`;
  } finally {
    liveCache.delete(String(id));
    if (root.isConnected && !signal.aborted) await loadLive(root, id, signal, true);
    root.dataset.pending = "false";
    button.textContent = label;
    buttons.forEach((node) => { node.disabled = false; });
  }
}

let detailController;
async function openWork(id) {
  if (privateMode && !privateUnlocked()) { leavePrivate("Private collection locked."); return; }
  const wasPrivate = privateMode;
  const epoch = viewEpoch;
  detailController?.abort();
  const controller = new AbortController();
  detailController = controller;
  const { signal } = controller;
  const detail = $("#detail");
  $("#detail-body").innerHTML = `<div class="detail-loading">${skeletons(3)}</div>`;
  if (!detail.open) detail.showModal();
  try {
    const work = await api(`api/work/${encodeURIComponent(id)}`, { signal, privateRequest: wasPrivate, cache: "no-store" });
    if (signal.aborted || !detail.open || epoch !== viewEpoch) return;
    if (wasPrivate && !privateUnlocked()) { leavePrivate("Private collection locked."); return; }
    if (!work || !allowedWork(work)) throw new Error("This work is not available in this section.");
    const requests = wasPrivate ? null : api(`api/work/${encodeURIComponent(id)}/requests`, { signal }).then((data) => ({ data }), (error) => ({ error }));
    const meta = [
      work.authors?.length ? `${work.kind === "scene" ? "Performers: " : work.kind === "game" ? "Companies: " : ""}${work.authors.join(", ")}` : "",
      ["game", "scene"].includes(work.kind) && work.libraries?.length ? `${work.kind === "game" ? "Platform" : "Studio"}: ${work.libraries.join(", ")}` : "",
      work.year,
      hours(work.duration),
      work.rating ? `★ ${Number(work.rating).toFixed(1)}` : "",
      work.series ? `${work.series}${work.series_index != null ? ` #${Number(work.series_index)}` : ""}` : "",
    ].filter(Boolean);
    const allVisibleEditions = (work.editions || []).filter((edition) => !edition.hidden);
    const visibleEditions = allVisibleEditions.filter((edition) => safeUrl(edition.url));
    const actions = visibleEditions.map((edition) => {
      const [defaultVerb, defaultApp] = FORMAT[edition.format] || ["Open", SOURCE[edition.source] || edition.source || "App"];
      const appName = SOURCE[edition.source] || defaultApp;
      const verb = ["jellyfin", "abs"].includes(edition.source) ? `Open in ${appName}` : defaultVerb;
      const progress = edition.finished ? "Finished" : edition.progress != null && edition.progress > 0 ? `${Math.round(edition.progress * 100)}% complete` : "";
      const details = [appName, edition.library && edition.library !== appName ? edition.library : "", progress, hours(edition.duration)].filter(Boolean).join(" · ");
      const split = (work.editions || []).length > 1 && work.kind === "book" ? `<button class="split-button" type="button" data-split="${esc(edition.key)}">Not this book?</button>` : "";
      return `<div class="edition-wrap"><a class="edition-action f-${esc(edition.format)}" href="${esc(safeUrl(edition.url))}" target="_blank" rel="noopener noreferrer"><span class="edition-icon" aria-hidden="true">${esc(verb.charAt(0))}</span><span class="edition-copy"><strong>${esc(verb)}</strong><small>${esc(details)}</small></span><span class="edition-arrow" aria-hidden="true">↗</span></a>${split}</div>`;
    }).join("");

    const unlinkedSplits = work.kind === "book" && (work.editions || []).length > 1 ? allVisibleEditions
      .filter((edition) => !safeUrl(edition.url))
      .map((edition) => `<button class="unlinked-split" type="button" data-split="${esc(edition.key)}">Not this book? <span>${esc(BADGE[edition.format] || edition.format || edition.source)}</span></button>`).join("") : "";
    const genres = (work.genres || []).map((genre) => `<button class="detail-tag" type="button" data-genre="${esc(genre)}">${esc(genre)}</button>`).join("");
    const tags = (work.tags || []).filter((tag) => !(work.genres || []).includes(tag)).slice(0, 8).map((tag) => `<span class="detail-tag passive">${esc(tag)}</span>`).join("");
    const narrators = work.narrators?.length ? `<p class="narrators">Narrated by <strong>${esc(work.narrators.join(", "))}</strong></p>` : "";
    const universeTitle = work.universe ? `Part of the ${work.universe}` : "Universe reading order";

    $("#detail-body").innerHTML = `<button class="dialog-close icon-button" type="button" aria-label="Close details"><svg aria-hidden="true" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg></button>
      <article class="detail-view${work.kind === "game" ? " game-detail" : ""}">
        <div class="detail-lead">
          <div class="detail-cover"><span class="cover-placeholder"><span class="placeholder-title">${esc(work.title)}</span><span class="placeholder-author">${esc(work.authors?.[0] || work.kind)}</span></span>${work.cover ? `<img class="cover-image" src="api/cover/${encodeURIComponent(work.cover)}" alt="Cover of ${esc(work.title)}">` : ""}</div>
          <div class="detail-intro"><div class="eyebrow">${esc(work.kind || "Work")}${work.universe ? ` · ${esc(work.universe)}` : ""}</div><h2>${esc(work.title)}</h2><div class="detail-meta">${meta.map((item) => `<span>${esc(item)}</span>`).join("")}</div>${narrators}<div class="detail-tags">${genres}${tags}</div><div class="detail-playback">${wasPrivate ? "" : playbackActions(work)}</div><p class="live-header hint" data-live-header role="status"></p></div>
        </div>
        <div class="detail-content">
          ${!wasPrivate && ["show", "movie", "book"].includes(work.kind) ? '<section class="detail-section live-section" aria-label="Live library details"><p class="live-action-message hint" role="status" aria-live="polite" aria-atomic="true"></p><div data-live-content aria-busy="true">' + liveSkeleton() + '</div></section>' : ""}
          <section class="detail-section actions-section"><div class="section-title"><h3>Open in</h3><span>${visibleEditions.length}</span></div><div class="edition-grid">${actions || '<p class="hint">No app links are configured for this work.</p>'}</div>${unlinkedSplits ? `<div class="unlinked-splits">${unlinkedSplits}</div>` : ""}</section>
          ${downloadSection(work)}
          ${work.description ? `<section class="detail-section"><div class="section-title"><h3>About</h3></div><div class="description">${esc(work.description)}</div></section>` : ""}
          ${miniSection(work.series ? `More in ${work.series}` : "Series", work.series_works, work.id, "series-section")}
          ${miniSection(universeTitle, work.universe_works, work.id, "universe-section")}
          ${miniSection(work.kind === "book" ? "On screen" : "Based on", work.related, work.id, "related-section")}
          ${wasPrivate ? "" : '<section class="detail-section request-section"><div class="section-title"><h3>Get more</h3></div><p class="hint" data-request-status aria-live="polite" aria-atomic="true">Finding request options…</p><div data-request-content></div></section>'}
        </div>
      </article>`;
    bindCards($("#detail-body"));
    bindPlayback($(".detail-playback", $("#detail-body")));
    bindReading($(".detail-playback", $("#detail-body")), signal);
    const liveRoot = $(".live-section", $("#detail-body"));
    if (liveRoot) loadLive(liveRoot, id, signal);
    if (requests) loadWorkRequests($(".request-section", $("#detail-body")), id, requests, signal, work);
    $(".dialog-close", $("#detail-body")).addEventListener("click", () => detail.close());
    $$("[data-genre]", $("#detail-body")).forEach((button) => button.addEventListener("click", () => { detail.close(); setParam("genre", button.dataset.genre); }));
    $$("[data-split]", $("#detail-body")).forEach((button) => button.addEventListener("click", async () => {
      if (!confirm("Show this edition as a separate item? It won’t be automatically matched again.")) return;
      button.disabled = true;
      await api("api/override", { method: "POST", body: JSON.stringify({ a: button.dataset.split, action: "split" }) });
      button.textContent = "Separated — refreshing…";
      setTimeout(() => { detail.close(); run(); }, 2200);
    }));
    $(".dialog-close", $("#detail-body")).focus();
  } catch (error) {
    if (signal.aborted || !detail.open || epoch !== viewEpoch) return;
    if (wasPrivate) {
      await checkAdultStatus();
      if (signal.aborted || !detail.open || epoch !== viewEpoch) return;
    }
    if (error.message !== "login") $("#detail-body").innerHTML = `<button class="dialog-close icon-button" type="button" aria-label="Close details">×</button><div class="empty-state"><h2>Couldn’t open this work</h2><p>${esc(error.message)}</p></div>`;
    const close = $(".dialog-close", $("#detail-body"));
    if (close) close.addEventListener("click", () => detail.close());
  }
}

$("#detail").addEventListener("click", (event) => { if (event.target === $("#detail")) $("#detail").close(); });
$("#detail").addEventListener("close", () => { if (!$("#detail").open) detailController?.abort(); });

async function refreshStatus() {
  try {
    const status = await api("api/status");
    const index = status.index;
    if (!index) $("#index-status").textContent = "Building the library…";
    else {
      const errors = Object.keys(index.errors || {}).filter((source) => source !== "stash");
      // Global index totals may include private works; scoped search supplies counts.
      $("#index-status").textContent = `Updated ${index.built_at}${errors.length ? ` · Check ${errors.join(", ")}` : ""}`;
    }
    if (!index || status.indexing) setTimeout(() => { refreshStatus(); run(); }, 5000);
  } catch { /* A 401 already returns to sign-in. */ }
}

let activityData = null;
let activityController;
let activityTimer;
let activityUpdated = 0;
let approvalData = null;
let approvalError = "";
const approvalPending = new Set();
const approvalNotes = new Map();
const approvalMessages = new Map();
const wantedPending = new Set();
const wantedMessages = new Map();

function activityVisible() {
  return !$("#app").hidden && !privateMode && params.get("view") === "activity" && !document.hidden;
}

function stopActivity() {
  clearInterval(activityTimer);
  activityController?.abort();
  activityController = null;
}

function startActivity() {
  if (!activityVisible()) return;
  if (!activityData) $("#activity-body").innerHTML = liveSkeleton();
  refreshActivity();
  activityTimer = setInterval(() => { if (activityVisible()) refreshActivity(); }, 20000);
}

// ROMarr and Shelfmark may wrap their collections or key them by download ID.
function activityItems(value) {
  if (!value || value.error) return [];
  if (Array.isArray(value)) return value;
  if (typeof value !== "object") return [];
  for (const key of ["records", "items", "results", "active_downloads", "downloads", "queue", "wanted", "games", "data"]) {
    if (value[key] && typeof value[key] === "object") return activityItems(value[key]);
  }
  if (value.title || value.name || value.status || value.stage) return [value];
  return Object.entries(value).filter(([, item]) => item && typeof item === "object").flatMap(([key, item]) => Array.isArray(item) ? item : [{ ...item, _key: key }]);
}

function activeItems(value) {
  return activityItems(value).filter((item) => !/^(completed|complete|finished|failed|declined|cancelled|canceled|aligned)$/i.test(item.status || ""));
}

function updateActivityBadge() {
  const badge = $("#activity-badge");
  if (!badge) return;
  let active = 0;
  if (activityData) {
    for (const source of ["sonarr", "radarr"]) {
      const queue = activityData[source];
      if (!queue?.error) active += Math.max(0, Number(queue?.total ?? queue?.records?.length) || 0) - (queue?.records || []).filter((record) => /^completed$/i.test(record.status)).length;
    }
    active += activeItems(activityData.games?.queue).length + activeItems(activityData.books).length + activeItems(activityData.readalongs).length;
    active += activityItems(activityData.requests).filter((request) => ["pending approval", "approved"].includes(request.status) && request.media !== "available").length;
  }
  active = Math.max(0, active);
  badge.hidden = !active;
  badge.textContent = countText(active);
  badge.setAttribute("aria-label", `${countText(active)} active items reported`);
  let pendingBadge = $("#requests-badge");
  if (!pendingBadge) {
    pendingBadge = document.createElement("span");
    pendingBadge.id = "requests-badge";
    pendingBadge.className = "activity-badge";
    badge.after(pendingBadge);
  }
  const pending = Math.max(0, Number(approvalData?.pending) || 0);
  pendingBadge.hidden = !isAdmin() || !pending;
  pendingBadge.textContent = `${countText(pending)} pending`;
  pendingBadge.setAttribute("aria-label", `${countText(pending)} requests awaiting approval`);
}

function approvalTime(value) {
  if (value == null || value === "") return "Date unavailable";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function approvalQueue() {
  const error = approvalError ? `<p class="service-error" role="status">${esc(approvalError)} Use Refresh to try again.${approvalData ? " Showing the last requests update." : ""}</p>` : "";
  if (!approvalData) return error || '<p class="hint">Loading requests…</p>';
  const requests = [...(approvalData.requests || [])].sort((a, b) => Number(b.status === "pending") - Number(a.status === "pending"));
  if (!requests.length) return `${error}<p class="hint">${isAdmin() ? "No requests for approval yet." : "You haven’t asked for anything yet."}</p>`;
  return `${error}<ul class="activity-list approval-list">${requests.map((request) => {
    const id = String(request.id);
    const title = request.title || "Untitled request";
    const state = ["pending", "approved", "denied", "failed"].includes(request.status) ? request.status : "unknown";
    const tone = { pending: "state-coming", approved: "state-available", denied: "state-missing", failed: "state-missing" }[state] || "state-neutral";
    const kind = { screen: "Movie / TV", game: "Game", book_download: "Book download", wanted: "Book / comic" }[request.kind] || request.kind;
    const disabled = approvalPending.has(id) ? " disabled" : "";
    const person = (value) => typeof value === "object" && value !== null ? value.username || value.id || "" : value;
    return `<li class="approval-row" data-approval-id="${esc(id)}" tabindex="-1"><div class="approval-copy"><strong>${esc(title)}</strong><p class="hint">${esc(kind)} · ${esc(approvalTime(request.created))}${request.by != null ? ` · Asked by ${esc(person(request.by))}` : ""}</p><span class="state-chip ${tone}">${esc(state)}</span>${request.decided_by != null || request.decided_at != null ? `<p class="hint">${request.decided_by != null ? `Decided by ${esc(person(request.decided_by))}` : "Decided"}${request.decided_at != null ? ` · ${esc(approvalTime(request.decided_at))}` : ""}</p>` : ""}${request.note ? `<p class="hint approval-note">${esc(request.note)}</p>` : ""}</div>
      ${state === "pending" ? isAdmin() ? `<form class="approval-actions" aria-label="Review ${esc(title)}"><button class="primary-button" type="button" data-approval-action="approve" aria-label="Approve ${esc(title)}"${disabled}>Approve</button><label class="field">Denial note (optional)<input name="note" data-approval-focus="note" value="${esc(approvalNotes.get(id) || "")}" autocomplete="off"${disabled}></label><button class="secondary-button" type="submit" data-approval-action="deny" aria-label="Deny ${esc(title)}"${disabled}>Deny</button></form>` : `<div class="approval-actions"><button class="text-button" type="button" data-approval-action="withdraw" aria-label="Withdraw ${esc(title)}"${disabled}>Withdraw</button></div>` : ""}
      <p class="hint request-message" role="status" tabindex="-1" data-approval-focus="message">${esc(approvalMessages.get(id) || "")}</p></li>`;
  }).join("")}</ul>`;
}

function bindApprovalQueue(root) {
  $$("[data-approval-id]", root).forEach((row) => {
    const id = row.dataset.approvalId;
    const note = $('[name="note"]', row);
    note?.addEventListener("input", () => approvalNotes.set(id, note.value));
    $("form", row)?.addEventListener("submit", (event) => { event.preventDefault(); decide("deny"); });
    $$("[data-approval-action]", row).filter((button) => button.type !== "submit").forEach((button) => button.addEventListener("click", () => decide(button.dataset.approvalAction)));
    async function decide(action) {
      if (approvalPending.has(id) || !activityVisible() || (action !== "withdraw" && !isAdmin())) return;
      const epoch = viewEpoch;
      const hadFocus = row.contains(document.activeElement);
      const message = $('[role="status"]', row);
      approvalPending.add(id);
      activityController?.abort();
      activityController = null;
      $$("button, input", row).forEach((control) => { control.disabled = true; });
      message.textContent = action === "withdraw" ? "Withdrawing…" : "Saving decision…";
      approvalMessages.set(id, message.textContent);
      if (hadFocus) message.focus();
      try {
        const result = await api(`api/requests/${encodeURIComponent(id)}${action === "withdraw" ? "" : `/${action}`}`, {
          method: action === "withdraw" ? "DELETE" : "POST",
          ...(action === "deny" ? { body: JSON.stringify({ note: note.value.trim() }) } : {}),
        });
        if (epoch !== viewEpoch) return;
        if (result.ok === false) throw new Error(result.message || "The request could not be updated.");
        approvalMessages.set(id, result.message || (action === "withdraw" ? "Request withdrawn." : action === "approve" ? "Request approved." : "Request denied."));
        approvalNotes.delete(id);
        if (approvalData) {
          const request = approvalData.requests.find((item) => String(item.id) === id);
          if (request?.status === "pending") approvalData.pending = Math.max(0, approvalData.pending - 1);
          if (action === "withdraw") approvalData.requests = approvalData.requests.filter((item) => String(item.id) !== id);
          else if (request) Object.assign(request, { status: result.status || (action === "approve" ? "approved" : "denied"), note: action === "deny" ? note.value.trim() : request.note, decided_by: currentUser?.username, decided_at: Date.now() / 1000 });
        }
        if (action === "withdraw" && activityVisible()) $("#activity-status").textContent = approvalMessages.get(id);
      } catch (error) {
        if (epoch === viewEpoch && error.message !== "login") approvalMessages.set(id, error.message);
      } finally {
        approvalPending.delete(id);
        if (epoch === viewEpoch) {
          updateActivityBadge();
          if (activityVisible()) renderActivity(activityData || {});
          refreshActivity();
        }
      }
    }
  });
}

function serviceError(value, name) {
  if (value?.error) return `<p class="service-error" role="status">${esc(name)} unavailable: ${esc(value.error)}</p>`;
  if (value == null) return `<p class="hint">${esc(name)} is not connected or did not report activity.</p>`;
  return "";
}

function activitySection(title, app, body) {
  return `<section class="activity-section"><div class="section-title"><h2>${esc(title)}</h2><span>${esc(app)}</span></div>${body}</section>`;
}

function downloadGroups(data) {
  return ["sonarr", "radarr"].map((source) => {
    const queue = data[source];
    const name = source === "sonarr" ? "Sonarr" : "Radarr";
    const error = serviceError(queue, name);
    if (error) return error;
    const records = queue.records || [];
    const total = Number(queue.total ?? records.length) || 0;
    const groups = new Map();
    for (const record of records) {
      const group = record.item || record.title || "Untitled download";
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(record);
    }
    return `<div class="download-service"><h3>${name} <span class="hint">${countText(total)} in queue</span></h3>${total > records.length ? `<p class="hint">Showing ${countText(records.length)} of ${countText(total)} queue entries reported by ${name}.</p>` : ""}${[...groups].map(([title, items]) => {
      const warnings = items.filter((item) => /warning|error/i.test(item.tracked || "") || /warning|error|failed/i.test(item.status || "")).length;
      const average = items.reduce((sum, item) => sum + percent(item.progress), 0) / items.length;
      return `<details class="download-group${warnings ? " has-warning" : ""}" data-disclosure="${esc(`${source}:${title}`)}"${items.length <= 3 ? " open" : ""}><summary><strong>${countText(items.length)} ${esc(title)}${source === "sonarr" ? ` episode${items.length === 1 ? "" : "s"}` : ` download${items.length === 1 ? "" : "s"}`}</strong><span>${Math.round(average)}% average${warnings ? ` · ${warnings} need attention` : ""}</span></summary><div class="download-records">${items.map((record) => `<article class="download-record"><h4>${esc(record.episode || record.item || "Movie")}</h4><p class="release-name">${esc(record.title)}</p>${downloadStatus(record, `${title} ${record.episode || ""} download`)}</article>`).join("")}</div></details>`;
    }).join("") || '<p class="hint">No active downloads.</p>'}</div>`;
  }).join("");
}

function requestActivity(value) {
  const error = serviceError(value, "Seerr");
  if (error) return error;
  return `<ul class="activity-list">${activityItems(value).map((request) => {
    const type = request.type === "tv" ? "TV" : "Movie";
    const tmdb = /^\d+$/.test(String(request.tmdb)) ? `https://www.themoviedb.org/${request.type === "tv" ? "tv" : "movie"}/${request.tmdb}` : "";
    return `<li class="request-activity-row"><div><strong>${esc(request.title || `${type} · TMDB ${request.tmdb ?? "unknown"}`)}</strong><p class="hint">Requested ${esc(request.created || "date unavailable")}${tmdb ? ` · <a href="${esc(tmdb)}" target="_blank" rel="noopener noreferrer" aria-label="View ${esc(type)} ${esc(request.tmdb)} on TMDB (opens in a new tab)">Details ↗</a>` : ""}</p></div><div class="request-states"><span class="state-chip${["failed", "declined"].includes(request.status) ? " state-missing" : ""}">${esc(request.status || "Unknown status")}</span><span class="hint">Media: ${esc(request.media || "Unknown")}</span></div></li>`;
  }).join("") || '<li class="hint">No recent requests.</li>'}</ul>`;
}

function genericActivity(value, name, empty) {
  const error = serviceError(value, name);
  if (error) return error;
  const items = activityItems(value);
  const rows = items.map((item) => {
    if (typeof item !== "object" || item == null) return `<li>${esc(item)}</li>`;
    const title = item.title || item.name || item.book_title || item.game?.title || item.game?.name || item.book?.title || item.filename || item._key || "Untitled item";
    const status = item.status || item.state || "";
    const stage = item.stage || item.current_stage || "";
    const progress = item.progress ?? item.percent ?? item.percentage;
    return `<li><div class="progress-caption"><strong>${esc(title)}</strong><span>${esc(status)}</span></div>${stage ? `<p class="hint">${esc(stage)}</p>` : ""}${progress != null && Number.isFinite(Number(progress)) ? `<div class="progress-caption"><span class="hint">Progress</span><span>${Math.round(percent(progress))}%</span></div>${progressBar(progress, `${title} progress`)}` : ""}${item.error || item.message ? `<p class="${item.error ? "service-error" : "hint"}">${esc(item.error || item.message)}</p>` : ""}</li>`;
  }).join("");
  if (!items.length) {
    // Keep useful summary counts visible when a service provides no item list.
    const counts = Object.entries(value || {}).filter(([, item]) => typeof item === "number").map(([key, count]) => `${key.replaceAll("_", " ")}: ${countText(count)}`).join(" · ");
    return `<p class="hint">${esc(counts || empty)}</p>`;
  }
  return items.length > 6 ? `<details class="activity-overflow" data-disclosure="${esc(name)}"><summary>${items.length} ${esc(name.toLowerCase())} items</summary><ul class="activity-list">${rows}</ul></details>` : `<ul class="activity-list">${rows}</ul>`;
}

function wantedActivity(items) {
  return `${canSubmitRequest() ? "" : requestHint()}<ul class="activity-list wanted-list">${(items || []).map((item) => {
    const id = String(item.id);
    const context = `${item.title || "Untitled book"} (${item.format})`;
    const disabled = wantedPending.has(id) ? " disabled" : "";
    return `<li class="wanted-row" data-wanted-id="${esc(id)}"><div class="wanted-copy"><strong>${esc(item.title || "Untitled book")}</strong>${item.author ? `<p class="hint">${esc(item.author)}</p>` : ""}${wantedStatus(item)}<p class="hint request-message" role="status" aria-atomic="true">${esc(wantedMessages.get(id) || "")}</p></div>${canRequest() ? `<div class="wanted-actions"><button class="secondary-button live-button" type="button" data-wanted-action="search" aria-label="Search now for ${esc(context)}"${disabled}>Search now</button><button class="text-button live-button" type="button" data-wanted-action="stop" aria-label="Stop looking for ${esc(context)}"${disabled}>Stop looking</button></div>` : ""}</li>`;
  }).join("") || '<li class="hint">No books on the wanted list.</li>'}</ul>`;
}

function bindWantedActivity(root, items) {
  $$("[data-wanted-action]", root).forEach((button) => button.addEventListener("click", async () => {
    const row = button.closest("[data-wanted-id]");
    const id = row.dataset.wantedId;
    if (wantedPending.has(id)) return;
    const item = (items || []).find((item) => String(item.id) === id);
    if (!item) return;
    const stop = button.dataset.wantedAction === "stop";
    if (stop && !confirm(`Stop looking for “${item.title}” (${item.format})?`)) return;
    const epoch = viewEpoch;
    wantedPending.add(id);
    activityController?.abort();
    activityController = null;
    wantedMessages.set(id, stop ? "Stopping…" : "Requesting a new search…");
    $$("button", row).forEach((control) => { control.disabled = true; });
    $("[role=status]", row).textContent = wantedMessages.get(id);
    try {
      await api(`api/wanted/${encodeURIComponent(id)}${stop ? "" : "/search"}`, { method: stop ? "DELETE" : "POST" });
      wantedMessages.set(id, stop ? "Stopped looking." : "Search requested. Omnarr is looking again.");
      if (epoch !== viewEpoch || !activityVisible()) return;
      if (stop && activityData) {
        activityData.wanted_books = (activityData.wanted_books || []).filter((item) => String(item.id) !== id);
      }
    } catch (error) {
      wantedMessages.set(id, error.message);
    } finally {
      wantedPending.delete(id);
      if (epoch === viewEpoch && activityVisible()) {
        if (activityData) renderActivity(activityData);
        activityController?.abort();
        activityController = null;
        refreshActivity();
      }
    }
  }));
}

function renderActivity(data) {
  const root = $("#activity-body");
  const active = document.activeElement;
  const focusedApproval = active.closest?.("[data-approval-id]")?.dataset.approvalId;
  const approvalFocus = active.dataset?.approvalFocus || active.dataset?.approvalAction;
  const selection = active.matches('input[name="note"]') ? [active.selectionStart, active.selectionEnd] : null;
  const disclosures = new Map($$("[data-disclosure]", root).map((node) => [node.dataset.disclosure, node.open]));
  const focused = root.contains(document.activeElement) ? document.activeElement.closest("[data-disclosure]")?.dataset.disclosure : null;
  const focusedWanted = document.activeElement.closest?.("[data-wanted-id]")?.dataset.wantedId;
  const focusedAction = document.activeElement.dataset?.wantedAction;
  const games = serviceError(data.games, "ROMarr") || `<h3>Download queue</h3>${genericActivity(data.games?.queue, "Game queue", "No games downloading.")}<details class="activity-overflow" data-disclosure="wanted-games"><summary>Wanted games</summary>${genericActivity(data.games?.wanted, "Wanted games", "No missing games on the wanted list.")}</details>`;
  const logs = serviceError(data.logs, "Logs") || [["readalong_linker", "Read-along linker"], ["stash_identify", "Stash identify"]].map(([key, label]) => `<h3>${label}</h3><pre class="log-tail">${esc(Array.isArray(data.logs?.[key]) && data.logs[key].length ? data.logs[key].join("\n") : "No log lines reported.")}</pre>`).join("");
  root.innerHTML = activitySection("Requests", isAdmin() ? "Needs approval" : "Your requests", approvalQueue())
    + activitySection("Downloads", "Sonarr + Radarr", downloadGroups(data))
    + activitySection("Movie & TV requests", "Seerr", requestActivity(data.requests))
    + activitySection("Games", "ROMarr", games)
    + activitySection("Books", "Shelfmark", genericActivity(data.books, "Books", "No active book downloads."))
    + activitySection("Books we’re looking for", "Omnarr", wantedActivity(data.wanted_books))
    + activitySection("Read-alongs", "Storyteller processing", genericActivity(data.readalongs, "Read-alongs", "No read-alongs processing."))
    + `<details class="activity-section activity-logs" data-disclosure="logs"><summary>Logs <span>Latest service output</span></summary>${logs}</details>`;
  $$("[data-disclosure]", root).forEach((node) => {
    if (disclosures.has(node.dataset.disclosure)) node.open = disclosures.get(node.dataset.disclosure);
    if (focused === node.dataset.disclosure) $("summary", node)?.focus({ preventScroll: true });
  });
  bindWantedActivity(root, data.wanted_books);
  bindApprovalQueue(root);
  if (focusedApproval) {
    const row = $$("[data-approval-id]", root).find((node) => node.dataset.approvalId === focusedApproval);
    const control = row && $$("[data-approval-focus], [data-approval-action]", row).find((node) => (node.dataset.approvalFocus || node.dataset.approvalAction) === approvalFocus);
    (control && !control.disabled && control.getClientRects().length ? control : row || $("#activity-title")).focus({ preventScroll: true });
    if (selection && control?.matches("input")) control.setSelectionRange(...selection);
  }
  if (focusedWanted) {
    const row = $$("[data-wanted-id]", root).find((row) => row.dataset.wantedId === focusedWanted);
    const button = row && $$("[data-wanted-action]", row).find((button) => button.dataset.wantedAction === focusedAction);
    (button || $("#activity-refresh")).focus({ preventScroll: true });
  }
}

async function refreshActivity(render = true) {
  if ($("#app").hidden || privateMode || document.hidden || activityController || wantedPending.size || approvalPending.size) return;
  const controller = new AbortController();
  activityController = controller;
  const button = $("#activity-refresh");
  button.disabled = true;
  const timeout = setTimeout(() => controller.abort(), 45000);
  try {
    const [activityResult, approvalResult] = await Promise.allSettled([
      api("api/activity", { signal: controller.signal, cache: "no-store" }),
      api("api/requests", { signal: controller.signal, cache: "no-store" }),
    ]);
    if (activityController !== controller) return;
    if (controller.signal.aborted) throw new Error("The request timed out.");
    if (approvalResult.status === "fulfilled") { approvalData = approvalResult.value; approvalError = ""; }
    else approvalError = `Couldn’t refresh requests. ${approvalResult.reason.message}`;
    updateActivityBadge();
    if (activityResult.status === "rejected") {
      if (render && activityVisible()) renderActivity(activityData || {});
      throw activityResult.reason;
    }
    const data = activityResult.value;
    activityData = data;
    activityUpdated = Date.now();
    updateActivityBadge();
    if (render && activityVisible()) {
      renderActivity(data);
      $("#activity-status").textContent = `Updated ${new Date(activityUpdated).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })} · Refreshes every 20 seconds while visible`;
    }
  } catch (error) {
    if (activityController !== controller || error.message === "login") return;
    if (render && activityVisible()) {
      if (!activityData && !approvalData) $("#activity-body").innerHTML = '<p class="hint">Activity could not be loaded. Use Refresh to try again.</p>';
      $("#activity-status").textContent = `Couldn’t refresh Activity. ${controller.signal.aborted ? "The request timed out." : error.message}${activityData ? ` Showing the last update from ${new Date(activityUpdated).toLocaleTimeString()}.` : ""}`;
    }
  } finally {
    clearTimeout(timeout);
    if (activityController === controller) { activityController = null; button.disabled = false; }
  }
}

$("#activity-refresh").addEventListener("click", () => refreshActivity());
document.addEventListener("visibilitychange", () => {
  stopActivity();
  if (activityVisible()) startActivity();
});

boot();
