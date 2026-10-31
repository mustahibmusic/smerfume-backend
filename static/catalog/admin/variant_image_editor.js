/* Variant image editor (Manage Images page). Crop, zoom, rotate, flip,
   free or 1:1 aspect (Cropper.js 1.7.0, vendored), brightness and contrast
   (canvas filters, hidden when the browser lacks them), live preview and
   reset. The result is exported at most 2000 px as WebP (PNG when
   transparent, otherwise JPEG, if the browser cannot encode WebP) and
   posted to the replace endpoint; the server validates and normalises it
   again. */
(function () {
    "use strict";

    var MAX_SIDE = 2000;
    var QUALITY = 0.9;
    var FILTERS_SUPPORTED = typeof CanvasRenderingContext2D !== "undefined"
        && "filter" in CanvasRenderingContext2D.prototype;

    function init() {
        var root = document.getElementById("variant-gallery");
        var dialog = document.getElementById("vg-editor");
        if (!root || !dialog || typeof Cropper === "undefined") {
            return;
        }
        var csrf = root.querySelector("input[name=csrfmiddlewaretoken]").value;
        var image = dialog.querySelector(".vg-editor-image");
        var preview = dialog.querySelector(".vg-editor-preview");
        var message = dialog.querySelector(".vg-editor-message");
        var brightness = dialog.querySelector("[name=brightness]");
        var contrast = dialog.querySelector("[name=contrast]");
        var saveButton = dialog.querySelector("[data-action=save]");
        var cropper = null;
        var replaceUrl = null;
        var flip = { x: 1, y: 1 };
        var previewQueued = false;

        if (!FILTERS_SUPPORTED) {
            dialog.querySelector(".vg-editor-adjust").hidden = true;
        }

        function filterValue() {
            if (!FILTERS_SUPPORTED) {
                return "none";
            }
            return "brightness(" + brightness.value + "%) contrast(" + contrast.value + "%)";
        }

        function filtered(source) {
            var canvas = document.createElement("canvas");
            canvas.width = source.width;
            canvas.height = source.height;
            var context = canvas.getContext("2d");
            context.filter = filterValue();
            context.drawImage(source, 0, 0);
            return canvas;
        }

        function drawPreview() {
            previewQueued = false;
            if (!cropper) {
                return;
            }
            var cropped = cropper.getCroppedCanvas({ maxWidth: 360, maxHeight: 360 });
            if (!cropped) {
                return;
            }
            var result = filtered(cropped);
            preview.width = result.width;
            preview.height = result.height;
            preview.getContext("2d").drawImage(result, 0, 0);
        }

        function queuePreview() {
            if (!previewQueued) {
                previewQueued = true;
                window.requestAnimationFrame(drawPreview);
            }
        }

        function resetAdjustments() {
            brightness.value = 100;
            contrast.value = 100;
            flip = { x: 1, y: 1 };
        }

        // Reset rebuilds the cropper from the loaded image: Cropper's own
        // reset() leaves a broken canvas after rotate + flip.
        function build() {
            if (cropper) {
                cropper.destroy();
            }
            resetAdjustments();
            cropper = new Cropper(image, {
                viewMode: 1,
                autoCropArea: 1,
                checkCrossOrigin: true,
                ready: queuePreview,
                crop: queuePreview,
                zoom: queuePreview,
            });
        }

        function open(src, url) {
            replaceUrl = url;
            message.textContent = "";
            saveButton.disabled = false;
            if (cropper) {
                cropper.destroy();
                cropper = null;
            }
            image.onload = build;
            // Cache-busting query so the browser does not reuse a copy
            // fetched without CORS headers (canvas export would fail).
            image.src = src + (src.indexOf("?") === -1 ? "?" : "&") + "edit=" + Date.now();
            dialog.showModal();
        }

        function close() {
            if (cropper) {
                cropper.destroy();
                cropper = null;
            }
            dialog.close();
        }

        function hasTransparency(canvas) {
            var data = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
            for (var i = 3; i < data.length; i += 4) {
                if (data[i] < 255) {
                    return true;
                }
            }
            return false;
        }

        function toBlob(canvas, type, quality) {
            return new Promise(function (resolve) {
                canvas.toBlob(resolve, type, quality);
            });
        }

        function encode(canvas) {
            return toBlob(canvas, "image/webp", QUALITY).then(function (blob) {
                if (blob && blob.type === "image/webp") {
                    return { blob: blob, name: "edited.webp" };
                }
                if (hasTransparency(canvas)) {
                    return toBlob(canvas, "image/png").then(function (png) {
                        return { blob: png, name: "edited.png" };
                    });
                }
                return toBlob(canvas, "image/jpeg", QUALITY).then(function (jpeg) {
                    return { blob: jpeg, name: "edited.jpg" };
                });
            });
        }

        function save() {
            var cropped;
            try {
                cropped = cropper.getCroppedCanvas({
                    maxWidth: MAX_SIDE, maxHeight: MAX_SIDE, imageSmoothingEnabled: true, imageSmoothingQuality: "high",
                });
            } catch (error) {
                message.textContent = "This image cannot be edited in the browser (storage CORS settings).";
                return;
            }
            saveButton.disabled = true;
            message.textContent = "Saving…";
            encode(filtered(cropped)).then(function (encoded) {
                var body = new FormData();
                body.append("image", encoded.blob, encoded.name);
                return fetch(replaceUrl, {
                    method: "POST",
                    body: body,
                    headers: { "X-CSRFToken": csrf, "Accept": "application/json" },
                    credentials: "same-origin",
                });
            }).then(function (response) {
                return response.json().then(function (data) {
                    if (!response.ok) {
                        throw new Error(data.error || "The edited image could not be saved.");
                    }
                    window.location.reload();
                });
            }).catch(function (error) {
                saveButton.disabled = false;
                message.textContent = error.message;
            });
        }

        root.addEventListener("click", function (event) {
            var button = event.target.closest("[data-edit]");
            if (button) {
                open(button.dataset.src, button.dataset.replaceUrl);
            }
        });

        dialog.addEventListener("click", function (event) {
            var button = event.target.closest("[data-action]");
            if (!button || !cropper) {
                if (button && button.dataset.action === "cancel") {
                    close();
                }
                return;
            }
            switch (button.dataset.action) {
            case "rotate-left": cropper.rotate(-90); break;
            case "rotate-right": cropper.rotate(90); break;
            case "flip-horizontal": flip.x = -flip.x; cropper.scaleX(flip.x); break;
            case "flip-vertical": flip.y = -flip.y; cropper.scaleY(flip.y); break;
            case "zoom-in": cropper.zoom(0.1); break;
            case "zoom-out": cropper.zoom(-0.1); break;
            case "aspect-free": cropper.setAspectRatio(NaN); break;
            case "aspect-square": cropper.setAspectRatio(1); break;
            case "reset": build(); break;
            case "save": save(); return;
            case "cancel": close(); return;
            }
            queuePreview();
        });

        [brightness, contrast].forEach(function (input) {
            input.addEventListener("input", queuePreview);
        });
        dialog.addEventListener("cancel", function (event) {
            event.preventDefault();
            close();
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
