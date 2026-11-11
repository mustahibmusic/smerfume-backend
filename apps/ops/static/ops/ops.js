// Smerfume Operations: mobile sidebar toggle only.
(function () {
  "use strict";
  document.addEventListener("DOMContentLoaded", function () {
    var shell = document.querySelector("[data-shell]");
    var toggle = document.querySelector("[data-nav-toggle]");
    if (!shell || !toggle) {
      return;
    }
    function setOpen(open) {
      shell.classList.toggle("is-nav-open", open);
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    }
    toggle.addEventListener("click", function () {
      setOpen(!shell.classList.contains("is-nav-open"));
    });
    document.querySelectorAll("[data-nav-close]").forEach(function (el) {
      el.addEventListener("click", function () { setOpen(false); });
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") { setOpen(false); }
    });
  });
})();
