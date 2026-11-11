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
      var wasOpen = shell.classList.contains("is-nav-open");
      shell.classList.toggle("is-nav-open", open);
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) {
        var target = document.querySelector("#ops-sidebar .nav__link.is-active") || document.querySelector("#ops-sidebar a");
        if (target) { target.focus(); }
      } else if (wasOpen) {
        toggle.focus();
      }
    }
    toggle.addEventListener("click", function () {
      setOpen(!shell.classList.contains("is-nav-open"));
    });
    document.querySelectorAll("[data-nav-close]").forEach(function (el) {
      el.addEventListener("click", function () { setOpen(false); });
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && shell.classList.contains("is-nav-open")) { setOpen(false); }
    });
  }

  // Theme: Light / Dark / System. Saved per browser in localStorage
  // ("ops-theme"); System (the default) removes data-theme so the CSS
  // follows prefers-color-scheme. base.html applies it before first paint.
  var THEME_KEY = "ops-theme";

  function readTheme() {
    try {
      var value = window.localStorage.getItem(THEME_KEY);
      return value === "light" || value === "dark" ? value : "system";
    } catch (e) {
      return "system";
    }
  }

  function applyTheme(theme) {
    if (theme === "light" || theme === "dark") {
      document.documentElement.setAttribute("data-theme", theme);
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
    document.querySelectorAll("[data-theme-set]").forEach(function (button) {
      button.setAttribute("aria-pressed", button.getAttribute("data-theme-set") === theme ? "true" : "false");
    });
  }

  function initTheme() {
    applyTheme(readTheme());
    document.addEventListener("click", function (event) {
      var button = event.target.closest("[data-theme-set]");
      if (!button) {
        return;
      }
      var theme = button.getAttribute("data-theme-set");
      try {
        window.localStorage.setItem(THEME_KEY, theme);
      } catch (e) { /* private mode: applies to this page only */ }
      applyTheme(theme);
    });
  }

  // Disclosure menus (account menu, row three-dot menus): one open at a
  // time, close on outside click or Escape (focus back to the trigger).
  function initMenus() {
    function menus() {
      return document.querySelectorAll("details[data-menu][open], details.row-menu[open]");
    }
    document.addEventListener("click", function (event) {
      menus().forEach(function (menu) {
        if (!menu.contains(event.target)) { menu.open = false; }
      });
    });
    document.addEventListener("toggle", function (event) {
      var opened = event.target;
      if (!opened.open || !opened.matches || !opened.matches("details[data-menu], details.row-menu")) {
        return;
      }
      menus().forEach(function (menu) {
        if (menu !== opened) { menu.open = false; }
      });
      // Row menus live in scrollable table cards: float the list in the
      // viewport (fixed) so the card cannot clip it; open upward near the bottom.
      var list = opened.querySelector(".row-menu__list");
      if (list) {
        var rect = opened.querySelector("summary").getBoundingClientRect();
        list.style.position = "fixed";
        list.style.right = Math.max(8, window.innerWidth - rect.right) + "px";
        list.style.top = rect.bottom + 4 + "px";
        list.style.bottom = "auto";
        var height = list.offsetHeight;
        if (rect.bottom + 4 + height > window.innerHeight && rect.top - 4 - height > 0) {
          list.style.top = rect.top - 4 - height + "px";
        }
      }
    }, true);
    window.addEventListener("scroll", function () {
      document.querySelectorAll("details.row-menu[open]").forEach(function (menu) { menu.open = false; });
    }, { passive: true });
    window.addEventListener("resize", function () {
      document.querySelectorAll("details.row-menu[open]").forEach(function (menu) { menu.open = false; });
    });
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape") {
        return;
      }
      menus().forEach(function (menu) {
        menu.open = false;
        var summary = menu.querySelector("summary");
        if (summary && menu.contains(document.activeElement)) { summary.focus(); }
      });
    });
  }

  // Phones: list filters fold behind a "Filters" button; search stays
  // visible. Opened automatically when a filter other than search is set.
  function initFilterToggle() {
    document.querySelectorAll("form.filters[method='get']").forEach(function (form, index) {
      var actions = form.querySelector(".filters__actions");
      var extra = Array.prototype.slice.call(form.querySelectorAll(":scope > .field")).slice(1);
      if (!actions || !extra.length) {
        return;
      }
      var active = extra.filter(function (field) {
        var input = field.querySelector("input, select");
        return input && (input.type === "checkbox" ? input.checked : input.value);
      }).length;
      var id = "filters-more-" + index;
      extra[0].id = extra[0].id || id;
      var button = document.createElement("button");
      button.type = "button";
      button.className = "button button--ghost filters__toggle";
      button.setAttribute("aria-expanded", "false");
      button.setAttribute("aria-controls", extra[0].id);
      button.textContent = active ? "Filters (" + active + ")" : "Filters";
      button.addEventListener("click", function () {
        var open = form.classList.toggle("is-expanded");
        button.setAttribute("aria-expanded", open ? "true" : "false");
      });
      actions.insertBefore(button, actions.firstChild);
      form.classList.add("is-collapsible");
    });
  }

  // List filters: one removable chip per applied filter, from the GET form
  // itself (labels and option text), so every list gets them for free.
  function initFilterChips() {
    document.querySelectorAll("form.filters[method='get']").forEach(function (form) {
      var chips = [];
      Array.prototype.forEach.call(form.elements, function (el) {
        if (!el.name || el.type === "submit" || el.type === "button" || el.type === "hidden") {
          return;
        }
        var value = el.type === "checkbox" ? (el.checked ? "on" : "") : (el.value || "").trim();
        if (!value) {
          return;
        }
        var label = form.querySelector("label[for='" + el.id + "']");
        var text = el.tagName === "SELECT" ? el.options[el.selectedIndex].text : (el.type === "checkbox" ? "Yes" : value);
        var name = label ? label.textContent.replace(/\*/g, "").trim() : el.name;
        if (el.type === "checkbox" && el.closest("label")) {
          name = el.closest("label").textContent.trim();
          text = "";
        }
        chips.push({ el: el, name: name, text: text });
      });
      if (!chips.length) {
        return;
      }
      var box = document.createElement("div");
      box.className = "filter-chips";
      box.setAttribute("aria-label", "Active filters");
      var heading = document.createElement("span");
      heading.className = "filter-chips__label";
      heading.textContent = "Filtered by";
      box.appendChild(heading);
      chips.forEach(function (chip) {
        var button = document.createElement("button");
        button.type = "button";
        button.className = "chip";
        button.setAttribute("aria-label", "Remove filter " + chip.name + (chip.text ? ": " + chip.text : ""));
        button.appendChild(document.createTextNode(chip.name + (chip.text ? ": " : "")));
        if (chip.text) {
          var strong = document.createElement("strong");
          strong.textContent = chip.text;
          button.appendChild(strong);
        }
        var x = document.createElement("span");
        x.className = "chip__x";
        x.setAttribute("aria-hidden", "true");
        x.textContent = "×";
        button.appendChild(x);
        button.addEventListener("click", function () {
          if (chip.el.type === "checkbox") { chip.el.checked = false; } else { chip.el.value = ""; }
          form.submit();
        });
        box.appendChild(button);
      });
      if (chips.length > 1) {
        var clear = document.createElement("a");
        clear.className = "chip chip--clear";
        clear.href = form.getAttribute("action") || window.location.pathname;
        clear.textContent = "Clear all";
        box.appendChild(clear);
      }
      form.insertAdjacentElement("afterend", box);
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

  // Generic formset tables: "+ Add row" copies the empty form.
  function initFormsets() {
    document.querySelectorAll("[data-formset]").forEach(function (box) {
      var prefix = box.getAttribute("data-prefix");
      var total = box.querySelector("input[name='" + prefix + "-TOTAL_FORMS']");
      var body = box.querySelector("[data-formset-body]");
      var template = box.querySelector("template[data-formset-empty]");
      var add = box.querySelector("[data-formset-add]");
      if (!total || !body || !template || !add) {
        return;
      }
      add.addEventListener("click", function () {
        var index = parseInt(total.value, 10);
        var holder = document.createElement("tbody");
        holder.innerHTML = template.innerHTML.replace(/__prefix__/g, String(index)).trim();
        var row = holder.firstElementChild;
        body.appendChild(row);
        total.value = String(index + 1);
        bindSkuInputs(row);
      });
    });
  }

  // SKU boxes on purchase forms: suggestions from the form's data-items-url.
  function bindSkuInputs(root) {
    var form = root.closest ? root.closest("form[data-items-url]") : null;
    var options = document.getElementById("sku-options");
    if (!form || !options) {
      return;
    }
    var url = form.getAttribute("data-items-url");
    var timer = null;
    root.querySelectorAll("input[data-sku-search]").forEach(function (input) {
      input.setAttribute("list", "sku-options");
      input.addEventListener("input", function () {
        window.clearTimeout(timer);
        var query = input.value.trim();
        if (query.length < 2) {
          return;
        }
        timer = window.setTimeout(function () {
          fetch(url + "?" + new URLSearchParams({ q: query }).toString(), { credentials: "same-origin" })
            .then(function (response) { return response.ok ? response.json() : { results: [] }; })
            .then(function (data) {
              options.innerHTML = "";
              (data.results || []).forEach(function (row) {
                var option = document.createElement("option");
                option.value = row.sku;
                option.label = row.text;
                options.appendChild(option);
              });
            })
            .catch(function () { /* the server validates the SKU on save */ });
        }, 250);
      });
    });
  }

  // List pages: current-page multi-select, bulk bar and CSV export.
  function initListSelection() {
    var bar = document.querySelector("[data-bulk-bar]");
    var all = document.querySelector("[data-select-all]");
    var boxes = Array.prototype.slice.call(document.querySelectorAll("[data-select-row]"));
    if (!bar || !boxes.length) {
      return;
    }
    var count = bar.querySelector("[data-bulk-count]");
    function selected() {
      return boxes.filter(function (box) { return box.checked; });
    }
    function refresh() {
      var n = selected().length;
      count.textContent = String(n);
      bar.hidden = n === 0;
      if (all) {
        all.checked = n > 0 && n === boxes.length;
        all.indeterminate = n > 0 && n < boxes.length;
      }
    }
    boxes.forEach(function (box) { box.addEventListener("change", refresh); });
    if (all) {
      all.addEventListener("change", function () {
        boxes.forEach(function (box) { box.checked = all.checked; });
        refresh();
      });
    }
    bar.querySelector("[data-bulk-clear]").addEventListener("click", function () {
      boxes.forEach(function (box) { box.checked = false; });
      refresh();
    });
    bar.querySelector("[data-bulk-export]").addEventListener("click", function () {
      var table = boxes[0].closest("table");
      var heads = Array.prototype.slice.call(table.querySelectorAll("thead th"))
        .filter(function (th) { return !th.classList.contains("select-col") && !th.classList.contains("actions-col"); })
        .map(function (th) { return th.textContent.trim(); });
      var lines = [heads];
      selected().forEach(function (box) {
        var cells = Array.prototype.slice.call(box.closest("tr").children)
          .filter(function (td) { return !td.classList.contains("select-col") && !td.classList.contains("actions-col"); });
        lines.push(cells.map(function (td) { return td.innerText.replace(/\s+/g, " ").trim(); }));
      });
      var csv = lines.map(function (row) {
        return row.map(function (value) { return '"' + value.replace(/"/g, '""') + '"'; }).join(",");
      }).join("\r\n");
      var link = document.createElement("a");
      link.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
      link.download = "selection.csv";
      link.click();
      URL.revokeObjectURL(link.href);
    });
    refresh();
  }

  // Quick view drawer: a detail page rendered read-only (?quick=1), or the
  // row's own cells for lists without a detail page. The list stays put.
  function initQuickView() {
    var drawer = document.querySelector("[data-quick-drawer]");
    if (!drawer) {
      return;
    }
    var body = drawer.querySelector("[data-quick-body]");
    function open() {
      if (typeof drawer.showModal === "function") { drawer.showModal(); } else { drawer.setAttribute("open", ""); }
    }
    function fromRow(row) {
      var list = document.createElement("dl");
      list.className = "dl";
      Array.prototype.slice.call(row.children).forEach(function (td) {
        if (td.classList.contains("select-col") || td.classList.contains("actions-col")) {
          return;
        }
        var dt = document.createElement("dt");
        dt.textContent = td.getAttribute("data-label") || "";
        var dd = document.createElement("dd");
        dd.textContent = td.innerText.replace(/\s+/g, " ").trim() || "—";
        list.appendChild(dt);
        list.appendChild(dd);
      });
      return list;
    }
    document.addEventListener("click", function (event) {
      var trigger = event.target.closest("[data-quick-view]");
      if (!trigger) {
        return;
      }
      var url = trigger.getAttribute("data-quick-view");
      var menu = trigger.closest("details");
      if (menu) { menu.open = false; }
      body.innerHTML = "";
      if (!url) {
        body.appendChild(fromRow(trigger.closest("tr")));
        open();
        return;
      }
      body.textContent = "Loading…";
      open();
      fetch(url + (url.indexOf("?") === -1 ? "?" : "&") + "quick=1", { credentials: "same-origin" })
        .then(function (response) { return response.ok ? response.text() : Promise.reject(response.status); })
        .then(function (html) { body.innerHTML = html; })
        .catch(function () { body.textContent = "This record could not be loaded."; });
    });
    drawer.querySelector("[data-quick-close]").addEventListener("click", function () { drawer.close(); });
    drawer.addEventListener("click", function (event) {
      if (event.target === drawer) { drawer.close(); }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initSidebar();
    initTheme();
    initMenus();
    initFilterChips();
    initFilterToggle();
    initForms();
    initSale();
    initFormsets();
    initListSelection();
    initQuickView();
    document.querySelectorAll("form[data-items-url]").forEach(bindSkuInputs);
  });
})();
