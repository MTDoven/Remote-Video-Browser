(() => {
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
    previewLoading.textContent = "预览图生成中…";
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

    const videoTitle = button.dataset.videoTitle || "九宫格预览";
    const previewUrl = buildPreviewUrl(baseUrl);

    previewTitle.textContent = `${videoTitle} · 九宫格预览`;
    previewLoading.textContent = "预览图生成中…";
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

    previewLoading.textContent = "预览图加载失败，请重试";
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
})();
