"use strict";

// Playback owns its DOM and lifetime; library navigation never replaces media.
window.OmnarrPlayer = (() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const number = (value) => Math.max(0, Number(value) || 0);
  const clamp = (value, max) => Math.min(number(value), number(max));
  const time = (value) => {
    const seconds = Math.floor(number(value));
    return `${Math.floor(seconds / 3600)}:${String(Math.floor(seconds / 60) % 60).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  };
  const readPreference = (key, fallback) => {
    try { const value = localStorage.getItem(`omnarr.player.${key}`); return value === null || !Number.isFinite(Number(value)) ? fallback : Number(value); } catch { return fallback; }
  };
  const savePreference = (key, value) => { try { localStorage.setItem(`omnarr.player.${key}`, String(value)); } catch { /* Storage can be unavailable in private browsing. */ } };
  let speed = Math.min(2.5, Math.max(0.8, readPreference("speed", 1) || 1));
  let volume = Math.min(1, Math.max(0, readPreference("volume", 1)));
  let audioState = null;
  let videoState = null;
  let audioRequest = 0;
  let videoRequest = 0;
  let hlsPromise;
  let sessionAudio = false;

  const host = document.createElement("div");
  host.innerHTML = `
    <dialog id="video-player" class="video-player" aria-labelledby="video-title">
      <header class="player-heading"><div><span class="eyebrow">Now watching</span><h2 id="video-title">Loading video…</h2></div><button type="button" class="icon-button" data-video-close aria-label="Close video player">×</button></header>
      <div class="video-stage"></div>
      <div class="player-toolbar">
        <button type="button" class="secondary-button" data-video-skip="-10" aria-label="Skip back 10 seconds">−10 s</button>
        <button type="button" class="secondary-button" data-video-skip="10" aria-label="Skip forward 10 seconds">+10 s</button>
        <label>Subtitles<select id="video-subtitles" aria-label="Subtitles"><option value="off">Off</option></select></label>
        <button type="button" class="secondary-button" id="video-pip" hidden>Picture in picture</button>
        <button type="button" class="primary-button" id="video-next" hidden>Next episode</button>
      </div>
      <div class="player-countdown" hidden><span id="video-countdown" role="status"></span><button type="button" class="text-button" id="video-cancel-next">Cancel auto-play</button></div>
      <p class="player-message" id="video-message" role="status" aria-live="polite"></p>
      <p class="hint player-sync-note" id="video-sync-note" hidden>Progress isn't saved — link your accounts in Settings → My account.</p>
    </dialog>
    <section id="audio-dock" class="audio-dock" aria-label="Audiobook mini-player" hidden>
      <button type="button" class="audio-dock-info" id="audio-expand" aria-label="Expand audiobook player" aria-controls="audio-sheet" aria-expanded="false"><img id="audio-mini-cover" alt="" hidden><span><strong id="audio-mini-title">Audiobook</strong><small id="audio-mini-chapter">Loading…</small></span></button>
      <button type="button" class="icon-button" data-audio-skip="-30" aria-label="Skip back 30 seconds">−30</button>
      <button type="button" class="primary-button audio-toggle" aria-label="Play audiobook" disabled>Play</button>
      <button type="button" class="icon-button" data-audio-close aria-label="Close audiobook player">×</button>
    </section>
    <dialog id="audio-sheet" class="audio-sheet" aria-labelledby="audio-title">
      <header class="player-heading"><span class="eyebrow">Now listening</span><button type="button" class="icon-button" id="audio-collapse" aria-label="Minimize audiobook player">⌄</button></header>
      <div class="audio-sheet-content">
        <img id="audio-cover" class="audio-cover" alt="" hidden><h2 id="audio-title">Loading audiobook…</h2><p id="audio-chapter" class="hint"></p>
        <label class="sr-only" for="audio-scrubber">Position in audiobook</label><input id="audio-scrubber" type="range" min="0" max="1" step="1" value="0" disabled>
        <div class="audio-times"><output id="audio-position">0:00:00</output><span id="audio-duration">0:00:00</span></div>
        <div class="audio-transport"><button type="button" class="secondary-button" data-audio-skip="-30" aria-label="Skip back 30 seconds">−30 s</button><button type="button" class="primary-button audio-toggle" aria-label="Play audiobook" disabled>Play</button><button type="button" class="secondary-button" data-audio-skip="30" aria-label="Skip forward 30 seconds">+30 s</button></div>
        <div class="audio-options">
          <label>Speed<select id="audio-speed">${[0.8, 1, 1.1, 1.2, 1.25, 1.3, 1.4, 1.5, 1.6, 1.75, 1.8, 2, 2.25, 2.5].map((rate) => `<option value="${rate}">${rate}×</option>`).join("")}</select></label>
          <label>Sleep timer<select id="audio-sleep"><option value="off">Off</option><option value="15">15 minutes</option><option value="30">30 minutes</option><option value="45">45 minutes</option><option value="60">60 minutes</option><option value="chapter">End of chapter</option></select></label>
          <label class="audio-volume">Volume<input id="audio-volume" type="range" min="0" max="1" step="0.05" aria-label="Audiobook volume"></label>
        </div>
        <p id="audio-sleep-status" class="hint" role="status"></p><p id="audio-message" class="player-message" role="status" aria-live="polite"></p>
        <p class="hint player-sync-note" id="audio-sync-note" hidden>Progress isn't saved — link your accounts in Settings → My account.</p>
        <details class="audio-chapters" open><summary>Chapters</summary><ol id="audio-chapters"></ol></details>
        <button type="button" class="text-button" data-audio-close>Close audiobook player</button>
      </div>
    </dialog>`;
  document.body.append(...host.children);
  const videoDialog = $("#video-player");
  const audioDialog = $("#audio-sheet");
  const dock = $("#audio-dock");
  const scrubber = $("#audio-scrubber");
  let scrubbing = false;

  // A modal detail view makes siblings inert. Move only the dock, never the audio.
  const detail = $("#detail");
  function mountDock() { (detail?.open ? detail : document.body).append(dock); }
  if (detail) new MutationObserver(mountDock).observe(detail, { attributes: true, attributeFilter: ["open"] });

  async function getInfo(type, id, signal) {
    const data = await api(`api/play/${type}/${encodeURIComponent(id)}`, { cache: "no-store", signal });
    if (data.type !== type || !data.item_id) throw new Error("Playback details are incomplete. Try again.");
    return data;
  }

  function message(state, text, sync = false) {
    if (state !== audioState && state !== videoState) return;
    const target = $(state === audioState ? "#audio-message" : "#video-message");
    if (sync && target.textContent && !target.dataset.sync) return;
    target.textContent = text;
    target.dataset.sync = sync ? "true" : "";
    if (state === audioState && text) $("#audio-mini-chapter").textContent = text;
  }

  function position(state) {
    if (!state.ready || state.pendingSeek !== null) return state.position;
    const offset = state.type === "audio" ? number(state.data.tracks[state.track].offset) : 0;
    return clamp(offset + number(state.media.currentTime), state.data.duration);
  }

  // Capture before any source replacement. Errors never become unhandled promises.
  function report(state, keepalive = false) {
    if (!state?.data || !state.started) return Promise.resolve();
    const payload = { source: state.type === "audio" ? "abs" : "jellyfin", item_id: state.data.item_id, position: position(state), duration: number(state.data.duration), finished: Boolean(state.finished) };
    const version = state.reportVersion = (state.reportVersion || 0) + 1;
    const send = async () => {
      if (version < (state.lastSent || 0)) return;
      state.lastSent = version;
      try {
        await api("api/play/progress", { method: "POST", body: JSON.stringify(payload), keepalive });
        const target = $(state.type === "audio" ? "#audio-message" : "#video-message");
        if (target.dataset.sync) message(state, "", true);
      } catch (error) { message(state, error.message || "Your place could not be saved.", true); }
    };
    // Unload reports must start immediately, without waiting on another request.
    if (keepalive) return send();
    state.reporting = (state.reporting || Promise.resolve()).then(send);
    return state.reporting;
  }

  function stopSession(data) {
    if (!data?.play_session_id) return;
    void api("api/play/stop", { method: "POST", body: JSON.stringify({ play_session_id: data.play_session_id }), keepalive: true }).catch(() => {});
  }

  function preferences(media, type) {
    media.volume = volume;
    if (type === "audio") media.playbackRate = speed;
    media.addEventListener("volumechange", () => {
      volume = media.volume;
      savePreference("volume", volume);
      $("#audio-volume").value = volume;
    });
  }

  function pauseState(state) {
    if (!state) return;
    state.wantPlay = false;
    state.media.pause();
  }

  async function play(state) {
    if (!state?.data) return;
    state.media.volume = volume;
    if (state.type === "audio") {
      cancelCountdown();
      pauseState(videoState);
      setMediaSession(state);
    } else pauseState(audioState);
    try { await state.media.play(); }
    catch (error) { if (error.name !== "AbortError") message(state, "Press Play to start playback. If it still fails, reopen this item."); }
  }

  function loadHls() {
    if (window.Hls) return Promise.resolve(window.Hls);
    if (!hlsPromise) hlsPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "vendor/hls.min.js";
      script.onload = () => window.Hls ? resolve(window.Hls) : reject(new Error("The video engine could not be loaded."));
      script.onerror = () => { script.remove(); reject(new Error("The video engine could not be loaded. Check your connection and reopen the video.")); };
      document.head.append(script);
    }).catch((error) => { hlsPromise = null; throw error; });
    return hlsPromise;
  }

  function stateFor(type, media) {
    return { type, media, data: null, ready: false, started: false, position: 0, pendingSeek: null, finished: false, track: -1, wantPlay: true };
  }

  function cancelCountdown() {
    if (videoState) clearInterval(videoState.countdown);
    $(".player-countdown").hidden = true;
  }

  function closeVideo() {
    ++videoRequest;
    const state = videoState;
    cancelCountdown();
    if (state) {
      void report(state, true);
      videoState = null;
      state.media.pause();
      state.hls?.destroy();
      state.media.removeAttribute("src");
      state.media.load();
      state.media.remove();
      stopSession(state.data);
    }
    if (videoDialog.open) videoDialog.close();
    if (audioState?.data) { setMediaSession(audioState); renderAudio(); }
  }

  async function openVideo(id, episodes = []) {
    closeVideo();
    const request = ++videoRequest;
    pauseState(audioState);
    clearMediaSession();
    const media = document.createElement("video");
    media.controls = true;
    media.playsInline = true;
    media.preload = "metadata";
    media.setAttribute("aria-label", "Video player");
    $(".video-stage").replaceChildren(media);
    const state = videoState = stateFor("video", media);
    state.episodes = [...episodes];
    $("#video-title").textContent = "Loading video…";
    $("#video-message").textContent = "";
    $("#video-sync-note").hidden = true;
    $("#video-message").dataset.sync = "";
    $("#video-subtitles").replaceChildren(new Option("Off", "off"));
    $("#video-next").hidden = true;
    $("#video-pip").hidden = !(document.pictureInPictureEnabled && media.requestPictureInPicture) && !media.webkitSupportsPresentationMode?.("picture-in-picture");
    videoDialog.showModal();
    $("[data-video-close]").focus();
    preferences(media, "video");
    media.addEventListener("loadedmetadata", () => {
      if (state !== videoState || !state.data) return;
      media.currentTime = clamp(state.position, state.data.duration);
      state.pendingSeek = null;
      state.ready = true;
      state.started = true;
      applySubtitles();
      if (state.wantPlay) void play(state);
    }, { once: true });
    media.addEventListener("play", () => { if (state === videoState) { state.wantPlay = true; pauseState(audioState); clearMediaSession(); cancelCountdown(); } });
    media.addEventListener("pause", () => { if (state === videoState) { if (state.ready) state.wantPlay = false; void report(state); } });
    media.addEventListener("seeked", () => {
      if (state !== videoState) return;
      if (!media.ended) state.finished = false;
      cancelCountdown();
      void report(state);
    });
    media.addEventListener("ended", () => {
      if (state !== videoState) return;
      state.finished = true;
      void report(state);
      if (!nextEpisode()) return;
      let remaining = 10;
      $(".player-countdown").hidden = false;
      $("#video-countdown").textContent = `Next episode in ${remaining} seconds`;
      state.countdown = setInterval(() => {
        $("#video-countdown").textContent = `Next episode in ${--remaining} seconds`;
        if (remaining <= 0) advanceEpisode();
      }, 1000);
    });
    media.addEventListener("error", () => message(state, "This video could not be played. Reopen it to try again, or use Open in Jellyfin."));
    try {
      const data = await getInfo("video", id);
      if (request !== videoRequest) { stopSession(data); return; }
      state.data = data;
      $("#video-sync-note").hidden = data.progress_sync !== false;
      state.position = clamp(data.resume, data.duration);
      if (!data.url || !["direct", "hls"].includes(data.mode)) throw new Error("No supported video stream is available.");
      $("#video-title").textContent = data.title || "Video";
      media.poster = data.poster || "";
      const subtitles = Array.isArray(data.subtitles) ? data.subtitles : [];
      state.subtitleTracks = [];
      const defaultIndex = subtitles.findIndex((sub) => sub.default);
      subtitles.forEach((sub, index) => {
        const track = document.createElement("track");
        track.kind = "subtitles";
        track.src = sub.url;
        track.srclang = sub.lang || "und";
        track.label = sub.label || `Subtitles ${index + 1}`;
        track.default = index === defaultIndex;
        state.subtitleTracks.push(track);
        track.addEventListener("load", () => { if (state === videoState) applySubtitles(); });
        track.addEventListener("error", () => message(state, "A subtitle track could not be loaded. Try another subtitle track."));
        media.append(track);
        $("#video-subtitles").add(new Option(track.label, String(index)));
      });
      $("#video-subtitles").value = defaultIndex < 0 ? "off" : String(defaultIndex);
      applySubtitles();
      media.textTracks.addEventListener("change", () => {
        if (state !== videoState) return;
        const showing = state.subtitleTracks.findIndex((track) => track.track.mode === "showing");
        $("#video-subtitles").value = showing < 0 ? "off" : String(showing);
      });
      $("#video-next").hidden = !nextEpisode();
      if (data.mode === "direct" || media.canPlayType("application/vnd.apple.mpegurl")) media.src = data.url;
      else {
        const Hls = await loadHls();
        if (state !== videoState) return;
        if (!Hls.isSupported()) throw new Error("This browser cannot play this video stream. Try Open in Jellyfin.");
        const hls = state.hls = new Hls();
        hls.on(Hls.Events.ERROR, (_event, info) => {
          if (info.fatal && state === videoState) { media.pause(); message(state, "Video streaming stopped. Close and reopen the player to retry."); }
        });
        hls.loadSource(data.url);
        hls.attachMedia(media);
      }
    } catch (error) { message(state, error.message); }
  }

  function applySubtitles() {
    if (!videoState) return;
    const selected = $("#video-subtitles").value;
    const active = videoState.subtitleTracks?.[Number(selected)]?.track;
    [...videoState.media.textTracks].forEach((track) => {
      if (["subtitles", "captions"].includes(track.kind)) track.mode = track === active ? "showing" : "disabled";
    });
  }
  function nextEpisode() {
    const state = videoState;
    if (!state?.data) return null;
    const index = state.episodes.findIndex((episode) => String(episode.jellyfin_id) === String(state.data.item_id));
    return index < 0 ? null : state.episodes.slice(index + 1).find((episode) => episode.jellyfin_id && episode.has_file);
  }
  function advanceEpisode() {
    const next = nextEpisode();
    const episodes = videoState?.episodes;
    cancelCountdown();
    if (next) void openVideo(next.jellyfin_id, episodes);
  }

  function chapterIndex(state, at = position(state)) {
    return state.data.chapters.findLastIndex((chapter) => number(chapter.start) <= at);
  }
  function chapterLabel(state, at = position(state)) {
    const index = chapterIndex(state, at);
    return index < 0 ? "Audiobook" : state.data.chapters[index].title || `Chapter ${index + 1}`;
  }

  function closeAudio() {
    ++audioRequest;
    const state = audioState;
    if (state) {
      void report(state, true);
      audioState = null;
      state.media.pause();
      state.media.removeAttribute("src");
      state.media.load();
      state.media.remove();
    }
    if (audioDialog.open) audioDialog.close();
    dock.hidden = true;
    clearMediaSession();
  }

  function expandAudio() {
    if (!audioState) return;
    if (!audioDialog.open) audioDialog.showModal();
    $("#audio-expand").setAttribute("aria-expanded", "true");
    $("#audio-collapse").focus();
  }

  function seekAudio(target, wantPlay = audioState && (!audioState.media.paused || (!audioState.ready && audioState.wantPlay))) {
    const state = audioState;
    if (!state?.data) return;
    const at = clamp(target, state.data.duration);
    const tracks = state.data.tracks;
    let index = tracks.findLastIndex((track) => number(track.offset) <= at);
    if (index < 0) index = 0;
    state.position = at;
    state.finished = false;
    state.wantPlay = Boolean(wantPlay);
    const localTime = clamp(at - number(tracks[index].offset), tracks[index].duration);
    if (index !== state.track || !state.ready) {
      state.ready = false;
      state.pendingSeek = localTime;
      state.track = index;
      state.media.src = tracks[index].url;
      state.media.load();
      state.media.playbackRate = speed;
      // Start inside the user gesture when possible; metadata will apply the seek.
      if (state.wantPlay) void play(state);
    } else {
      state.media.currentTime = localTime;
      if (state.wantPlay) void play(state);
      else state.media.pause();
      void report(state);
    }
    renderAudio();
  }

  async function openAudio(id) {
    if (String(audioState?.data?.item_id) === String(id)) { expandAudio(); return; }
    closeAudio();
    const request = ++audioRequest;
    pauseState(videoState);
    cancelCountdown();
    const media = document.createElement("audio");
    media.preload = "metadata";
    document.body.append(media);
    const state = audioState = stateFor("audio", media);
    state.sleepDeadline = null;
    state.sleepEnd = null;
    dock.hidden = false;
    mountDock();
    scrubbing = false;
    scrubber.disabled = true;
    scrubber.value = 0;
    $("#audio-title").textContent = $("#audio-mini-title").textContent = "Loading audiobook…";
    $("#audio-chapter").textContent = $("#audio-mini-chapter").textContent = "";
    $("#audio-position").textContent = $("#audio-duration").textContent = "0:00:00";
    $("#audio-message").textContent = $("#audio-sleep-status").textContent = "";
    $("#audio-message").dataset.sync = "";
    $("#audio-sleep").value = "off";
    $("#audio-speed").value = String(speed);
    $("#audio-volume").value = volume;
    $("#audio-chapters").replaceChildren();
    for (const image of [$("#audio-cover"), $("#audio-mini-cover")]) { image.hidden = true; image.removeAttribute("src"); }
    $("#audio-sync-note").hidden = true;
    document.querySelectorAll(".audio-toggle").forEach((button) => { button.disabled = true; button.textContent = "Play"; });
    expandAudio();
    preferences(media, "audio");
    media.addEventListener("loadedmetadata", () => {
      if (state !== audioState || !state.data) return;
      media.currentTime = state.pendingSeek ?? 0;
      state.pendingSeek = null;
      state.ready = true;
      state.started = true;
      media.playbackRate = speed;
      if (state.wantPlay) void play(state);
      else { media.pause(); void report(state); }
      renderAudio();
    });
    media.addEventListener("play", () => {
      if (state !== audioState) return;
      state.wantPlay = true;
      state.sleepStopped = false;
      pauseState(videoState);
      renderAudio();
    });
    media.addEventListener("pause", () => {
      if (state !== audioState) return;
      if (state.ready) { state.wantPlay = false; void report(state); }
      renderAudio();
    });
    media.addEventListener("seeked", () => {
      if (state !== audioState || !state.ready) return;
      void report(state);
      renderAudio();
    });
    media.addEventListener("timeupdate", () => { if (state === audioState) { checkSleep(); renderAudio(); } });
    media.addEventListener("ended", () => {
      if (state !== audioState || !state.data || !state.ready) return;
      const endedTrack = state.track;
      const sleeping = checkSleep() || state.sleepStopped;
      if (endedTrack + 1 < state.data.tracks.length) {
        void report(state);
        // checkSleep may already have moved to the next track at a chapter boundary.
        if (state.track === endedTrack) seekAudio(state.data.tracks[endedTrack + 1].offset, !sleeping);
      } else {
        state.finished = true;
        state.position = number(state.data.duration);
        void report(state);
        renderAudio();
      }
    });
    media.addEventListener("error", () => message(state, "This audio track could not be played. Reopen the book to retry, or use Open in Audiobookshelf."));
    try {
      const data = await getInfo("audio", id);
      if (request !== audioRequest) return;
      if (!Array.isArray(data.tracks) || !data.tracks.length || data.tracks.some((track) => !track.url || !(number(track.duration) > 0))) throw new Error("No playable audio tracks were found.");
      data.tracks = [...data.tracks].sort((a, b) => number(a.offset) - number(b.offset));
      data.chapters = [...(data.chapters || [])].sort((a, b) => number(a.start) - number(b.start));
      state.data = data;
      $("#audio-sync-note").hidden = data.progress_sync !== false;
      $("#audio-title").textContent = $("#audio-mini-title").textContent = data.title || "Audiobook";
      for (const image of [$("#audio-cover"), $("#audio-mini-cover")]) {
        image.hidden = !data.cover;
        image.onerror = () => { image.hidden = true; };
        if (data.cover) image.src = data.cover;
      }
      scrubber.max = number(data.duration) || 1;
      scrubber.disabled = !number(data.duration);
      $("#audio-duration").textContent = time(data.duration);
      $("#audio-sleep option[value='chapter']").disabled = !data.chapters.length;
      renderChapters(state);
      setMediaSession(state);
      seekAudio(data.resume, state.wantPlay);
    } catch (error) { message(state, error.message); }
  }

  function renderChapters(state) {
    $("#audio-chapters").replaceChildren();
    state.data.chapters.forEach((chapter, index) => {
      const li = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.chapter = index;
      const title = document.createElement("span");
      title.textContent = chapter.title || `Chapter ${index + 1}`;
      const stamp = document.createElement("span");
      stamp.textContent = time(chapter.start);
      button.append(title, stamp);
      button.addEventListener("click", () => seekAudio(chapter.start));
      li.append(button);
      $("#audio-chapters").append(li);
    });
    if (!state.data.chapters.length) $("#audio-chapters").textContent = "No chapters available for this book.";
  }

  function renderAudio() {
    const state = audioState;
    if (!state?.data) return;
    const at = position(state);
    const chapter = chapterLabel(state, at);
    $("#audio-chapter").textContent = chapter;
    $("#audio-mini-chapter").textContent = $("#audio-message").textContent || `${chapter} · ${time(at)}`;
    if (!scrubbing) { scrubber.value = at; $("#audio-position").textContent = time(at); }
    scrubber.setAttribute("aria-valuetext", `${time(scrubber.value)} of ${time(state.data.duration)}`);
    const playing = !state.media.paused;
    document.querySelectorAll(".audio-toggle").forEach((button) => {
      button.disabled = false;
      button.textContent = playing ? "Pause" : "Play";
      button.setAttribute("aria-label", `${playing ? "Pause" : "Play"} audiobook`);
    });
    const index = chapterIndex(state, at);
    if (index !== state.renderedChapter) {
      state.renderedChapter = index;
      $("#audio-chapters").querySelectorAll("button").forEach((button) => {
        if (Number(button.dataset.chapter) === index) button.setAttribute("aria-current", "true");
        else button.removeAttribute("aria-current");
      });
    }
    if ("mediaSession" in navigator && sessionAudio) {
      navigator.mediaSession.playbackState = playing ? "playing" : "paused";
      try { if (state.data.duration > 0) navigator.mediaSession.setPositionState?.({ duration: state.data.duration, playbackRate: speed, position: at }); } catch { /* Optional browser feature. */ }
    }
  }

  function toggleAudio() {
    const state = audioState;
    if (!state?.data) return;
    if (!state.media.paused) { state.wantPlay = false; state.media.pause(); }
    else if (state.finished || state.media.ended) seekAudio(state.finished ? 0 : position(state), true);
    else { state.wantPlay = true; void play(state); }
  }

  function jumpChapter(direction) {
    const state = audioState;
    if (!state?.data?.chapters.length) return;
    const index = chapterIndex(state);
    const target = Math.max(0, Math.min(state.data.chapters.length - 1, index + direction));
    seekAudio(state.data.chapters[target].start);
  }

  function checkSleep() {
    const state = audioState;
    if (!state?.data) return false;
    const timedOut = state.sleepDeadline !== null && Date.now() >= state.sleepDeadline;
    const chapterEnded = state.sleepEnd !== null && position(state) >= state.sleepEnd;
    if (!timedOut && !chapterEnded) return false;
    const end = state.sleepEnd;
    state.sleepDeadline = state.sleepEnd = null;
    state.sleepStopped = true;
    state.wantPlay = false;
    state.media.pause();
    if (chapterEnded) seekAudio(end, false);
    $("#audio-sleep").value = "off";
    $("#audio-sleep-status").textContent = "Sleep timer ended. Playback paused.";
    void report(state);
    return true;
  }

  const mediaActions = ["play", "pause", "seekbackward", "seekforward", "seekto", "previoustrack", "nexttrack", "stop"];
  function clearMediaSession() {
    sessionAudio = false;
    if (!("mediaSession" in navigator)) return;
    for (const action of mediaActions) { try { navigator.mediaSession.setActionHandler(action, null); } catch { /* Unsupported action. */ } }
    navigator.mediaSession.metadata = null;
    navigator.mediaSession.playbackState = "none";
    try { navigator.mediaSession.setPositionState?.(); } catch { /* Unsupported. */ }
  }
  function setMediaSession(state) {
    sessionAudio = true;
    if (!("mediaSession" in navigator) || !state?.data) return;
    if ("MediaMetadata" in window) {
      try { navigator.mediaSession.metadata = new MediaMetadata({ title: state.data.title || "Audiobook", album: "Omnarr", artwork: state.data.cover ? [{ src: new URL(state.data.cover, location.href).href }] : [] }); } catch { /* Invalid artwork must not interrupt playback. */ }
    }
    const handlers = {
      play: () => { if (audioState?.media.paused) toggleAudio(); },
      pause: () => { if (audioState) { audioState.wantPlay = false; audioState.media.pause(); } },
      seekbackward: (event) => { if (audioState?.data) seekAudio(position(audioState) - (event.seekOffset || 30)); },
      seekforward: (event) => { if (audioState?.data) seekAudio(position(audioState) + (event.seekOffset || 30)); },
      seekto: (event) => seekAudio(event.seekTime),
      previoustrack: () => jumpChapter(-1), nexttrack: () => jumpChapter(1), stop: closeAudio,
    };
    for (const [action, handler] of Object.entries(handlers)) { try { navigator.mediaSession.setActionHandler(action, handler); } catch { /* Safari supports a subset. */ } }
  }

  $("[data-video-close]").addEventListener("click", closeVideo);
  videoDialog.addEventListener("cancel", (event) => { event.preventDefault(); closeVideo(); });
  videoDialog.addEventListener("close", () => { if (!videoDialog.open && videoState) closeVideo(); });
  $("#video-subtitles").addEventListener("change", applySubtitles);
  $("#video-next").addEventListener("click", advanceEpisode);
  $("#video-cancel-next").addEventListener("click", cancelCountdown);
  document.querySelectorAll("[data-video-skip]").forEach((button) => button.addEventListener("click", () => {
    if (videoState?.ready) videoState.media.currentTime = clamp(videoState.media.currentTime + Number(button.dataset.videoSkip), videoState.data.duration);
  }));
  $("#video-pip").addEventListener("click", async () => {
    const state = videoState;
    if (!state?.ready) return;
    try {
      if (document.pictureInPictureElement) await document.exitPictureInPicture();
      else if (state.media.requestPictureInPicture) await state.media.requestPictureInPicture();
      else state.media.webkitSetPresentationMode("picture-in-picture");
    } catch { message(state, "Picture in picture is unavailable for this video."); }
  });
  $("#audio-expand").addEventListener("click", expandAudio);
  $("#audio-collapse").addEventListener("click", () => audioDialog.close());
  audioDialog.addEventListener("close", () => { if (audioDialog.open) return; $("#audio-expand").setAttribute("aria-expanded", "false"); if (!dock.hidden) $("#audio-expand").focus({ preventScroll: true }); });
  document.querySelectorAll("[data-audio-close]").forEach((button) => button.addEventListener("click", closeAudio));
  document.querySelectorAll(".audio-toggle").forEach((button) => button.addEventListener("click", toggleAudio));
  document.querySelectorAll("[data-audio-skip]").forEach((button) => button.addEventListener("click", () => { if (audioState?.data) seekAudio(position(audioState) + Number(button.dataset.audioSkip)); }));
  scrubber.addEventListener("input", () => { scrubbing = true; $("#audio-position").textContent = time(scrubber.value); scrubber.setAttribute("aria-valuetext", time(scrubber.value)); });
  scrubber.addEventListener("change", () => { scrubbing = false; seekAudio(Number(scrubber.value)); });
  scrubber.addEventListener("blur", () => { scrubbing = false; renderAudio(); });
  $("#audio-speed").addEventListener("change", (event) => {
    speed = Number(event.target.value);
    savePreference("speed", speed);
    if (audioState) audioState.media.playbackRate = speed;
    renderAudio();
  });
  $("#audio-volume").addEventListener("input", (event) => {
    volume = Number(event.target.value);
    savePreference("volume", volume);
    if (audioState) audioState.media.volume = volume;
  });
  $("#audio-sleep").addEventListener("change", (event) => {
    const state = audioState;
    if (!state?.data) return;
    state.sleepDeadline = state.sleepEnd = null;
    const value = event.target.value;
    let label = "";
    if (value === "chapter") {
      const chapter = state.data.chapters[chapterIndex(state)];
      state.sleepEnd = chapter ? number(chapter.end) : number(state.data.duration);
      label = `Pauses at the end of ${chapterLabel(state)}.`;
    } else if (value !== "off") {
      state.sleepDeadline = Date.now() + Number(value) * 60000;
      label = `Pauses at ${new Date(state.sleepDeadline).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}.`;
    }
    $("#audio-sleep-status").textContent = label;
  });
  setInterval(checkSleep, 1000);
  setInterval(() => { for (const state of [audioState, videoState]) if (state && !state.media.paused && !state.media.ended) void report(state); }, 20000);
  const flush = () => { void report(audioState, true); void report(videoState, true); };
  window.addEventListener("pagehide", flush);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") flush(); else checkSleep(); });

  return { openVideo, openAudio, closeAll: () => { closeVideo(); closeAudio(); }, audioInfo: (id, signal) => getInfo("audio", id, signal), time };
})();
