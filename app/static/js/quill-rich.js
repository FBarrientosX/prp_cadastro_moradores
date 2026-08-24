(function () {
    var toolbar = [["bold", "italic", "underline"]];

    function iniciarEditor(host) {
        if (!host || host.dataset.quillReady === "1" || typeof Quill === "undefined") {
            return;
        }
        var editorEl = host.querySelector(".quill-editor");
        var hidden = host.querySelector('input[type="hidden"]');
        if (!editorEl || !hidden) {
            return;
        }
        host.dataset.quillReady = "1";
        var placeholder = host.getAttribute("data-placeholder") || "";
        var quill = new Quill(editorEl, {
            theme: "snow",
            placeholder: placeholder,
            modules: { toolbar: toolbar },
        });
        if (hidden.value) {
            quill.clipboard.dangerouslyPasteHTML(hidden.value);
        }
        var form = host.closest("form");
        if (form) {
            form.addEventListener("submit", function () {
                hidden.value = quill.root.innerHTML;
            });
        }
    }

    function iniciarTodos(escopo) {
        var raiz = escopo || document;
        raiz.querySelectorAll("[data-quill-editor]").forEach(iniciarEditor);
    }

    document.addEventListener("DOMContentLoaded", function () {
        document.querySelectorAll("[data-quill-editor]").forEach(function (host) {
            if (!host.closest(".modal")) {
                iniciarEditor(host);
            }
        });
    });
    document.addEventListener("shown.bs.modal", function (evento) {
        iniciarTodos(evento.target);
    });
})();
