(function () {
  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  const csrfToken = csrfMeta ? csrfMeta.getAttribute("content") || "" : "";
  const langPick = document.getElementById("lang-select");
  if (langPick && langPick.form) {
    langPick.addEventListener("change", function () {
      langPick.form.submit();
    });
  }
  document.querySelectorAll("form[method='post' i], form[method='POST']").forEach(function (form) {
    if (!form.querySelector("input[name='csrf_token']") && csrfToken) {
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = "csrf_token";
      input.value = csrfToken;
      form.appendChild(input);
    }
  });

  const photo = document.getElementById("photo");
  const scanStatus = document.getElementById("scan-status");
  const scanToken = document.getElementById("scan_token");
  const saveBtn = document.querySelector("form.stack button[type='submit']");
  let scanAbort = null;
  const MAX_EDGE = 1280;
  const JPEG_Q = 0.72;

  function compressPhoto(file) {
    if (!file || !file.type || file.type.indexOf("image/") !== 0) {
      return Promise.resolve(file);
    }
    if (typeof createImageBitmap !== "function") {
      return Promise.resolve(file);
    }
    return createImageBitmap(file)
      .then(function (bmp) {
        const scale = Math.min(1, MAX_EDGE / Math.max(bmp.width, bmp.height));
        const w = Math.max(1, Math.round(bmp.width * scale));
        const h = Math.max(1, Math.round(bmp.height * scale));
        const canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext("2d");
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = "high";
        ctx.drawImage(bmp, 0, 0, w, h);
        if (bmp.close) bmp.close();
        return new Promise(function (resolve) {
          canvas.toBlob(
            function (blob) {
              resolve(blob || file);
            },
            "image/jpeg",
            JPEG_Q
          );
        });
      })
      .catch(function () {
        return file;
      });
  }

  function assignFile(input, blob, originalName) {
    if (!input || !blob || typeof DataTransfer === "undefined") return;
    const name = (originalName || "document.jpg").replace(/\.[^.]+$/, ".jpg");
    const file = new File([blob], name, { type: "image/jpeg" });
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
  }

  if (photo) {
    photo.addEventListener("change", function () {
      const file = photo.files && photo.files[0];
      const box = document.querySelector(".drop-preview");
      if (!file || !box) return;
      photo.setAttribute("name", "photo");
      if (scanToken) scanToken.value = "";
      if (scanAbort) scanAbort.abort();
      scanAbort = new AbortController();
      const setScan = function (state, extra) {
        if (!scanStatus) return;
        scanStatus.hidden = false;
        scanStatus.className = "scan-status is-" + state;
        let text = scanStatus.getAttribute("data-" + state) || "";
        if (extra) text = text + " " + extra;
        scanStatus.textContent = text;
      };
      if (saveBtn) saveBtn.disabled = true;
      setScan("reading");
      compressPhoto(file).then(function (blob) {
        const previewUrl = URL.createObjectURL(blob);
        box.innerHTML = '<img alt="" src="' + previewUrl + '">';
        assignFile(photo, blob, file.name);
        const body = new FormData();
        body.append("photo", blob, "document.jpg");
        return fetch("/app/scan", {
          method: "POST",
          body: body,
          credentials: "same-origin",
          headers: csrfToken ? { "X-CSRF-Token": csrfToken } : {},
          signal: scanAbort.signal,
        }).then(function (res) {
          if (res.status === 429) {
            return { ok: false, error: "limit" };
          }
          return res.json();
        });
      }).then(function (data) {
        if (saveBtn) saveBtn.disabled = false;
        if (!data) {
          setScan("fail");
          return;
        }
        if (data.token && scanToken) {
          scanToken.value = data.token;
          photo.removeAttribute("name");
        }
        if (!data.ok) {
          setScan(data.error === "off" ? "off" : data.error === "limit" ? "limit" : "fail");
          return;
        }
        const kind = document.getElementById("kind");
        const title = document.getElementById("title");
        const expires = document.getElementById("expires_on");
        if (kind && data.kind) kind.value = data.kind;
        if (title && data.title && !title.value) title.value = data.title;
        if (expires && data.expires_on) {
          expires.value = data.expires_on;
          expires.classList.add("is-suggested");
        }
        setScan("ok");
        if (data.reason) {
          scanStatus.textContent = (scanStatus.getAttribute("data-ok") || "") + " " + data.reason;
        }
      }).catch(function (err) {
        if (saveBtn) saveBtn.disabled = false;
        if (err && err.name === "AbortError") return;
        setScan("fail");
      });
    });
  }

  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm"))) {
        event.preventDefault();
      }
    });
  });

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(function () {});
  }

  let deferredPrompt = null;
  const installBtn = document.getElementById("install-app");
  const installBtnAccount = document.getElementById("install-app-account");
  const iosHint = document.getElementById("ios-hint");
  const iosClose = document.getElementById("ios-hint-close");
  const standalone =
    window.matchMedia("(display-mode: standalone)").matches ||
    window.navigator.standalone === true;

  function showInstallHelp() {
    if (window.location.pathname.indexOf("/app/account") === 0) {
      const box = document.getElementById("install");
      if (box && box.scrollIntoView) box.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    if (iosHint) iosHint.hidden = false;
  }

  function runInstall() {
    if (deferredPrompt) {
      deferredPrompt.prompt();
      deferredPrompt.userChoice.finally(function () {
        deferredPrompt = null;
        if (installBtn) installBtn.hidden = true;
        if (installBtnAccount) installBtnAccount.hidden = true;
      });
      return;
    }
    showInstallHelp();
  }

  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault();
    deferredPrompt = event;
    if (installBtn) installBtn.hidden = false;
    if (installBtnAccount) installBtnAccount.hidden = false;
    if (iosHint) iosHint.hidden = true;
  });
  if (installBtn) installBtn.addEventListener("click", runInstall);
  if (installBtnAccount) installBtnAccount.addEventListener("click", runInstall);
  window.addEventListener("appinstalled", function () {
    if (installBtn) installBtn.hidden = true;
    if (installBtnAccount) installBtnAccount.hidden = true;
    if (iosHint) iosHint.hidden = true;
  });

  if (standalone) {
    if (installBtn) installBtn.hidden = true;
    if (installBtnAccount) installBtnAccount.hidden = true;
    if (iosHint) iosHint.hidden = true;
  }

  if (iosClose && iosHint) {
    iosClose.addEventListener("click", function () {
      iosHint.hidden = true;
      window.sessionStorage.setItem("ios-hint-dismissed", "1");
    });
  }
})();
