const menu = document.querySelector("#menu");
menu?.addEventListener("click", () => {
  const visible = document.querySelector("#sidebar").classList.toggle("visible");
  menu.setAttribute("aria-expanded", visible);
});

const drawer = document.querySelector("#creator-drawer");
document.querySelectorAll("[data-drawer]").forEach(link => link.addEventListener("click", event => {
  if (!drawer || !drawer.showModal || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  drawer.querySelector("iframe").src = link.href + "?drawer=1";
  drawer.querySelector("#full-profile").href = link.href;
  drawer.showModal();
}));
drawer?.addEventListener("close", () => drawer.querySelector("iframe").removeAttribute("src"));

const preview = document.querySelector("#post-preview");
document.querySelectorAll("[data-post-preview]").forEach(button => button.addEventListener("click", () => {
  if (!preview || !preview.showModal) return;
  const url = button.dataset.url;
  const mediaUrl = button.dataset.mediaUrl;
  const mediaType = button.dataset.mediaType || "";
  const frame = preview.querySelector("iframe");
  const img = preview.querySelector("img");
  const video = preview.querySelector("video");
  preview.querySelector("#post-preview-title").textContent = button.dataset.title || "Post";
  preview.querySelector("#open-original").href = url;
  frame.hidden = true; frame.removeAttribute("src");
  img.hidden = true; img.removeAttribute("src");
  video.hidden = true; video.removeAttribute("src");
  if (mediaUrl && mediaType.startsWith("video/")) {
    video.src = mediaUrl; video.hidden = false;
  } else if (mediaUrl && mediaType.startsWith("image/")) {
    img.src = mediaUrl; img.hidden = false;
  } else if (/\.(jpg|jpeg|png|webp|gif)(\?|#|$)/i.test(url)) {
    img.src = url; img.hidden = false;
  } else {
    frame.src = url; frame.hidden = false;
  }
  preview.showModal();
}));
preview?.addEventListener("close", () => {
  preview.querySelector("iframe").removeAttribute("src");
  preview.querySelector("img").removeAttribute("src");
  preview.querySelector("video").removeAttribute("src");
});

document.querySelectorAll("[data-copy]").forEach(button => button.addEventListener("click", async () => {
  const source = document.getElementById(button.dataset.copy);
  try {
    await navigator.clipboard.writeText(source.value);
    button.textContent = "Copied";
  } catch {
    source.select();
    button.textContent = "Text selected";
  }
}));

document.querySelectorAll("[data-confirm]").forEach(button => button.addEventListener("click", event => {
  if (!window.confirm(button.dataset.confirm)) event.preventDefault();
}));

document.querySelectorAll("[data-columns]").forEach(panel => {
  const table = document.getElementById(panel.dataset.columns);
  panel.querySelectorAll("[data-column]").forEach(input => {
    const key = `kol-columns-${panel.dataset.columns}-${input.dataset.column}`;
    const update = () => table.querySelectorAll(`[data-col="${input.dataset.column}"]`)
      .forEach(cell => cell.hidden = !input.checked);
    try { input.checked = localStorage.getItem(key) !== "false"; } catch {}
    update();
    input.addEventListener("change", () => {
      update();
      try { localStorage.setItem(key, input.checked); } catch {}
    });
  });
});

if (window.self !== window.top) document.body.classList.add("embedded");
document.querySelector("#print")?.addEventListener("click", () => window.print());

document.querySelectorAll("[data-save-filters]").forEach(form => {
  const key = `kol-filters-${form.dataset.saveFilters}`;
  form.querySelector("[data-store-filter]")?.addEventListener("click", event => {
    try {
      localStorage.setItem(key, new URLSearchParams(new FormData(form)).toString());
      event.currentTarget.textContent = "Filters saved";
    } catch {
      event.currentTarget.textContent = "Unable to save";
    }
  });
  form.querySelector("[data-load-filter]")?.addEventListener("click", event => {
    try {
      const filters = localStorage.getItem(key);
      if (filters !== null) window.location.search = filters;
      else event.currentTarget.textContent = "No saved filters";
    } catch {
      event.currentTarget.textContent = "Unable to load";
    }
  });
});
