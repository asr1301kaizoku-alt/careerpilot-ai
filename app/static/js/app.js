(() => {
  "use strict";

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("form");
    if (!form) return;
    const submitter = event.submitter || form.querySelector('[type="submit"]');
    const loadingText = submitter?.dataset.loadingText || form.dataset.loadingText;
    if (!loadingText) return;
    if (form.dataset.submitting === "true") {
      event.preventDefault();
      return;
    }

    form.dataset.submitting = "true";
    form.setAttribute("aria-busy", "true");
    if (!submitter) return;
    submitter.dataset.originalText = submitter.value || submitter.textContent;
    if (submitter.tagName === "INPUT") submitter.value = loadingText;
    else submitter.textContent = loadingText;
    window.setTimeout(() => {
      submitter.disabled = true;
    }, 0);
  });

  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-loading-text]");
    if (!link) return;
    if (link.getAttribute("aria-disabled") === "true") {
      event.preventDefault();
      return;
    }
    link.setAttribute("aria-disabled", "true");
    link.setAttribute("aria-busy", "true");
    link.classList.add("disabled");
    link.textContent = link.dataset.loadingText;
  });
})();
