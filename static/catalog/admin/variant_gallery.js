/* Manage Images page: client-side upload checks, drag-and-drop and
   keyboard reordering. Every change is validated again on the server;
   without JavaScript the role, alt text, upload and remove forms still
   work (reordering needs JavaScript). */
(function () {
    "use strict";

    var ACCEPTED = ["image/jpeg", "image/png", "image/webp"];

    function init() {
        var root = document.getElementById("variant-gallery");
        if (!root) {
            return;
        }
        var csrf = root.querySelector("input[name=csrfmiddlewaretoken]").value;
        var status = root.querySelector(".vg-status");
        var grid = root.querySelector(".vg-grid");

        function say(message) {
            if (status) {
                status.textContent = message;
            }
        }

        initUpload(root);
        if (grid) {
            initReorder(root, grid, csrf, say);
        }
    }

    function initUpload(root) {
        var form = root.querySelector(".vg-upload");
        if (!form) {
            return;
        }
        var input = form.querySelector("input[type=file]");
        var errors = form.querySelector(".vg-upload-errors");
        var zone = form.querySelector(".vg-dropzone");
        var freeSlots = parseInt(root.dataset.freeSlots, 10);
        var maxBytes = parseInt(root.dataset.maxUploadMb, 10) * 1024 * 1024;

        function problems(files) {
            var found = [];
            if (!files.length) {
                found.push("Choose at least one image.");
            }
            if (files.length > freeSlots) {
                found.push("Only " + freeSlots + " more image(s) fit in this gallery.");
            }
            Array.prototype.forEach.call(files, function (file) {
                if (ACCEPTED.indexOf(file.type) === -1) {
                    found.push(file.name + ": only JPEG, PNG and WebP are accepted.");
                } else if (file.size > maxBytes) {
                    found.push(file.name + ": larger than " + root.dataset.maxUploadMb + " MB.");
                }
            });
            return found;
        }

        form.addEventListener("submit", function (event) {
            var found = problems(input.files);
            errors.textContent = found.join("\n");
            if (found.length) {
                event.preventDefault();
            }
        });

        ["dragenter", "dragover"].forEach(function (name) {
            zone.addEventListener(name, function (event) {
                if (event.dataTransfer && Array.prototype.indexOf.call(event.dataTransfer.types, "Files") !== -1) {
                    event.preventDefault();
                    zone.classList.add("is-over");
                }
            });
        });
        ["dragleave", "drop"].forEach(function (name) {
            zone.addEventListener(name, function () {
                zone.classList.remove("is-over");
            });
        });
        zone.addEventListener("drop", function (event) {
            if (event.dataTransfer && event.dataTransfer.files.length) {
                event.preventDefault();
                input.files = event.dataTransfer.files;
                errors.textContent = problems(input.files).join("\n");
            }
        });
    }

    function initReorder(root, grid, csrf, say) {
        var dragged = null;

        function cards() {
            return Array.prototype.slice.call(grid.querySelectorAll(".vg-card"));
        }

        function save() {
            var order = cards().map(function (card) {
                return card.dataset.publicId;
            });
            var body = new FormData();
            body.append("order", JSON.stringify(order));
            say("Saving order…");
            return fetch(root.dataset.reorderUrl, {
                method: "POST",
                body: body,
                headers: { "X-CSRFToken": csrf, "Accept": "application/json" },
                credentials: "same-origin",
            }).then(function (response) {
                return response.json().then(function (data) {
                    if (!response.ok) {
                        throw new Error(data.error || "The order could not be saved.");
                    }
                    say("Order saved.");
                });
            }).catch(function (error) {
                say(error.message + " Reloading…");
                window.setTimeout(function () { window.location.reload(); }, 1500);
            });
        }

        // Primary and secondary always come first for customers, so only
        // gallery-role cards move, and only among themselves.
        function isGallery(card) {
            return card && card.dataset.role === "gallery";
        }

        grid.addEventListener("click", function (event) {
            var button = event.target.closest("[data-move]");
            if (!button) {
                return;
            }
            var card = button.closest(".vg-card");
            var step = parseInt(button.dataset.move, 10);
            var neighbour = step < 0 ? card.previousElementSibling : card.nextElementSibling;
            if (!isGallery(neighbour)) {
                say("Already at the " + (step < 0 ? "start" : "end") + " of the gallery images.");
                return;
            }
            grid.insertBefore(card, step < 0 ? neighbour : neighbour.nextElementSibling);
            button.focus();
            save();
        });

        grid.addEventListener("dragstart", function (event) {
            var card = event.target.closest(".vg-card");
            if (!isGallery(card)) {
                return;
            }
            dragged = card;
            card.classList.add("is-dragging");
            event.dataTransfer.effectAllowed = "move";
            event.dataTransfer.setData("text/plain", card.dataset.publicId);
        });

        grid.addEventListener("dragover", function (event) {
            var target = event.target.closest(".vg-card");
            if (!dragged || !isGallery(target) || target === dragged) {
                return;
            }
            event.preventDefault();
            cards().forEach(function (card) { card.classList.remove("is-drop-target"); });
            target.classList.add("is-drop-target");
        });

        grid.addEventListener("drop", function (event) {
            var target = event.target.closest(".vg-card");
            if (!dragged || !isGallery(target) || target === dragged) {
                return;
            }
            event.preventDefault();
            var list = cards();
            var after = list.indexOf(dragged) < list.indexOf(target);
            grid.insertBefore(dragged, after ? target.nextElementSibling : target);
            save();
        });

        grid.addEventListener("dragend", function () {
            if (dragged) {
                dragged.classList.remove("is-dragging");
            }
            dragged = null;
            cards().forEach(function (card) { card.classList.remove("is-drop-target"); });
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
