// Smerfume Panel: sidebar toggle, confirmations, single-submit buttons and
// the in-store sale line editor. Progressive only: every page works without
// this file (the server validates everything).
(function () {
  "use strict";

  function initSidebar() {
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
  }

  function initForms() {
    document.addEventListener("submit", function (event) {
      var form = event.target;
      var submitter = event.submitter;
      var message = (submitter && submitter.getAttribute("data-confirm-button")) || form.getAttribute("data-confirm");
      if (message && !window.confirm(message)) {
        event.preventDefault();
        return;
      }
      // Stop double submits; the server-side keys still decide.
      form.querySelectorAll("[data-once]").forEach(function (button) {
        window.setTimeout(function () { button.disabled = true; }, 0);
      });
    });
  }

  function initSale() {
    var form = document.querySelector("[data-sale-form]");
    if (!form) {
      return;
    }
    var lines = form.querySelector("[data-sale-lines]");
    var template = form.querySelector("template[data-sale-empty]");
    var total = form.querySelector("input[name='lines-TOTAL_FORMS']");
    var options = document.getElementById("sale-item-options");
    var warehouse = form.querySelector("select[name='warehouse']");
    var url = form.getAttribute("data-items-url");
    var known = {};
    var timer = null;

    function describe(row) {
      var text = row.text + " · " + row.sku + " · stock " + row.stock + " · ₹" + row.price;
      return row.stock > 0 ? text : text + " · Out of stock";
    }

    function search(query) {
      if (!warehouse || !warehouse.value || query.length < 2) {
        return;
      }
      var params = new URLSearchParams({ q: query, warehouse: warehouse.value });
      fetch(url + "?" + params.toString(), { credentials: "same-origin" })
        .then(function (response) { return response.ok ? response.json() : { results: [] }; })
        .then(function (data) {
          options.innerHTML = "";
          (data.results || []).forEach(function (row) {
            known[row.sku] = row;
            if (row.barcode) { known[row.barcode] = row; }
            var option = document.createElement("option");
            option.value = row.sku;
            option.label = describe(row);
            options.appendChild(option);
          });
        })
        .catch(function () { /* the server validates the SKU on submit */ });
    }

    function showInfo(input) {
      var info = input.closest("[data-sale-line]").querySelector("[data-sale-item-info]");
      var row = known[input.value.trim()];
      if (row && info) {
        info.textContent = describe(row);
        if (input.value.trim() === row.barcode) { input.value = row.sku; }
      }
    }

    function bind(line) {
      var input = line.querySelector("[data-sale-item]");
      var remove = line.querySelector("[data-sale-remove]");
      input.addEventListener("input", function () {
        window.clearTimeout(timer);
        var query = input.value.trim();
        timer = window.setTimeout(function () { search(query); }, 250);
        showInfo(input);
      });
      input.addEventListener("change", function () { showInfo(input); });
      input.addEventListener("keydown", function (event) {
        // A scanner ends with Enter: keep it from submitting the sale.
        if (event.key === "Enter") {
          event.preventDefault();
          var qty = line.querySelector("input[type='number']");
          if (qty && !qty.value) { qty.value = "1"; }
          addLine();
        }
      });
      remove.addEventListener("click", function () {
        line.querySelector("[data-sale-delete]").checked = true;
        line.classList.add("is-removed");
      });
    }

    function addLine() {
      var index = parseInt(total.value, 10);
      var html = template.innerHTML.replace(/__prefix__/g, String(index));
      var wrapper = document.createElement("div");
      wrapper.innerHTML = html.trim();
      var line = wrapper.firstElementChild;
      lines.appendChild(line);
      total.value = String(index + 1);
      bind(line);
      line.querySelector("[data-sale-item]").focus();
    }

    lines.querySelectorAll("[data-sale-line]").forEach(bind);
    form.querySelector("[data-sale-add]").addEventListener("click", addLine);
  }

  document.addEventListener("DOMContentLoaded", function () {
    initSidebar();
    initForms();
    initSale();
  });
})();
