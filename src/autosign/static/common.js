"use strict";
// Helpers shared by every AutoSign page.

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const type = response.headers.get("content-type") || "";
  let body;
  if (type.includes("application/json")) {
    body = await response.json();
  } else {
    body = await response.text();
  }
  if (!response.ok) {
    throw new Error(errorMessage(body, response.status));
  }
  return body;
}

function errorMessage(body, status) {
  if (body && typeof body.detail === "string") {
    return body.detail;
  }
  if (body && body.detail) {
    return JSON.stringify(body.detail);
  }
  return `The request failed (HTTP ${status})`;
}

function sendJSON(path, data, method = "POST") {
  return api(path, {
    method: method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

// The token in /a/<token> or /s/<token>.
function tokenFromPath() {
  const parts = location.pathname.split("/").filter(Boolean);
  return decodeURIComponent(parts[1] || "");
}

function esc(value) {
  const text = value === null || value === undefined ? "" : String(value);
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function fullUrl(path) {
  return location.origin + path;
}

function toast(message, bad = false) {
  let box = document.getElementById("toast");
  if (!box) {
    box = document.createElement("div");
    box.id = "toast";
    box.setAttribute("role", "status");
    document.body.appendChild(box);
  }
  box.textContent = message;
  box.className = bad ? "bad" : "";
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.hidden = true; }, bad ? 9000 : 3500);
}

async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (error) {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  if (button) {
    const label = button.textContent;
    button.textContent = "Copied";
    setTimeout(() => { button.textContent = label; }, 1500);
  }
  toast("Link copied. Paste it into your email or message to the client.");
}

const STATUS_TEXT = {
  sent: ["Not opened yet", "warn"],
  viewed: ["Opened, not signed", "warn"],
  signed: ["Signed", "good"],
};

function statusChip(status) {
  const known = STATUS_TEXT[status] || [status, ""];
  return `<span class="chip ${known[1]}">${esc(known[0])}</span>`;
}

// Phones and tablets often can't show a PDF inside a page, so they get page pictures instead.
function prefersPageImages() {
  const touchDevice = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
  const narrow = window.matchMedia("(max-width: 700px)").matches;
  return touchDevice || narrow;
}

function showSignedDocument(container, pdfUrl, pageImageUrl, pageCount) {
  if (prefersPageImages()) {
    const images = [];
    for (let number = 1; number <= pageCount; number += 1) {
      images.push(`<img loading="lazy" src="${esc(pageImageUrl(number))}" alt="Signed page ${number}">`);
    }
    container.innerHTML = `<div class="pages">${images.join("")}</div>`;
  } else {
    container.innerHTML = `<iframe class="pdf-frame" src="${esc(pdfUrl)}#view=FitH" title="The signed agreement"></iframe>`;
  }
}

async function run(task) {
  try {
    await task();
  } catch (error) {
    toast(error.message, true);
  }
}
