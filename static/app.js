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
      await preloadAv1Stream(streamUrl, 0);
    } catch (error) {
      if (directUrl) {
        playDirectStream(video, directUrl);
      }
      return;
    }

    video.preload = "auto";
    video.src = streamUrl.toString();
    video.load();
  }

  function playDirectStream(video, directUrl, resumeSeconds = 0, resumePlayback = false) {
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

  async function preloadAv1Stream(streamUrl, startSeconds) {
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

    await response.json();
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
      await preloadAv1Stream(streamUrl, controller.currentStartSeconds);
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
