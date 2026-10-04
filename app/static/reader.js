"use strict";

// Reading owns its modal and lifetime, independently of library navigation.
window.OmnarrReader = (() => {
  const $ = (selector, root = dialog) => root.querySelector(selector);
  const clamp = (value, min, max) => Math.min(max, Math.max(min, Number(value) || 0));
  const preference = (key, fallback, choices) => {
    try { const value = localStorage.getItem(`omnarr.reader.${key}`); return choices.includes(value) ? value : fallback; } catch { return fallback; }
  };
  const savePreference = (key, value) => { try { localStorage.setItem(`omnarr.reader.${key}`, String(value)); } catch { /* Private browsing may disable storage. */ } };
  const darkScheme = matchMedia("(prefers-color-scheme: dark)");
  const prefs = {
    fit: preference("fit", "screen", ["screen", "width"]),
    spread: preference("spread", "auto", ["auto", "single"]),
    direction: preference("direction", "ltr", ["ltr", "rtl"]),
    font: Number(preference("font", "100", Array.from({ length: 9 }, (_, i) => String(80 + i * 10)))),
    theme: preference("theme", "system", ["system", "light", "sepia", "dark"]),
    layout: preference("layout", "paginated", ["paginated", "scrolled-doc"]),
  };
  let current = null;
  let enginePromise;
  let chromeTimer;
  let resizeTimer;
  let returnFocus;
  let bodyStyle;
  const dialog = document.createElement("dialog");
  dialog.id = "omnarr-reader";
  dialog.className = "reader-overlay";
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  dialog.setAttribute("aria-labelledby", "reader-title");
  dialog.innerHTML = `
    <header class="reader-top reader-chrome player-heading">
      <button type="button" class="icon-button" data-reader-close aria-label="Close reader">×</button>
      <div class="reader-heading"><h2 id="reader-title">Loading book…</h2><p id="reader-chapter"></p><small id="reader-offline-status" hidden>Offline — showing saved items</small></div>
      <button type="button" class="secondary-button" id="reader-toc-toggle" aria-controls="reader-toc" aria-expanded="false" hidden>Contents</button>
      <button type="button" class="icon-button" id="reader-settings-toggle" aria-label="Reading settings" aria-controls="reader-settings" aria-expanded="false">⚙</button>
    </header>
    <div id="reader-stage" class="reader-stage" tabindex="0" aria-label="Reading area"></div>
    <p id="reader-message" class="reader-message" role="status" aria-live="polite"></p>
    <section id="reader-end" class="reader-end reader-chrome" aria-label="Finished book" hidden>
      <strong>Finished</strong><button type="button" class="primary-button" id="reader-next-book" hidden>Next book</button><button type="button" class="secondary-button" data-reader-close>Close</button>
    </section>
    <footer class="reader-bottom reader-chrome">
      <div id="reader-narration" class="reader-narration" hidden>
        <button type="button" class="secondary-button" id="reader-sentence-prev" aria-label="Previous sentence">⏮</button>
        <button type="button" class="primary-button" id="reader-play" aria-label="Play narration">Play</button>
        <button type="button" class="secondary-button" id="reader-sentence-next" aria-label="Next sentence">⏭</button>
        <label class="sr-only" for="reader-speed">Narration speed</label><select id="reader-speed">${Array.from({ length: 6 }, (_, i) => 0.75 + i * 0.25).map((rate) => `<option value="${rate}">${rate}×</option>`).join("")}</select>
        <output id="reader-elapsed" aria-label="Elapsed time in chapter">0:00</output>
      </div>
      <button type="button" class="secondary-button" id="reader-prev" aria-label="Previous page" disabled>Previous</button>
      <div class="reader-position"><label class="sr-only" for="reader-scrubber">Reading position</label><input id="reader-scrubber" type="range" min="1" max="1" step="1" value="1" disabled><output id="reader-position" for="reader-scrubber" aria-live="polite"></output></div>
      <button type="button" class="secondary-button" id="reader-next" aria-label="Next page" disabled>Next</button>
    </footer>
    <section id="reader-settings" class="reader-sheet" aria-labelledby="reader-settings-title" hidden>
      <div class="reader-sheet-heading"><h3 id="reader-settings-title">Reading settings</h3><button type="button" class="icon-button" data-reader-dismiss aria-label="Close settings">×</button></div>
      <div id="reader-comic-settings">
        <label>Fit<select data-preference="fit"><option value="screen">Screen</option><option value="width">Width</option></select></label>
        <label>Spread<select data-preference="spread"><option value="auto">Auto</option><option value="single">Single</option></select></label>
        <label>Direction<select data-preference="direction"><option value="ltr">Left to right</option><option value="rtl">Right to left (manga)</option></select></label>
        <p id="reader-komga-sync" class="hint" hidden>Saved to Komga too</p>
      </div>
      <div id="reader-epub-settings" hidden>
        <div class="reader-font"><span>Font size</span><button type="button" class="secondary-button" id="reader-font-down" aria-label="Decrease font size">−</button><output id="reader-font-size"></output><button type="button" class="secondary-button" id="reader-font-up" aria-label="Increase font size">+</button></div>
        <label>Theme<select data-preference="theme"><option value="system">System</option><option value="light">Light</option><option value="sepia">Sepia</option><option value="dark">Dark</option></select></label>
        <label>Layout<select data-preference="layout"><option value="paginated">Paginated</option><option value="scrolled-doc">Scrolled</option></select></label>
      </div>
      <div id="reader-offline" hidden><button type="button" class="secondary-button" id="reader-save-offline">Save offline</button><p id="reader-offline-note" class="hint" hidden>Read-alongs can't be saved offline yet</p></div>
    </section>
    <nav id="reader-toc" class="reader-sheet reader-toc" aria-labelledby="reader-toc-title" hidden>
      <div class="reader-sheet-heading"><h3 id="reader-toc-title">Contents</h3><button type="button" class="icon-button" data-reader-dismiss aria-label="Close contents">×</button></div><div id="reader-toc-list"></div>
    </nav>`;
  document.body.append(dialog);
  const backToNarration = document.createElement("button");
  backToNarration.type = "button"; backToNarration.className = "reader-back secondary-button";
  backToNarration.textContent = "Back to narration"; backToNarration.hidden = true; dialog.append(backToNarration);
  const stage = $("#reader-stage");
  const slider = $("#reader-scrubber");
  const panels = [$("#reader-settings"), $("#reader-toc")];
  const active = (state) => current === state && !state.closed;
  const readInfo = (key, signal) => window.OmnarrOffline.readInfo(key, signal);
  function message(state, text) { if (active(state)) $("#reader-message").textContent = text; }

  // One queue covers turns, close, hidden tabs, and rapid switches between books.
  // A newer position for a unit replaces its unsent position; requests stay ordered.
  const pending = new Map();
  let reportTimer;
  let reporting = false;
  let lastReport = 0;
  function scheduleReports() {
    clearTimeout(reportTimer);
    if (reporting || !pending.size) return;
    const due = Math.min(...[...pending.values()].map((entry) => entry.due));
    reportTimer = setTimeout(sendReport, Math.max(0, due - Date.now(), lastReport + 1000 - Date.now()));
  }
  async function sendReport() {
    if (reporting || !pending.size) return;
    const [key, entry] = [...pending.entries()].sort((a, b) => a[1].due - b[1].due)[0];
    if (Date.now() < Math.max(entry.due, lastReport + 1000)) { scheduleReports(); return; }
    pending.delete(key);
    reporting = true;
    lastReport = Date.now();
    try {
      if (!entry.beaconOnly && ["epub", "readalong"].includes(entry.state.data.mode)) {
        const xpath = await (entry.xpathTask || progressXPath(entry.state, entry.payload.locator));
        if (xpath) entry.body = JSON.stringify({ ...entry.payload, xpath });
      }
      // Async DOM resolution must not shorten the interval between actual requests.
      lastReport = Date.now();
      if (entry.beaconOnly) {
        if (!navigator.sendBeacon?.("api/read/progress", new Blob([entry.body], { type: "application/json" }))) throw new Error("Your place could not be saved.");
      } else {
        await window.OmnarrOffline.progress(JSON.parse(entry.body), entry.revision);
      }
      if (active(entry.state) && $("#reader-message").dataset.sync) {
        message(entry.state, "");
        delete $("#reader-message").dataset.sync;
      }
    } catch (error) {
      // Fetch can be unavailable during teardown. Keep the fallback throttled too.
      if (entry.urgent && !entry.beaconOnly && !error.status && error.message !== "login" && navigator.sendBeacon && !pending.has(key)) {
        pending.set(key, { ...entry, beaconOnly: true, due: lastReport + 1000 });
      } else {
        entry.state.lastQueued = "";
        if (active(entry.state)) {
          message(entry.state, error.message || "Your place could not be saved.");
          $("#reader-message").dataset.sync = "true";
        }
      }
    } finally { reporting = false; scheduleReports(); }
  }
  function report(state, urgent = false) {
    if (!state?.progress || state.data.progress_sync === false) return;
    const payload = { unit_key: state.data.unit_key, ...state.progress };
    const body = JSON.stringify(payload);
    const previous = pending.get(payload.unit_key);
    if (body === state.lastQueued && !previous) return;
    state.lastQueued = body;
    // Capture a closing book's DOM before teardown, even if another save is in flight.
    const xpathTask = urgent && ["epub", "readalong"].includes(state.data.mode) ? progressXPath(state, payload.locator) : null;
    // Persist before the debounce so closing an offline home-screen app cannot lose a turn.
    let revision;
    try { revision = window.OmnarrOffline.queue(payload); } catch { message(state, "Device storage is unavailable; keep this reader open to sync your place."); }
    pending.set(payload.unit_key, { state, payload, body, xpathTask, revision, urgent, due: Date.now() + (urgent ? 0 : 1500) });
    // Start a close/hidden report synchronously when the rate limit permits.
    if (urgent && !reporting && Date.now() >= lastReport + 1000) void sendReport();
    else scheduleReports();
  }

  function showChrome(show = true) {
    clearTimeout(chromeTimer);
    const hadChromeFocus = !show && !!document.activeElement?.closest?.(".reader-chrome");
    dialog.classList.toggle("reader-chrome-hidden", !show);
    dialog.querySelectorAll(".reader-chrome").forEach((element) => { element.inert = !show; });
    // Inert controls drop focus to <body>, where the reader's keys aren't heard: keep it in the reader.
    if (hadChromeFocus || (!show && !dialog.contains(document.activeElement))) stage.focus({ preventScroll: true });
    if (show && current?.data?.mode === "pages" && panels.every((panel) => panel.hidden)) {
      chromeTimer = setTimeout(() => {
        // Controls stay visible while a keyboard user is operating them.
        if (!dialog.querySelector(".reader-chrome :focus-visible")) showChrome(false);
      }, 3500);
    }
  }
  function dismissPanels(restore = true) {
    const opened = panels.find((panel) => !panel.hidden);
    panels.forEach((panel) => { panel.hidden = true; });
    $("#reader-settings-toggle").setAttribute("aria-expanded", "false");
    $("#reader-toc-toggle").setAttribute("aria-expanded", "false");
    stage.inert = false;
    if (restore && opened) $(opened.id === "reader-settings" ? "#reader-settings-toggle" : "#reader-toc-toggle").focus();
    showChrome();
  }
  function togglePanel(id) {
    const panel = $(id);
    const opening = panel.hidden;
    dismissPanels(false);
    if (!opening) return;
    panel.hidden = false;
    stage.inert = true;
    $(id === "#reader-settings" ? "#reader-settings-toggle" : "#reader-toc-toggle").setAttribute("aria-expanded", "true");
    clearTimeout(chromeTimer);
    $("button", panel).focus();
  }
  function destroyRendition(state) {
    // epub.js leaves its startup queue running in destroy(); cancel queued work first.
    const rendition = state.rendition;
    state.rendition = null;
    if (!rendition) return;
    rendition.q.stop();
    rendition.manager?.q?.stop();
    // Before attachTo runs there is no container for the manager to tear down.
    if (rendition.manager && !rendition.manager.rendered) rendition.manager = undefined;
    rendition.destroy();
  }
  function destroyBook(state) {
    if (!state.book || state.bookDestroyed) return;
    state.bookDestroyed = true;
    state.book.rendition = undefined;
    state.book.destroy();
  }
  function closeAll(restore = true) {
    const state = current;
    if (state) {
      stopNarration(state);
      report(state, true);
      state.closed = true;
      state.controller.abort();
      current = null;
      state.preloads = [];
      destroyRendition(state);
      if (state.book) {
        // Let an in-flight archive read finish before releasing its resources.
        // Clear future location jobs; only the current chapter needs to settle.
        state.book.locations?.q?.clear();
        if (state.bookFailed) destroyBook(state);
        else void Promise.allSettled([state.book.opened, state.locationsTask, state.resumeTask, ...(state.saveTasks || [])]).then(() => destroyBook(state));
      }
    }
    clearTimeout(chromeTimer);
    clearTimeout(resizeTimer);
    stage.replaceChildren();
    if (dialog.open) dialog.close();
    if (bodyStyle) {
      Object.assign(document.body.style, bodyStyle.styles);
      window.scrollTo(bodyStyle.x, bodyStyle.y);
      bodyStyle = null;
    }
    if (restore && returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
  }
  async function open(key, trigger) {
    const focus = trigger || (dialog.open ? returnFocus : document.activeElement);
    closeAll(false);
    returnFocus = focus;
    const state = current = { controller: new AbortController(), closed: false, data: null, progress: null, page: 1, renditionVersion: 0 };
    const styles = {};
    for (const name of ["overflow", "position", "top", "left", "right", "width"]) styles[name] = document.body.style[name];
    bodyStyle = { styles, x: window.scrollX, y: window.scrollY };
    Object.assign(document.body.style, { overflow: "hidden", position: "fixed", top: `${-window.scrollY}px`, left: "0", right: "0", width: "100%" });
    dialog.dataset.mode = "loading";
    delete dialog.dataset.theme;
    $("#reader-title").textContent = "Loading book…";
    $("#reader-chapter").textContent = "";
    $("#reader-message").textContent = "Loading…";
    delete $("#reader-message").dataset.sync;
    $("#reader-end").hidden = true;
    $("#reader-next-book").hidden = true;
    $("#reader-toc-toggle").hidden = true;
    $("#reader-settings-toggle").disabled = true;
    $("#reader-prev").disabled = $("#reader-next").disabled = slider.disabled = true;
    $("#reader-position").textContent = "";
    $("#reader-narration").hidden = true;
    $("#reader-offline").hidden = true;
    backToNarration.hidden = true;
    dismissPanels(false);
    dialog.showModal();
    $("[data-reader-close]").focus();
    try {
      const data = await readInfo(key, state.controller.signal);
      if (!active(state)) return;
      if (!["pages", "epub", "readalong"].includes(data.mode) || !data.unit_key) throw new Error("Reading details are incomplete. Reopen this book to try again.");
      state.data = data;
      dialog.dataset.mode = data.mode;
      $("#reader-title").textContent = data.title || "Book";
      $("#reader-chapter").textContent = data.series || "";
      $("#reader-settings-toggle").disabled = data.mode !== "pages";
      $("#reader-comic-settings").hidden = data.mode !== "pages";
      $("#reader-epub-settings").hidden = data.mode === "pages";
      $("#reader-offline").hidden = !data.offline_allowed;
      $("#reader-save-offline").hidden = data.mode === "readalong";
      $("#reader-offline-note").hidden = data.mode !== "readalong";
      $("#reader-narration").hidden = data.mode !== "readalong";
      $("#reader-komga-sync").hidden = !data.app_sync;
      dialog.querySelectorAll("[data-preference]").forEach((select) => { select.value = prefs[select.dataset.preference]; });
      message(state, "");
      if (data.mode === "pages") {
        if (!data.pages?.length || !data.page_url) throw new Error("This comic has no pages to read.");
        state.page = clamp(data.resume?.page || 1, 1, data.pages.length);
        renderPages(state);
      } else await openEpub(state);
      if (active(state)) showChrome();
    } catch (error) { if (active(state)) { message(state, error.message || "This book could not be opened."); showChrome(); } }
  }

  function pageGroups(state) {
    const pages = state.data.pages;
    const spread = prefs.spread === "auto" && dialog.clientWidth > 900 && dialog.clientWidth > dialog.clientHeight;
    const wide = (index) => pages[index]?.w > 0 && pages[index]?.h > 0 && pages[index].w > pages[index].h;
    const groups = [];
    for (let index = 0; index < pages.length;) {
      const count = spread && index > 0 && index + 1 < pages.length && !wide(index) && !wide(index + 1) ? 2 : 1;
      groups.push(Array.from({ length: count }, (_, offset) => index + offset + 1));
      index += count;
    }
    return groups;
  }
  function renderPages(state) {
    if (!active(state)) return;
    const groups = state.groups = pageGroups(state);
    const group = state.group = groups.findIndex((pages) => pages.includes(state.page));
    const visible = groups[group];
    stage.dataset.fit = prefs.fit;
    stage.dataset.direction = prefs.direction;
    stage.replaceChildren();
    stage.scrollTop = 0;
    visible.forEach((n) => {
      const frame = document.createElement("div");
      frame.className = "reader-page";
      const spinner = document.createElement("span");
      spinner.className = "reader-spinner";
      spinner.setAttribute("role", "status");
      spinner.setAttribute("aria-label", `Loading page ${n}`);
      const img = document.createElement("img");
      img.alt = `Page ${n} of ${state.data.pages.length}`;
      img.decoding = "async";
      img.draggable = false;
      img.onload = () => {
        spinner.remove();
        const page = state.data.pages[n - 1];
        if (!page.w || !page.h) {
          page.w = img.naturalWidth;
          page.h = img.naturalHeight;
          if (active(state) && frame.isConnected && visible.length === 2 && page.w > page.h) renderPages(state);
        }
      };
      img.onerror = () => {
        spinner.remove();
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "secondary-button reader-page-retry";
        retry.textContent = `Page ${n} couldn’t load. Retry`;
        retry.onclick = () => { retry.remove(); frame.append(spinner); img.src = state.data.page_url + n; };
        frame.append(retry);
      };
      img.src = state.data.page_url + n;
      frame.append(spinner, img);
      stage.append(frame);
    });
    slider.min = 1; slider.max = state.data.pages.length; slider.step = 1; slider.value = state.page; slider.disabled = false;
    const label = visible.length === 2 ? `${visible[0]}–${visible[1]} / ${state.data.pages.length}` : `${state.page} / ${state.data.pages.length}`;
    $("#reader-position").textContent = label;
    slider.setAttribute("aria-valuetext", `Page ${state.page} of ${state.data.pages.length}`);
    $("#reader-prev").disabled = group === 0;
    $("#reader-next").disabled = group === groups.length - 1;
    const finished = visible.includes(state.data.pages.length);
    $("#reader-end").hidden = !finished;
    state.progress = { page: finished ? state.data.pages.length : state.page, pages: state.data.pages.length, finished };
    report(state);
    state.preloads = [visible[0] - 1, visible.at(-1) + 1, visible.at(-1) + 2].filter((n) => n >= 1 && n <= state.data.pages.length).map((n) => {
      const img = new Image(); img.src = state.data.page_url + n; return img;
    });
    if (finished) { showChrome(); void loadNext(state); }
  }
  async function loadNext(state) {
    if (!state.data.next || state.nextRequested) return;
    state.nextRequested = true;
    const button = $("#reader-next-book");
    button.hidden = false;
    button.textContent = "Next book";
    try {
      const info = await readInfo(state.data.next, state.controller.signal);
      if (active(state)) button.textContent = `Next: ${info.title || "book"}`;
    } catch { /* The next book remains available and can show its own error on open. */ }
  }
  function turn(direction) {
    const state = current;
    if (!state?.data || !panels.every((panel) => panel.hidden)) return;
    if (state.data.mode === "pages" && state.groups) {
      const group = state.groups[state.group + direction];
      if (group) { state.page = group[0]; renderPages(state); }
    } else if (state.rendition && state.displayed) {
      void navigateEpub(state, () => direction > 0 ? state.rendition.next() : state.rendition.prev());
    }
  }

  function loadEngine() {
    if (!enginePromise) {
      const script = (src, global) => new Promise((resolve, reject) => {
        if (window[global]) { resolve(); return; }
        const element = document.createElement("script");
        element.src = src;
        element.onload = () => window[global] ? resolve() : reject(new Error("The ebook engine could not be loaded."));
        element.onerror = () => { element.remove(); reject(new Error("The ebook engine could not be loaded. Check your connection and reopen the book.")); };
        document.head.append(element);
      });
      enginePromise = script("vendor/jszip.min.js", "JSZip").then(() => script("vendor/epub.min.js", "ePub")).catch((error) => { enginePromise = null; throw error; });
    }
    return enginePromise;
  }
  const blockTags = new Set(["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "section", "article", "figure", "td", "dd", "dt", "table", "aside"]);
  const inlineTags = new Set(["span", "a", "em", "i", "b", "strong", "small", "sup", "sub", "u", "s", "font", "abbr", "cite", "code", "q", "mark"]);
  const localTag = (element) => element?.localName?.toLowerCase();
  async function cfiToXPath(state, cfi) {
    try {
      // Only resolve the currently displayed start; never load an old queued chapter.
      if (!active(state) || !state.displayed || state.location?.start?.cfi !== cfi) return null;
      const section = state.book.spine.get(cfi);
      if (!section) return null;
      const fragment = section.index + 1;
      const range = await state.book.getRange(cfi);
      let element = range.startContainer;
      if (element.nodeType !== 1) element = element.parentElement;
      while (element && !blockTags.has(localTag(element))) element = element.parentElement;
      if (!element) return null;
      const steps = [];
      while (localTag(element) !== "body") {
        const parent = element.parentElement;
        if (!parent) return null;
        const tag = localTag(element);
        if (!inlineTags.has(tag)) {
          const siblings = [...parent.children].filter((child) => localTag(child) === tag);
          steps.unshift(tag + (siblings.length > 1 ? `[${siblings.indexOf(element) + 1}]` : ""));
        }
        element = parent;
      }
      return `/body/DocFragment[${fragment}]/body/${steps.join("/")}.0`;
    } catch { return null; }
  }
  function saveXPath(state, cfi) {
    const task = cfiToXPath(state, cfi);
    state.saveTasks ||= new Set();
    state.saveTasks.add(task);
    void task.then(() => state.saveTasks.delete(task));
    return task;
  }
  function progressXPath(state, cfi) {
    // Keep pass 14's converter unchanged; its displayed-start guard also applies
    // to the active narration element, which can differ from the page's start.
    if (state.data.mode !== "readalong") return saveXPath(state, cfi);
    const previous = state.location;
    state.location = { ...previous, start: { ...previous?.start, cfi } };
    const task = saveXPath(state, cfi);
    state.location = previous;
    return task;
  }
  async function xpathToCfi(book, xpath) {
    let section;
    try {
      if (typeof xpath !== "string") return null;
      const path = xpath.replace(/(?:\/text\(\))?\.\d+$/, "");
      const match = /^\/body\/DocFragment\[([1-9]\d*)\]\/body(?:\/(.*))?$/.exec(path);
      if (!match) return null;
      const index = Number(match[1]) - 1;
      if (!Number.isSafeInteger(index)) return null;
      const steps = match[2] ? match[2].split("/").map((step) => /^([a-z][a-z0-9-]*)(?:\[([1-9]\d*)\])?$/i.exec(step)) : [];
      if (steps.some((step) => !step)) return null;
      section = book.spine.get(index);
      if (!section) return null;
      await section.load(book.load.bind(book));
      let element = section.document.body;
      if (!element) return null;
      for (const step of steps) {
        const child = [...element.children].filter((child) => localTag(child) === step[1].toLowerCase())[Number(step[2] || 1) - 1];
        if (!child) break;
        element = child;
      }
      return section.cfiFromElement(element);
    } catch { return null; }
    finally { try { section?.unload(); } catch { /* Conversion must never block opening. */ } }
  }
  /* Manual self-check (not executed): spine index 2, cfiBase /6/6[chapter3],
     <html><head/><body><div><p>one</p><p><em>two</em></p></div></body></html>:
     epubcfi(/6/6[chapter3]!/4/2/4/2/1:0) -> /body/DocFragment[3]/body/div/p[2].0
     -> epubcfi(/6/6[chapter3]!/4/2/4) -> the same XPath (block precision).
     div is unindexed; the two p siblings require [2]; inline em is skipped. */
  function resumeToast(state) {
    const from = state.data.resume?.from;
    if (!active(state) || !from || from === "omnarr") return;
    const device = /kobo/i.test(from) ? "your Kobo" : /koreader/i.test(from) ? "KOReader"
      : /abs-sync-bot|bookbridge/i.test(from) || from.length > 30 ? "another device" : from;
    const text = `Picked up where you left off on ${device}`;
    message(state, text);
    setTimeout(() => {
      if (active(state) && $("#reader-message").textContent === text) message(state, "");
    }, 4000);
  }

  // One audio element for the entire read-along session, with chapter-local overlays.
  const narrationActions = ['play', 'pause', 'previoustrack', 'nexttrack', 'seekbackward', 'seekforward'];
  function clipTime(value) {
    const text = String(value || '').replace(/^npt=/, '').replace(/s$/, '');
    if (!/^(?:\d+:){0,2}\d+(?:\.\d+)?$/.test(text)) return NaN;
    return text.split(':').reduce((seconds, part) => seconds * 60 + Number(part), 0);
  }
  function memberUrl(path, base) {
    const url = new URL(path, new URL(base, document.baseURI));
    const root = new URL('./', document.baseURI);
    if (url.origin !== root.origin || !url.pathname.startsWith(root.pathname)) throw new Error('Narration member is outside this book server.');
    return url.pathname.slice(root.pathname.length) + url.search + url.hash;
  }
  async function overlayFor(state, index) {
    const section = state.book.spine.get(index);
    if (!section) return [];
    state.overlays ||= new Map();
    if (state.overlays.has(index)) return state.overlays.get(index);
    const manifest = state.book.packaging.manifest;
    const overlay = manifest[manifest[section.idref]?.overlay];
    if (!overlay || overlay.type !== 'application/smil+xml') { state.overlays.set(index, []); return []; }
    const opf = memberUrl(state.book.container.packagePath, state.data.root_url);
    const smilUrl = memberUrl(overlay.href, opf);
    const source = await api(smilUrl, { responseType: 'text', signal: state.controller.signal });
    const xml = new DOMParser().parseFromString(source, 'application/xml');
    if (xml.querySelector('parsererror')) throw new Error('This chapter’s narration timing could not be read.');
    const pars = [...xml.getElementsByTagNameNS('*', 'par')].flatMap((par) => {
      const text = par.getElementsByTagNameNS('*', 'text')[0];
      const audio = par.getElementsByTagNameNS('*', 'audio')[0];
      if (!text || !audio) return [];
      const target = memberUrl(text.getAttribute('src'), smilUrl);
      const id = decodeURIComponent(target.split('#')[1] || '');
      const begin = clipTime(audio.getAttribute('clipBegin'));
      const end = clipTime(audio.getAttribute('clipEnd'));
      if (!id || !Number.isFinite(begin) || !Number.isFinite(end) || end <= begin) return [];
      return [{ id, target, src: memberUrl(audio.getAttribute('src'), smilUrl), begin, end, section: index }];
    });
    let elapsed = 0;
    for (const par of pars) {
      par.elapsed = elapsed; elapsed += par.end - par.begin;
      const element = section.document?.getElementById(par.id);
      if (element) par.cfi = section.cfiFromElement(element);
    }
    state.overlays.set(index, pars);
    return pars;
  }
  function narrationElement(state, par = state.pars?.[state.parIndex]) {
    if (!par) return null;
    for (const contents of state.rendition?.getContents() || []) {
      if (contents.sectionIndex !== par.section) continue;
      const element = contents.document.getElementById(par.id);
      if (element) return { element, contents, cfi: contents.cfiFromNode(element) };
    }
    return null;
  }
  function narrationProgress(state) {
    const par = state.pars?.[state.parIndex];
    if (!par || !state.narrationCfi) return;
    const total = state.book.spine.spineItems.length;
    const fraction = state.locationsReady ? state.book.locations.percentageFromCfi(state.narrationCfi) : (par.section + state.parIndex / Math.max(1, state.pars.length)) / Math.max(1, total);
    state.progress = { locator: state.narrationCfi, fraction: clamp(fraction, 0, 1), finished: false };
    $('#reader-position').textContent = `${Math.round(state.progress.fraction * 100)}%`;
    report(state);
  }
  async function highlightNarration(state, force = false) {
    if (!active(state)) return;
    state.highlight?.classList.remove('omnarr-narration-active');
    let found = narrationElement(state);
    const par = state.pars?.[state.parIndex];
    if (!par) return;
    const following = force || Date.now() - (state.manualAt || 0) >= 8000;
    let visible = false;
    if (found) {
      const rect = found.element.getBoundingClientRect();
      const frame = found.contents.document.defaultView.frameElement.getBoundingClientRect();
      const bounds = stage.getBoundingClientRect();
      visible = rect.right + frame.left > bounds.left && rect.left + frame.left < bounds.right && rect.bottom + frame.top > bounds.top && rect.top + frame.top < bounds.bottom;
      state.narrationCfi = found.cfi;
    }
    if (!found && par.cfi) state.narrationCfi = par.cfi;
    if (!visible && !following) backToNarration.hidden = false;
    else if (!visible && !state.following) {
      state.following = true;
      try {
        if (found && prefs.layout === 'scrolled-doc') found.element.scrollIntoView({ block: 'center' });
        else await state.rendition.display(found?.cfi || state.book.spine.get(par.section).href + '#' + encodeURIComponent(par.id));
        found = narrationElement(state);
      } catch (error) { message(state, error.message); }
      finally { state.following = false; }
    }
    if (!active(state)) return;
    if (found) {
      state.highlight = found.element; state.highlight.classList.add('omnarr-narration-active'); state.narrationCfi = par.cfi = found.cfi;
    }
    if (following) backToNarration.hidden = true;
    narrationProgress(state);
  }
  function mediaNarration(state) {
    if (!navigator.mediaSession) return;
    try {
      if (window.MediaMetadata) navigator.mediaSession.metadata = new MediaMetadata({ title: state.data.title || 'Book', artist: state.data.author || state.book.packaging.metadata.creator || '', album: 'Omnarr' });
      const handlers = { play: () => void playNarration(state), pause: () => pauseNarration(state), previoustrack: () => void stepSentence(state, -1), nexttrack: () => void stepSentence(state, 1), seekbackward: () => void stepSentence(state, -1), seekforward: () => void stepSentence(state, 1) };
      for (const [action, handler] of Object.entries(handlers)) { try { navigator.mediaSession.setActionHandler(action, handler); } catch { /* Optional action. */ } }
    } catch { /* Optional lock-screen metadata. */ }
  }
  function pauseNarration(state) {
    if (!state?.audio) return;
    state.wantPlay = false; state.audio.pause(); report(state, true);
    updateNarrationControls(state);
  }
  function updateNarrationControls(state) {
    if (!active(state) || !state.audio) return;
    const playing = state.wantPlay && !state.audio.paused;
    $('#reader-play').textContent = playing ? 'Pause' : 'Play';
    $('#reader-play').setAttribute('aria-label', playing ? 'Pause narration' : 'Play narration');
    if (navigator.mediaSession) navigator.mediaSession.playbackState = playing ? 'playing' : 'paused';
    const par = state.pars?.[state.parIndex];
    const seconds = Math.floor((par?.elapsed || 0) + (par ? clamp(state.audio.currentTime - par.begin, 0, par.end - par.begin) : 0));
    $('#reader-elapsed').textContent = Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0');
  }
  async function playNarration(state) {
    if (!active(state) || !state.audio) return;
    state.wantPlay = true;
    if (state.seekingClip) return;
    try {
      if (!state.audio.getAttribute('src')) await selectSentence(state, state.parIndex || 0, true);
      else await state.audio.play();
      if (active(state)) mediaNarration(state);
    } catch (error) {
      state.wantPlay = false;
      message(state, error.name === 'NotAllowedError' ? 'Tap Play to start narration.' : "This browser can't play the narration");
    }
    updateNarrationControls(state);
  }
  function stopNarration(state) {
    if (!state.audio) return;
    state.wantPlay = false; state.audio.pause(); cancelAnimationFrame(state.audioFrame);
    state.audio.removeAttribute('src'); state.audio.load();
    if (navigator.mediaSession) {
      for (const action of narrationActions) { try { navigator.mediaSession.setActionHandler(action, null); } catch { /* Optional action. */ } }
      navigator.mediaSession.metadata = null; navigator.mediaSession.playbackState = 'none';
      try { navigator.mediaSession.setPositionState?.(); } catch { /* Optional feature. */ }
    }
  }
  function setupNarration(state) {
    window.OmnarrPlayer?.closeAll();
    const audio = state.audio = document.createElement('audio');
    audio.preload = 'metadata';
    audio.defaultPlaybackRate = audio.playbackRate = Number(preference('speed', '1', ['0.75', '1', '1.25', '1.5', '1.75', '2']));
    $('#reader-speed').value = String(audio.playbackRate);
    state.pars = []; state.parIndex = 0;
    const check = () => {
      if (!active(state)) return;
      updateNarrationControls(state);
      const par = state.pars[state.parIndex];
      if (state.wantPlay && !state.seekingClip && !state.changingSentence && par && audio.currentTime >= par.end - 0.05) void stepSentence(state, 1, true);
      else if (state.wantPlay && !backToNarration.hidden && !state.following && !state.changingSentence && Date.now() - (state.manualAt || 0) >= 8000) void highlightNarration(state);
    };
    const frame = () => { check(); if (active(state) && !audio.paused) state.audioFrame = requestAnimationFrame(frame); };
    audio.addEventListener('timeupdate', check);
    audio.addEventListener('ended', check);
    audio.addEventListener('play', () => { cancelAnimationFrame(state.audioFrame); state.audioFrame = requestAnimationFrame(frame); updateNarrationControls(state); });
    audio.addEventListener('pause', () => { cancelAnimationFrame(state.audioFrame); updateNarrationControls(state); if (!state.seekingClip) report(state, true); });
    audio.addEventListener('error', () => { if (!state.closed && audio.getAttribute('src')) { state.wantPlay = false; message(state, "This browser can't play the narration"); updateNarrationControls(state); } });
    mediaNarration(state); updateNarrationControls(state);
  }
  async function selectSentence(state, index, play = false, continuous = false) {
    const par = state.pars[index];
    if (!par || !active(state)) { message(state, 'No narration for this part'); return; }
    const old = state.pars[state.parIndex];
    state.parIndex = index; state.wantPlay = play;
    $('#reader-end').hidden = true;
    state.narrationCfi = par.cfi || null;
    const audio = state.audio;
    const sameFile = audio.getAttribute('src') === par.src;
    state.seekingClip = true;
    try {
      if (!sameFile) {
        audio.pause();
        await new Promise((resolve, reject) => {
          const cleanup = () => { clearTimeout(timeout); audio.removeEventListener('loadedmetadata', loaded); audio.removeEventListener('error', failed); state.controller.signal.removeEventListener('abort', aborted); };
          const loaded = () => { cleanup(); resolve(); };
          const failed = () => { cleanup(); reject(new Error('Audio metadata unavailable')); };
          const aborted = () => { cleanup(); reject(new DOMException('Closed', 'AbortError')); };
          const timeout = setTimeout(failed, 15000);
          audio.addEventListener('loadedmetadata', loaded, { once: true }); audio.addEventListener('error', failed, { once: true }); state.controller.signal.addEventListener('abort', aborted, { once: true });
          audio.src = par.src; audio.load();
        });
      }
      if (!active(state)) return;
      if (!(continuous && sameFile && old && Math.abs(old.end - par.begin) <= 0.15)) audio.currentTime = par.begin;
      if (state.wantPlay) await audio.play();
      else audio.pause();
    } catch (error) {
      if (active(state)) { state.wantPlay = false; message(state, error.name === 'NotAllowedError' ? 'Tap Play to start narration.' : "This browser can't play the narration"); }
    } finally {
      state.seekingClip = false;
      if (active(state)) { await highlightNarration(state); updateNarrationControls(state); }
    }
  }
  async function stepSentence(state, direction, continuous = false) {
    if (!active(state) || state.changingSentence) return;
    state.changingSentence = true;
    const play = state.wantPlay;
    try {
      const next = state.parIndex + direction;
      const visibleIndex = state.location?.start?.cfi ? state.book.spine.get(state.location.start.cfi)?.index : undefined;
      const noNarration = state.overlays?.get(visibleIndex)?.length === 0;
      if (!noNarration && next >= 0 && next < state.pars.length) await selectSentence(state, next, play, continuous);
      else {
        const start = noNarration ? visibleIndex : state.pars[state.parIndex]?.section ?? visibleIndex ?? -1;
        for (let index = start + direction; index >= 0 && index < state.book.spine.spineItems.length; index += direction) {
          if (!state.book.packaging.manifest[state.book.spine.get(index).idref]?.overlay) continue;
          const pars = await overlayFor(state, index);
          if (!active(state)) return;
          if (!pars.length) continue;
          state.audio.pause(); state.pars = pars; state.parIndex = direction > 0 ? 0 : pars.length - 1;
          const section = state.book.spine.get(index);
          if (!continuous || Date.now() - (state.manualAt || 0) >= 8000) {
            state.manualAt = 0;
            await state.rendition.display(section.href);
          } else {
            // Keep the user's page in place even when narration changes chapters.
            await section.load(state.book.load.bind(state.book));
            for (const par of pars) { const element = section.document.getElementById(par.id); if (element) par.cfi = section.cfiFromElement(element); }
          }
          await selectSentence(state, state.parIndex, play); return;
        }
        pauseNarration(state);
        if (direction > 0) { state.progress = { ...state.progress, fraction: 1, finished: true }; report(state, true); $('#reader-end').hidden = false; }
      }
    } catch (error) { pauseNarration(state); message(state, error.message); }
    finally { state.changingSentence = false; }
  }
  async function resumeNarration(state, locator, target) {
    // display() can settle before its relocated event; use the requested target.
    const section = state.book.spine.get(locator || target || state.location?.start?.cfi) || state.book.spine.get(0);
    const pars = await overlayFor(state, section.index);
    if (!active(state)) return;
    state.pars = pars; state.parIndex = 0;
    if (!pars.length) { message(state, 'No narration for this part'); return; }
    if (locator) {
      const comparator = new window.ePub.CFI();
      let index = pars.findIndex((par) => {
        const found = narrationElement(state, par);
        return found && comparator.compare(found.cfi, locator) >= 0;
      });
      state.parIndex = index < 0 ? pars.length - 1 : index;
    } else if (state.data.resume?.fraction > 0) {
      const within = state.data.resume.fraction * state.book.spine.spineItems.length - section.index;
      state.parIndex = clamp(Math.floor(within * pars.length), 0, pars.length - 1);
    }
    state.changingSentence = true;
    try { await selectSentence(state, state.parIndex, false); }
    finally { state.changingSentence = false; }
    resumeToast(state);
  }

  async function openEpub(state) {
    const along = state.data.mode === "readalong";
    if (!(along ? state.data.root_url : state.data.file_url)) throw new Error("This book has no EPUB to read in the browser.");
    message(state, "Loading ebook…");
    await loadEngine();
    if (!active(state)) return;
    const book = state.book = along ? window.ePub(state.data.root_url) : window.ePub(state.data.file_url, { openAs: "epub" });
    book.on("openFailed", (error) => {
      state.bookFailed = true;
      message(state, error.message || "This EPUB could not be opened.");
      if (state.closed) destroyBook(state);
    });
    await book.opened;
    if (!active(state)) return;
    const navigation = await book.loaded.navigation;
    if (!active(state)) return;
    state.toc = [];
    const tocList = (items) => {
      const list = document.createElement("ol");
      for (const item of items) {
        state.toc.push(item);
        const li = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "text-button";
        button.textContent = item.label || "Untitled chapter";
        button.dataset.href = item.href;
        button.onclick = () => { dismissPanels(); void navigateEpub(state, () => state.rendition.display(item.href)); };
        li.append(button);
        if (item.subitems?.length) li.append(tocList(item.subitems));
        list.append(li);
      }
      return list;
    };
    $("#reader-toc-list").replaceChildren(tocList(navigation.toc || []));
    if (!state.toc.length) $("#reader-toc-list").textContent = "No table of contents is available.";
    $("#reader-toc-toggle").hidden = false;
    $("#reader-toc-toggle").disabled = true;
    const resume = state.data.resume;
    let locator = resume?.locator;
    if (!locator && resume?.xpath) {
      state.resumeTask = xpathToCfi(book, resume.xpath);
      locator = await state.resumeTask;
      if (!active(state)) return;
    }
    const fractionResume = !locator && resume?.fraction > 0;
    state.resuming = true;
    if (along) {
      setupNarration(state);
      let target = locator;
      if (!target && fractionResume) target = book.spine.get(Math.min(book.spine.spineItems.length - 1, Math.floor(resume.fraction * book.spine.spineItems.length))).href;
      if (!target) target = book.spine.spineItems.find((section) => book.packaging.manifest[section.idref]?.overlay)?.href;
      try { await renderEpub(state, target); }
      catch (error) {
        if (!locator) throw error;
        locator = resume?.xpath ? await xpathToCfi(book, resume.xpath) : null;
        if (!active(state)) return;
        target = locator || book.spine.get(Math.min(book.spine.spineItems.length - 1, Math.floor((resume?.fraction || 0) * book.spine.spineItems.length))).href;
        await renderEpub(state, target);
      }
      if (!active(state)) return;
      state.resuming = false;
      $('#reader-settings-toggle').disabled = $('#reader-toc-toggle').disabled = false;
      await resumeNarration(state, locator, target);
      // locations.generate() fetches EVERY spine document. Keep directory books lazy;
      // use loaded locations if available, otherwise chapter/sentence estimates.
      return;
    }
    await renderEpub(state, locator || undefined);
    if (!active(state)) return;
    state.resuming = fractionResume;
    if (!fractionResume && state.location) relocated(state, state.location);
    if (locator) resumeToast(state);
    $("#reader-settings-toggle").disabled = false;
    $("#reader-toc-toggle").disabled = false;
    // Generating locations is deliberately outside the first-display critical path.
    state.locationsTask = book.locations.generate(1600).then(async () => {
      if (!active(state)) return;
      state.locationsReady = true;
      if (fractionResume) {
        const cfi = book.locations.cfiFromPercentage(clamp(resume.fraction, 0, 1));
        if (!cfi) throw new Error("No percentage location is available.");
        // display() may resolve before relocated; don't replay the temporary start.
        state.location = null;
        await state.rendition.display(cfi);
        if (!active(state)) return;
        state.resuming = false;
        resumeToast(state);
      }
      if (state.location) relocated(state, state.location);
    }).catch(() => {
      state.resuming = false;
      message(state, "Percentage navigation is unavailable. You can still read and use Contents.");
    });
  }
  async function navigateEpub(state, operation) {
    if (!active(state) || state.navigating) return;
    const version = state.renditionVersion;
    state.navigating = true;
    if (state.audio) state.manualAt = Date.now();
    try { await operation(); }
    catch (error) { message(state, error.message || "This part of the book could not be displayed."); }
    finally { if (version === state.renditionVersion) state.navigating = false; }
  }
  function applyTheme(state) {
    const theme = prefs.theme === "system" ? (darkScheme.matches ? "dark" : "light") : prefs.theme;
    dialog.dataset.theme = theme;
    const rendition = state.rendition;
    rendition.themes.select(theme);
    rendition.themes.fontSize(`${prefs.font}%`);
    $("#reader-font-size").textContent = `${prefs.font}%`;
    $("#reader-font-down").disabled = prefs.font <= 80;
    $("#reader-font-up").disabled = prefs.font >= 160;
  }
  async function renderEpub(state, locator) {
    const version = ++state.renditionVersion;
    state.navigating = false;
    state.displayed = false;
    destroyRendition(state);
    stage.replaceChildren();
    const target = document.createElement("div");
    target.className = "reader-epub";
    stage.append(target);
    const rendition = state.rendition = state.book.renderTo(target, { width: "100%", height: "100%", flow: prefs.layout, spread: "auto", allowScriptedContent: false });
    const valid = () => active(state) && version === state.renditionVersion;
    rendition.hooks.content.register((contents) => {
      contents.document.addEventListener('wheel', () => { if (valid() && state.audio) state.manualAt = Date.now(); }, { passive: true });
    });
    for (const [name, colors] of Object.entries({ light: ["#fbfaf6", "#1d2426"], sepia: ["#f1e5cf", "#493b2b"], dark: ["#151a21", "#e4e6eb"] })) {
      rendition.themes.register(name, { body: { background: `${colors[0]} !important`, color: `${colors[1]} !important` }, "a": { color: name === "dark" ? "#efa17c !important" : "#a43e23 !important" }, ".omnarr-narration-active, .-epub-media-overlay-active": { background: name === "dark" ? "#705521 !important" : "#f5e6a5 !important", color: name === "dark" ? "#fff1d4 !important" : "#302b20 !important" }, "img, svg": { "max-width": "100%" }, "*": { "animation": "none !important", "transition": "none !important" } });
    }
    applyTheme(state);
    rendition.on("relocated", (location) => { if (valid()) relocated(state, location); });
    rendition.on("keyup", (event) => { if (valid()) key(event, true); });
    rendition.on("keydown", (event) => {
      if (!valid()) return;
      if (event.key === "Tab") { event.preventDefault(); showChrome(); $("[data-reader-close]").focus(); }
      else if (!interactive(event.target) && ["ArrowLeft", "ArrowRight", "PageUp", "PageDown", " "].includes(event.key)) event.preventDefault();
    });
    rendition.on("click", (event, contents) => { if (valid()) tap(event, contents); });
    rendition.on("touchstart", (event) => { if (valid()) touchStart(event); });
    rendition.on("touchend", (event) => { if (valid()) touchEnd(event); });
    rendition.on("displayError", (error) => { if (valid()) message(state, error.message || "This chapter could not be displayed."); });
    await rendition.display(locator || undefined);
    if (!valid()) return;
    state.displayed = true;
    message(state, "");
    $("#reader-prev").disabled = Boolean(state.location?.atStart);
    $("#reader-next").disabled = Boolean(state.location?.atEnd);
    if (state.audio && state.pars?.length) void highlightNarration(state);
  }
  function relocated(state, location) {
    state.location = location;
    const start = location.start;
    if (!start?.cfi) return;
    const href = (start.href || "").split("#")[0];
    const chapter = state.toc?.find((item) => item.href === start.href) || state.toc?.find((item) => (item.href || "").split("#")[0] === href);
    $("#reader-chapter").textContent = chapter?.label || "";
    $("#reader-toc-list").querySelectorAll("button").forEach((button) => {
      if (chapter && button.dataset.href === chapter.href) button.setAttribute("aria-current", "location");
      else button.removeAttribute("aria-current");
    });
    let fraction = Number(start.percentage) || 0;
    if (!fraction || state.locationsReady) fraction = state.book.locations.percentageFromCfi(start.cfi) ?? fraction;
    fraction = clamp(fraction, 0, 1);
    $("#reader-prev").disabled = Boolean(location.atStart);
    $("#reader-next").disabled = Boolean(location.atEnd);
    slider.disabled = !state.locationsReady;
    if (state.locationsReady) {
      slider.min = 0; slider.max = 100; slider.step = 0.1; slider.value = fraction * 100;
      slider.setAttribute("aria-valuetext", `${Math.round(fraction * 100)}%`);
      $("#reader-position").textContent = `${Math.round(fraction * 100)}%`;
    }
    const resume = state.data.resume;
    const hasResume = Boolean(resume?.locator || resume?.xpath || resume?.fraction > 0);
    if (state.resuming) return;
    if (state.audio) {
      const section = state.book.spine.get(start.cfi);
      if (section && !state.book.packaging.manifest[section.idref]?.overlay) {
        state.overlays.set(section.index, []);
        message(state, 'No narration for this part');
      }
      // Load only the displayed chapter's overlay so tapping a manually visited
      // sentence works while the audio remains at the previous narration position.
      if (section) void overlayFor(state, section.index).catch((error) => message(state, error.message));
      if (state.pars?.length) {
        narrationProgress(state);
        const estimated = state.progress?.fraction || 0;
        $('#reader-position').textContent = `${Math.round(estimated * 100)}%`;
        if (!state.following && state.wantPlay) backToNarration.hidden = false;
      }
      return;
    }
    if (!fraction && !state.locationsReady && hasResume) return;
    state.progress = { fraction, locator: start.cfi, finished: Boolean(location.atEnd) };
    report(state);
  }

  const interactive = (target) => target?.closest?.("a, button, input, select, textarea, [contenteditable='true']");
  function directionSign() { return current?.data?.mode === "pages" && prefs.direction === "rtl" ? -1 : 1; }
  function key(event, iframe = false) {
    if (!current || !dialog.open) return;
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeAll(); return; }
    if (event.key === "Tab" && !iframe) {
      showChrome();
      const focusable = [...dialog.querySelectorAll("button, input, select, [tabindex='0']")].filter((el) => !el.disabled && !el.closest("[hidden], [inert]") && el.getClientRects().length);
      const index = focusable.indexOf(document.activeElement);
      if (event.shiftKey && index <= 0) { event.preventDefault(); focusable.at(-1)?.focus(); }
      else if (!event.shiftKey && (index === focusable.length - 1 || index < 0)) { event.preventDefault(); focusable[0]?.focus(); }
      return;
    }
    if (event.target?.closest?.("input, select, textarea, [contenteditable='true']") ||
      (event.key === " " && interactive(event.target)) || event.ctrlKey || event.metaKey || event.altKey || !panels.every((panel) => panel.hidden)) return;
    let direction;
    if (current.audio) {
      if (event.key === ' ') { event.preventDefault(); current.wantPlay ? pauseNarration(current) : void playNarration(current); return; }
      if (event.shiftKey && ['ArrowLeft', 'ArrowRight'].includes(event.key)) { event.preventDefault(); void stepSentence(current, event.key === 'ArrowLeft' ? -1 : 1); return; }
    }
    if (event.key === "ArrowLeft") direction = -directionSign();
    if (event.key === "ArrowRight") direction = directionSign();
    if (event.key === "PageUp" || (event.key === " " && event.shiftKey)) direction = -1;
    if (event.key === "PageDown" || (event.key === " " && !event.shiftKey)) direction = 1;
    if (direction) { event.preventDefault(); turn(direction); }
    if (["Home", "End"].includes(event.key) && current.data?.mode === "pages" && current.groups) {
      event.preventDefault(); current.page = event.key === "Home" ? 1 : current.data.pages.length; renderPages(current);
    }
  }
  let gesture;
  let suppressClickUntil = 0;
  function zoomed() { return window.visualViewport && window.visualViewport.scale > 1.05; }
  function touchStart(event) {
    if (event.touches.length !== 1 || zoomed()) { gesture = null; suppressClickUntil = Date.now() + 700; return; }
    gesture = { x: event.touches[0].clientX, y: event.touches[0].clientY, time: Date.now(), target: event.target };
  }
  function touchEnd(event) {
    const start = gesture;
    gesture = null;
    if (!start || event.touches.length || !event.changedTouches.length || zoomed()) return;
    const dx = event.changedTouches[0].clientX - start.x;
    const dy = event.changedTouches[0].clientY - start.y;
    if (Math.abs(dx) > 12 || Math.abs(dy) > 12) suppressClickUntil = Date.now() + 700;
    if (current?.audio && Math.abs(dy) > 12) current.manualAt = Date.now();
    if (!interactive(start.target) && Math.abs(dx) > 55 && Math.abs(dx) > Math.abs(dy) * 1.5 && Date.now() - start.time < 900) turn((dx < 0 ? 1 : -1) * directionSign());
  }
  function tap(event, contents) {
    if (Date.now() < suppressClickUntil || interactive(event.target) || zoomed()) return;
    const selection = contents?.window?.getSelection?.() || window.getSelection();
    if (selection && !selection.isCollapsed) return;
    if (current?.audio && contents) {
      const state = current;
      const pars = state.overlays?.get(contents.sectionIndex);
      const index = pars?.findIndex((par) => {
        const span = contents.document.getElementById(par.id);
        return span && (span === event.target || span.contains(event.target));
      }) ?? -1;
      if (index >= 0) {
        event.preventDefault();
        if (!state.changingSentence) {
          state.changingSentence = true; state.manualAt = 0; state.pars = pars;
          void selectSentence(state, index, true).finally(() => { state.changingSentence = false; });
        }
        return;
      }
    }
    if (!contents) stage.focus({ preventScroll: true });
    const rect = stage.getBoundingClientRect();
    const frame = contents?.document?.defaultView?.frameElement;
    const x = event.clientX + (frame ? frame.getBoundingClientRect().left : 0) - rect.left;
    if (x < rect.width / 3) turn(-directionSign());
    else if (x > rect.width * 2 / 3) turn(directionSign());
    else showChrome(dialog.classList.contains("reader-chrome-hidden"));
  }
  dialog.querySelectorAll("[data-reader-close]").forEach((button) => button.addEventListener("click", () => closeAll()));
  dialog.querySelectorAll("[data-reader-dismiss]").forEach((button) => button.addEventListener("click", () => dismissPanels()));
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); closeAll(); });
  dialog.addEventListener("close", () => { if (!dialog.open && current) closeAll(); });
  dialog.addEventListener("keydown", key);
  dialog.addEventListener("focusin", (event) => { if (event.target !== stage) showChrome(); });
  stage.addEventListener("click", (event) => { if (current?.data?.mode === "pages") tap(event); });
  stage.addEventListener("touchstart", touchStart, { passive: true });
  stage.addEventListener("touchend", touchEnd, { passive: true });
  stage.addEventListener("touchcancel", () => { gesture = null; }, { passive: true });
  $("#reader-settings-toggle").onclick = () => togglePanel("#reader-settings");
  $("#reader-toc-toggle").onclick = () => togglePanel("#reader-toc");
  $('#reader-save-offline').onclick = () => { if (current?.data) window.OmnarrOffline.offer(current.data); };
  $('#reader-play').onclick = () => { if (current?.audio) current.wantPlay ? pauseNarration(current) : void playNarration(current); };
  $('#reader-sentence-prev').onclick = () => { if (current?.audio) void stepSentence(current, -1); };
  $('#reader-sentence-next').onclick = () => { if (current?.audio) void stepSentence(current, 1); };
  $('#reader-speed').onchange = (event) => { savePreference('speed', event.target.value); if (current?.audio) current.audio.defaultPlaybackRate = current.audio.playbackRate = Number(event.target.value); };
  backToNarration.onclick = () => { if (current?.audio) { current.manualAt = 0; void highlightNarration(current, true); } };
  $("#reader-prev").onclick = () => turn(-1);
  $("#reader-next").onclick = () => turn(1);
  $("#reader-next-book").onclick = () => { if (current?.data?.next) void open(current.data.next); };
  slider.addEventListener("input", () => {
    const label = current?.data?.mode === "pages" ? `${slider.value} / ${current.data.pages.length}` : `${Math.round(Number(slider.value))}%`;
    $("#reader-position").textContent = label;
    slider.setAttribute("aria-valuetext", label);
  });
  slider.addEventListener("change", () => {
    const state = current;
    if (state?.data?.mode === "pages") { state.page = clamp(slider.value, 1, state.data.pages.length); renderPages(state); }
    else if (state?.locationsReady) {
      const cfi = state.book.locations.cfiFromPercentage(Number(slider.value) / 100);
      if (cfi) void navigateEpub(state, () => state.rendition.display(cfi));
    }
  });
  dialog.querySelectorAll("[data-preference]").forEach((select) => select.addEventListener("change", () => {
    const key = select.dataset.preference;
    prefs[key] = select.value;
    savePreference(key, select.value);
    const state = current;
    if (state?.data?.mode === "pages") renderPages(state);
    else if (state?.rendition) {
      if (key === "layout") void renderEpub(state, state.location?.start?.cfi || state.data.resume?.locator).catch((error) => message(state, error.message));
      else applyTheme(state);
    }
  }));
  function font(delta) {
    prefs.font = clamp(prefs.font + delta, 80, 160);
    savePreference("font", prefs.font);
    if (current?.rendition) applyTheme(current);
  }
  $("#reader-font-down").onclick = () => font(-10);
  $("#reader-font-up").onclick = () => font(10);
  darkScheme.addEventListener("change", () => { if (current?.rendition && prefs.theme === "system") applyTheme(current); });
  function resize() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      const state = current;
      if (state?.data?.mode === "pages" && state.groups) renderPages(state);
      else if (state?.rendition && state.displayed) state.rendition.resize();
    }, 150);
  }
  window.addEventListener("resize", resize);
  window.addEventListener("orientationchange", resize);
  const updateOfflineStatus = () => { $('#reader-offline-status').hidden = navigator.onLine; };
  window.addEventListener('online', updateOfflineStatus);
  window.addEventListener('offline', updateOfflineStatus);
  updateOfflineStatus();
  window.addEventListener("pagehide", () => report(current, true));
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") report(current, true); });
  return { open, closeAll, readInfo };
})();
