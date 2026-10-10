document.querySelectorAll(".js-escopo-painel").forEach(function (painel) {
    const todos = painel.querySelector(".js-escopo-todos");
    const blocos = painel.querySelectorAll(".js-escopo-bloco");
    if (!todos) {
        return;
    }

    function sincronizar() {
        blocos.forEach(function (box) {
            box.disabled = todos.checked;
        });
    }

    todos.addEventListener("change", function () {
        if (todos.checked) {
            blocos.forEach(function (box) {
                box.checked = false;
            });
        }
        sincronizar();
    });

    blocos.forEach(function (box) {
        box.addEventListener("change", function () {
            if (box.checked) {
                todos.checked = false;
                sincronizar();
            }
        });
    });

    sincronizar();
});
