(() => {
  const player = document.querySelector(".player-video");
  if (player) {
    void configurePlayback(player);
  }

  const modal = document.querySelector(".preview-modal");
  if (!modal) {
    return;
  }

  const previewImage = modal.querySelector(".preview-image");
  const previewLoading = modal.querySelector(".preview-loading");
  const previewTitle = modal.querySelector(".preview-title");
  const closeTargets = modal.querySelectorAll("[data-preview-close]");
  const previewButtons = document.querySelectorAll(".preview-button");
  const warmedPreviews = new Set();
  let currentPreviewUrl = "";

  function openModal() {
    modal.hidden = false;
    modal.setAttribute("aria-hidden", "false");
    document.body.classList.add("preview-open");
  }

  function closeModal() {
    modal.hidden = true;
    modal.setAttribute("aria-hidden", "true");
    document.body.classList.remove("preview-open");
    currentPreviewUrl = "";
    previewImage.removeAttribute("src");
    previewImage.hidden = true;
    previewLoading.hidden = false;
    previewLoading.textContent = "Generating preview...";
  }

  function buildPreviewUrl(baseUrl) {
    const url = new URL(baseUrl, window.location.href);
    url.searchParams.set("seed", `${Date.now()}-${Math.random().toString(16).slice(2)}`);
    return url.toString();
  }

  function warmPreview(baseUrl) {
    if (!baseUrl || warmedPreviews.has(baseUrl)) {
      return;
    }

    warmedPreviews.add(baseUrl);
    const warmUrl = new URL(baseUrl, window.location.href);
    warmUrl.searchParams.set("seed", "warm");
    const image = new Image();
    image.decoding = "async";
    image.referrerPolicy = "same-origin";
    image.src = warmUrl.toString();
  }

  function showPreview(button) {
    const baseUrl = button.dataset.previewUrl;
    if (!baseUrl) {
      return;
    }

    const videoTitle = button.dataset.videoTitle || "Detailed preview";
    const previewUrl = buildPreviewUrl(baseUrl);

    previewTitle.textContent = `${videoTitle} · Detailed preview`;
    previewLoading.textContent = "Generating preview...";
    previewLoading.hidden = false;
    previewImage.hidden = true;
    currentPreviewUrl = previewUrl;
    openModal();
    previewImage.src = previewUrl;
  }

  previewImage.addEventListener("load", () => {
    if (!currentPreviewUrl) {
      return;
    }

    previewLoading.textContent = "";
    previewLoading.hidden = true;
    previewImage.hidden = false;
  });

  previewImage.addEventListener("error", () => {
    if (!currentPreviewUrl) {
      return;
    }

    previewLoading.textContent = "Preview failed to load. Please try again.";
    previewLoading.hidden = false;
    previewImage.hidden = true;
  });

  previewButtons.forEach((button) => {
    button.addEventListener("click", () => showPreview(button));
    button.addEventListener("pointerenter", () => warmPreview(button.dataset.previewUrl || ""));
    button.addEventListener("focus", () => warmPreview(button.dataset.previewUrl || ""));
  });

  closeTargets.forEach((target) => {
    target.addEventListener("click", closeModal);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !modal.hidden) {
      closeModal();
    }
  });

  modal.addEventListener("click", (event) => {
    if (event.target === modal) {
      closeModal();
    }
  });

  async function configurePlayback(video) {
    const directUrl = video.dataset.directMediaUrl || video.getAttribute("src") || "";
    const prepareUrl = video.dataset.prepareMediaUrl || "";
    const av1StreamUrl = video.dataset.av1StreamUrl || "";
    const sourceBitrateBps = Number(video.dataset.sourceBitrateBps || "0");
    const sourceDurationSeconds = Number(video.dataset.sourceDurationSeconds || "0");
    const forceEncodeAv1 = video.dataset.forceEncodeAv1 === "1";
    const av1Supported = supportsAv1Playback(video);
    const av1MseSupported = supportsAv1MediaSource();
    const bandwidthBps = estimateBandwidthBps();
    const shouldUseAv1 =
      Boolean(av1StreamUrl) &&
      av1Supported &&
      (forceEncodeAv1 || (sourceBitrateBps > 0 && bandwidthBps !== null && sourceBitrateBps > bandwidthBps * 0.75));

    if (!shouldUseAv1) {
      if (directUrl) {
        try {
          await prepareMedia(video, prepareUrl);
        } catch (error) {
          showPrepareStatus(video, "Preparation failed", 100, error.message || "Unable to prepare media");
          return;
        }
        playDirectStream(video, directUrl);
      }
      return;
    }

    const streamUrl = new URL(av1StreamUrl, window.location.href);
    const chosenBandwidth = bandwidthBps ?? sourceBitrateBps;
    if (chosenBandwidth && chosenBandwidth > 0) {
      streamUrl.searchParams.set("bandwidth_bps", String(Math.max(1, Math.round(chosenBandwidth))));
    }

    if (av1MseSupported && Number.isFinite(sourceDurationSeconds) && sourceDurationSeconds > 0) {
      try {
        const controller = await playAv1WithMediaSource(video, streamUrl, sourceDurationSeconds);
        installAv1Fallback(video, controller, directUrl);
        return;
      } catch (error) {
        console.warn("Falling back from AV1 MSE playback:", error);
      }
    }

    try {
      showPrepareStatus(video, "Preparing AV1 stream", 5, "Starting realtime transcode");
      await preloadAv1Stream(streamUrl, 0, video);
    } catch (error) {
      if (directUrl) {
        try {
          await prepareMedia(video, prepareUrl);
        } catch (prepareError) {
          showPrepareStatus(video, "Preparation failed", 100, prepareError.message || "Unable to prepare media");
          return;
        }
        playDirectStream(video, directUrl, 0, false, "Loading fallback media stream");
      }
      return;
    }

    installPlaybackLoadStatus(video, "Loading AV1 stream", "Waiting for browser playback buffer");
    video.preload = "auto";
    video.src = streamUrl.toString();
    video.load();
  }

  async function prepareMedia(video, prepareUrl) {
    if (!prepareUrl) {
      return;
    }

    showPrepareStatus(video, "Preparing media", 1, "");
    while (true) {
      const response = await fetch(prepareUrl, {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!response.ok) {
        throw new Error(`Media preparation failed: ${response.status}`);
      }

      const status = await response.json();
      showPrepareStatus(video, status.phase || "Preparing media", status.progress || 0, status.detail || "");
      if (status.state === "complete") {
        return;
      }
      if (status.state === "error") {
        throw new Error(status.error || "Media preparation failed");
      }
      await sleep(350);
    }
  }

  function showPrepareStatus(video, phase, progress, detail = "") {
    const status = video.closest(".player-frame")?.querySelector(".prepare-status");
    if (!status) {
      return;
    }

    const normalizedProgress = Math.max(0, Math.min(100, Number(progress) || 0));
    const progressLabel = normalizedProgress >= 100 ? "100%" : `${normalizedProgress.toFixed(1).replace(/\.0$/, "")}%`;
    status.hidden = false;
    const phaseTarget = status.querySelector(".prepare-phase");
    const percentTarget = status.querySelector(".prepare-percent");
    const fillTarget = status.querySelector(".prepare-meter-fill");
    const detailTarget = status.querySelector(".prepare-detail");
    if (phaseTarget) {
      phaseTarget.textContent = phase;
    }
    if (percentTarget) {
      percentTarget.textContent = progressLabel;
    }
    if (fillTarget) {
      fillTarget.style.width = `${normalizedProgress}%`;
    }
    if (detailTarget) {
      detailTarget.textContent = detail;
    }
  }

  function hidePrepareStatus(video) {
    const status = video.closest(".player-frame")?.querySelector(".prepare-status");
    if (status) {
      status.hidden = true;
    }
  }

  function installPlaybackLoadStatus(video, phase, detail = "") {
    if (video.prepareLoadCleanup) {
      video.prepareLoadCleanup();
    }

    let finished = false;
    let lastProgress = 95.5;
    const startedAt = performance.now();
    let pollTimer = 0;
    const cleanup = () => {
      if (pollTimer) {
        window.clearInterval(pollTimer);
        pollTimer = 0;
      }
      video.removeEventListener("loadstart", update);
      video.removeEventListener("durationchange", update);
      video.removeEventListener("loadedmetadata", update);
      video.removeEventListener("loadeddata", update);
      video.removeEventListener("progress", update);
      video.removeEventListener("waiting", update);
      video.removeEventListener("stalled", update);
      video.removeEventListener("canplay", ready);
      video.removeEventListener("canplaythrough", ready);
      video.removeEventListener("playing", ready);
      video.removeEventListener("error", failed);
      video.prepareLoadCleanup = null;
    };
    const update = (event = null) => {
      if (finished) {
        return;
      }
      if (video.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA) {
        ready();
        return;
      }
      const browserStatus = browserLoadStatus(video, event, startedAt, detail);
      lastProgress = Math.max(lastProgress, browserStatus.progress);
      showPrepareStatus(video, browserStatus.phase || phase, lastProgress, browserStatus.detail);
    };
    const ready = () => {
      if (finished) {
        return;
      }
      finished = true;
      showPrepareStatus(video, "Ready for playback", 100, "");
      window.setTimeout(() => hidePrepareStatus(video), 180);
      cleanup();
    };
    const failed = () => {
      if (finished) {
        return;
      }
      finished = true;
      showPrepareStatus(video, "Browser playback failed", 100, "The prepared stream could not be decoded");
      cleanup();
    };

    video.prepareLoadCleanup = cleanup;
    video.addEventListener("loadstart", update);
    video.addEventListener("durationchange", update);
    video.addEventListener("loadedmetadata", update);
    video.addEventListener("loadeddata", update);
    video.addEventListener("progress", update);
    video.addEventListener("waiting", update);
    video.addEventListener("stalled", update);
    video.addEventListener("canplay", ready);
    video.addEventListener("canplaythrough", ready);
    video.addEventListener("playing", ready);
    video.addEventListener("error", failed);
    pollTimer = window.setInterval(update, 500);
    update();
  }

  function browserLoadStatus(video, event, startedAt, fallbackDetail) {
    const readyState = video.readyState;
    const bufferedSeconds = bufferedAheadSeconds(video);
    const duration = Number.isFinite(video.duration) ? video.duration : 0;
    const elapsedSeconds = Math.max(0, (performance.now() - startedAt) / 1000);
    const eventType = event?.type || "poll";
    let phase = "Loading media stream";
    let progress = 96;

    if (eventType === "loadstart" || readyState === HTMLMediaElement.HAVE_NOTHING) {
      phase = "Requesting media stream";
      progress = 96;
    } else if (readyState === HTMLMediaElement.HAVE_METADATA) {
      phase = "Reading media metadata";
      progress = 97;
    } else if (readyState === HTMLMediaElement.HAVE_CURRENT_DATA) {
      phase = "Buffering first frame";
      progress = 98;
    } else {
      phase = "Waiting for playback buffer";
      progress = 98.5;
    }

    if (bufferedSeconds > 0) {
      const bufferedRatio = duration > 0 ? Math.min(bufferedSeconds / Math.min(duration, 8), 1) : Math.min(bufferedSeconds / 3, 1);
      progress = Math.max(progress, 98 + bufferedRatio);
    }
    if (eventType === "stalled" || eventType === "waiting") {
      phase = "Waiting for more buffered data";
      progress = Math.max(progress, 98.4);
    }

    const detailParts = [];
    if (fallbackDetail) {
      detailParts.push(fallbackDetail);
    }
    detailParts.push(`readyState=${readyStateName(readyState)}`);
    detailParts.push(`networkState=${networkStateName(video.networkState)}`);
    if (bufferedSeconds > 0) {
      detailParts.push(`buffered=${bufferedSeconds.toFixed(1)}s`);
    }
    if (duration > 0) {
      detailParts.push(`duration=${formatSeconds(duration)}`);
    }
    detailParts.push(`elapsed=${elapsedSeconds.toFixed(1)}s`);

    return { phase, progress: Math.min(progress, 99.4), detail: detailParts.join(" · ") };
  }

  function bufferedAheadSeconds(video) {
    if (!video.buffered || video.buffered.length === 0) {
      return 0;
    }

    const currentTime = Number.isFinite(video.currentTime) ? video.currentTime : 0;
    for (let index = 0; index < video.buffered.length; index += 1) {
      const start = video.buffered.start(index);
      const end = video.buffered.end(index);
      if (currentTime >= start - 0.25 && currentTime <= end + 0.25) {
        return Math.max(0, end - Math.max(start, currentTime));
      }
    }

    return Math.max(0, video.buffered.end(video.buffered.length - 1) - video.buffered.start(0));
  }

  function readyStateName(value) {
    return ["nothing", "metadata", "current-data", "future-data", "enough-data"][value] || String(value);
  }

  function networkStateName(value) {
    return ["empty", "idle", "loading", "no-source"][value] || String(value);
  }

  function formatSeconds(seconds) {
    if (!Number.isFinite(seconds) || seconds <= 0) {
      return "0:00";
    }
    const rounded = Math.round(seconds);
    const minutes = Math.floor(rounded / 60);
    const remainingSeconds = String(rounded % 60).padStart(2, "0");
    return `${minutes}:${remainingSeconds}`;
  }

  function sleep(milliseconds) {
    return new Promise((resolve) => {
      window.setTimeout(resolve, milliseconds);
    });
  }

  function playDirectStream(video, directUrl, resumeSeconds = 0, resumePlayback = false, loadingDetail = "Loading prepared media stream") {
    installPlaybackLoadStatus(video, "Loading media stream", loadingDetail);
    video.preload = "auto";
    video.src = directUrl;
    if (resumeSeconds > 0) {
      const restorePosition = () => {
        video.removeEventListener("loadedmetadata", restorePosition);
        try {
          video.currentTime = resumeSeconds;
        } catch (error) {
          console.warn("Unable to restore playback position:", error);
        }
        if (resumePlayback) {
          void video.play().catch(() => {});
        }
      };
      video.addEventListener("loadedmetadata", restorePosition);
    }
    video.load();
  }

  function installAv1Fallback(video, controller, directUrl) {
    if (!directUrl) {
      return;
    }

    let fallbackTimer = 0;
    const clearFallbackTimer = () => {
      if (fallbackTimer) {
        window.clearTimeout(fallbackTimer);
        fallbackTimer = 0;
      }
    };

    const fallback = (reason) => {
      if (!controller.active) {
        return;
      }
      const resumeSeconds = video.currentTime || 0;
      const resumePlayback = !video.paused;
      cleanup();
      console.warn(`Falling back to direct playback after AV1 ${reason}`);
      controller.cleanup();
      playDirectStream(video, directUrl, resumeSeconds, resumePlayback);
    };

    const scheduleFallback = (event) => {
      if (fallbackTimer || video.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA) {
        return;
      }
      const stalledAt = video.currentTime || 0;
      fallbackTimer = window.setTimeout(() => {
        fallbackTimer = 0;
        if (controller.active && video.readyState < HTMLMediaElement.HAVE_FUTURE_DATA && Math.abs((video.currentTime || 0) - stalledAt) < 0.25) {
          fallback(event.type);
        }
      }, 8000);
    };

    const cleanup = () => {
      clearFallbackTimer();
      video.removeEventListener("error", onError);
      video.removeEventListener("stalled", scheduleFallback);
      video.removeEventListener("waiting", scheduleFallback);
      video.removeEventListener("playing", clearFallbackTimer);
      video.removeEventListener("canplay", clearFallbackTimer);
      video.removeEventListener("av1streamerror", onAv1StreamError);
    };

    const onError = () => fallback("error");
    const onAv1StreamError = () => fallback("stream error");

    video.addEventListener("error", onError);
    video.addEventListener("stalled", scheduleFallback);
    video.addEventListener("waiting", scheduleFallback);
    video.addEventListener("playing", clearFallbackTimer);
    video.addEventListener("canplay", clearFallbackTimer);
    video.addEventListener("av1streamerror", onAv1StreamError);
  }

  async function preloadAv1Stream(streamUrl, startSeconds, video = null) {
    const preloadUrl = new URL(streamUrl.toString());
    preloadUrl.searchParams.set("preload", "1");
    preloadUrl.searchParams.set("start_seconds", String(Math.max(0, startSeconds || 0)));
    const response = await fetch(preloadUrl.toString(), {
      credentials: "same-origin",
      cache: "no-store",
    });

    if (!response.ok) {
      throw new Error(`AV1 preload failed: ${response.status}`);
    }

    const payload = await response.json();
    if (video) {
      const threshold = Number(payload.preload_threshold || "0");
      const bytesWritten = Number(payload.bytes_written || "0");
      const progress = threshold > 0 ? Math.min(95, Math.round((bytesWritten / threshold) * 90)) : 50;
      showPrepareStatus(video, "Preparing AV1 stream", progress, "Buffering realtime transcode");
    }
  }

  function supportsAv1MediaSource() {
    if (typeof MediaSource === "undefined" || typeof MediaSource.isTypeSupported !== "function") {
      return false;
    }

    return MediaSource.isTypeSupported('video/mp4; codecs="av01.0.08M.08, mp4a.40.2"');
  }

  async function playAv1WithMediaSource(video, streamUrl, durationSeconds) {
    const mimeType = 'video/mp4; codecs="av01.0.08M.08, mp4a.40.2"';
    const controller = {
      active: true,
      abortController: null,
      mediaSource: null,
      objectUrl: "",
      seekHandler: null,
      currentStartSeconds: 0,
      initializing: false,
      cleanup: null,
    };

    const cleanup = () => {
      controller.active = false;
      if (controller.abortController) {
        controller.abortController.abort();
      }
      if (controller.seekHandler) {
        video.removeEventListener("seeked", controller.seekHandler);
      }
      if (controller.objectUrl) {
        URL.revokeObjectURL(controller.objectUrl);
        controller.objectUrl = "";
      }
    };
    controller.cleanup = cleanup;

    const attachSession = async (startSeconds, resumePlayback) => {
      if (!controller.active) {
        return;
      }

      controller.initializing = true;
      controller.currentStartSeconds = Math.max(0, startSeconds || 0);
      showPrepareStatus(video, "Preparing AV1 stream", 5, "Starting realtime transcode");
      await preloadAv1Stream(streamUrl, controller.currentStartSeconds, video);
      if (controller.abortController) {
        controller.abortController.abort();
      }
      if (controller.objectUrl) {
        URL.revokeObjectURL(controller.objectUrl);
      }

      controller.abortController = new AbortController();
      const abortSignal = controller.abortController.signal;
      const mediaSource = new MediaSource();
      controller.mediaSource = mediaSource;
      controller.objectUrl = URL.createObjectURL(mediaSource);
      installPlaybackLoadStatus(video, "Loading AV1 stream", "Waiting for browser playback buffer");
      video.src = controller.objectUrl;
      video.load();

      await new Promise((resolve, reject) => {
        const onOpen = () => {
          mediaSource.removeEventListener("sourceopen", onOpen);
          mediaSource.removeEventListener("error", onError);
          resolve();
        };
        const onError = () => {
          mediaSource.removeEventListener("sourceopen", onOpen);
          mediaSource.removeEventListener("error", onError);
          reject(new Error("MediaSource failed to open"));
        };
        mediaSource.addEventListener("sourceopen", onOpen);
        mediaSource.addEventListener("error", onError);
      });

      if (!controller.active || abortSignal.aborted) {
        return;
      }

      const sourceBuffer = mediaSource.addSourceBuffer(mimeType);
      sourceBuffer.mode = "segments";
      if (Number.isFinite(durationSeconds) && durationSeconds > 0) {
        mediaSource.duration = durationSeconds;
      }
      if (controller.currentStartSeconds > 0) {
        sourceBuffer.timestampOffset = controller.currentStartSeconds;
      }
      video.currentTime = controller.currentStartSeconds;
      if (resumePlayback) {
        void video.play().catch(() => {});
      }

      queueMicrotask(() => {
        controller.initializing = false;
      });
      void pumpAv1Session({
        mediaSource,
        sourceBuffer,
        abortSignal,
        startSeconds: controller.currentStartSeconds,
        streamUrl,
      }).catch((error) => {
        if (!abortSignal.aborted) {
          console.warn("AV1 MSE session failed:", error);
          video.dispatchEvent(new Event("av1streamerror"));
        }
      });
    };

    controller.seekHandler = () => {
      if (!controller.active || controller.initializing) {
        return;
      }

      const targetSeconds = video.currentTime;
      if (bufferedContains(video.buffered, targetSeconds, 0.5)) {
        return;
      }

      void attachSession(targetSeconds, !video.paused);
    };

    video.addEventListener("seeked", controller.seekHandler);

    try {
      await attachSession(0, !video.paused);
    } catch (error) {
      cleanup();
      throw error;
    }

    return controller;
  }

  function buildAv1StreamRequestUrl(streamUrl, startSeconds) {
    const requestUrl = new URL(streamUrl.toString());
    requestUrl.searchParams.set("start_seconds", String(Math.max(0, startSeconds || 0)));
    return requestUrl.toString();
  }

  async function appendSourceBufferChunk(sourceBuffer, chunk, abortSignal) {
    if (sourceBuffer.updating) {
      await waitForSourceBufferEvent(sourceBuffer, "updateend", abortSignal);
    }

    sourceBuffer.appendBuffer(chunk);
    await waitForSourceBufferEvent(sourceBuffer, "updateend", abortSignal);
  }

  async function pumpAv1Session({ mediaSource, sourceBuffer, abortSignal, startSeconds, streamUrl }) {
    const response = await fetch(buildAv1StreamRequestUrl(streamUrl, startSeconds), {
      credentials: "same-origin",
      cache: "no-store",
      signal: abortSignal,
    });
    if (!response.ok || !response.body) {
      throw new Error(`AV1 stream failed: ${response.status}`);
    }

    const reader = response.body.getReader();
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done || abortSignal.aborted) {
          break;
        }
        await appendSourceBufferChunk(sourceBuffer, value, abortSignal);
      }
      if (mediaSource.readyState === "open") {
        try {
          mediaSource.endOfStream();
        } catch (error) {
          console.warn("Unable to end AV1 media source:", error);
        }
      }
    } finally {
      reader.releaseLock();
    }
  }

  function waitForSourceBufferEvent(target, eventName, abortSignal) {
    return new Promise((resolve, reject) => {
      const cleanup = () => {
        target.removeEventListener(eventName, onEvent);
        target.removeEventListener("error", onError);
        target.removeEventListener("abort", onError);
        if (abortSignal) {
          abortSignal.removeEventListener("abort", onAbort);
        }
      };

      const onEvent = () => {
        cleanup();
        resolve();
      };

      const onError = () => {
        cleanup();
        reject(new Error(`SourceBuffer event ${eventName} failed`));
      };

      const onAbort = () => {
        cleanup();
        reject(new DOMException("Aborted", "AbortError"));
      };

      target.addEventListener(eventName, onEvent, { once: true });
      target.addEventListener("error", onError, { once: true });
      target.addEventListener("abort", onError, { once: true });
      if (abortSignal) {
        abortSignal.addEventListener("abort", onAbort, { once: true });
      }
    });
  }

  function bufferedContains(timeRanges, time, slackSeconds) {
    if (!timeRanges || timeRanges.length === 0) {
      return false;
    }

    for (let index = 0; index < timeRanges.length; index += 1) {
      const start = timeRanges.start(index);
      const end = timeRanges.end(index);
      if (time >= start - slackSeconds && time <= end + slackSeconds) {
        return true;
      }
    }

    return false;
  }

  function estimateBandwidthBps() {
    const connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
    if (!connection || typeof connection.downlink !== "number" || Number.isNaN(connection.downlink)) {
      return null;
    }

    return Math.max(0, connection.downlink * 1_000_000);
  }

  function supportsAv1Playback(video) {
    const mimeType = 'video/mp4; codecs="av01.0.08M.08, mp4a.40.2"';
    if (typeof video.canPlayType === "function") {
      return video.canPlayType(mimeType) !== "";
    }

    return false;
  }
})();
