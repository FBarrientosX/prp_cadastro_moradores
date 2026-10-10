(function () {
    function painelDo(alvo) {
        const card = alvo.closest(".morador-item");
        return card ? card.querySelector(".foto-facial-painel") : null;
    }

    function pararCamera(painel) {
        if (!painel) {
            return;
        }
        const video = painel.querySelector(".js-video");
        const stream = video && video.srcObject;
        if (stream && stream.getTracks) {
            stream.getTracks().forEach((trilha) => trilha.stop());
        }
        if (video) {
            video.srcObject = null;
        }
        const caixa = painel.querySelector(".js-camera");
        if (caixa) {
            caixa.classList.add("d-none");
        }
    }

    function status(painel, texto) {
        const alvo = painel.querySelector(".js-foto-status");
        if (alvo) {
            alvo.textContent = texto || "";
        }
    }

    function aplicarPreview(painel, url, confirmado) {
        const card = painel.closest(".morador-item");
        const previa = painel.querySelector(".js-preview-foto");
        if (previa) {
            previa.src = url;
            previa.classList.remove("d-none");
        }
        if (!card) {
            return;
        }
        if (confirmado) {
            card.dataset.foto = "1";
            delete card.dataset.fotoPreview;
        } else {
            card.dataset.fotoPreview = "1";
        }
        const wrap = card.querySelector(".avatar-facial-wrap");
        if (wrap) {
            wrap.innerHTML =
                '<img class="avatar-facial rounded-circle" src="' +
                url +
                '" alt="" width="64" height="64">';
        }
        if (typeof window.atualizarResumoFacial === "function") {
            window.atualizarResumoFacial(card);
        }
    }

    async function enviar(painel) {
        const url = painel.dataset.uploadUrl || "";
        const base64 = painel.querySelector(".js-foto-base64");
        const arquivo = painel.querySelector(".js-arquivo-foto");
        if (!url) {
            status(painel, "A foto será enviada ao salvar o cadastro.");
            return;
        }
        const dados = new FormData();
        if (arquivo && arquivo.files && arquivo.files[0]) {
            dados.append("foto", arquivo.files[0]);
        } else if (base64 && base64.value) {
            dados.append("foto_base64", base64.value);
        } else {
            status(painel, "Escolha ou capture uma foto.");
            return;
        }
        status(painel, "Enviando foto...");
        let resposta;
        try {
            resposta = await fetch(url, {
                method: "POST",
                body: dados,
                headers: { "X-Requested-With": "XMLHttpRequest" },
            });
        } catch (erro) {
            status(painel, "Não foi possível enviar a foto. Ela segue no formulário para o salvamento.");
            return;
        }
        let corpo = {};
        try {
            corpo = await resposta.json();
        } catch (erro) {
            corpo = {};
        }
        if (!resposta.ok || !corpo.ok) {
            status(painel, corpo.erro || "Não foi possível salvar a foto.");
            return;
        }
        if (base64) {
            base64.value = "";
        }
        if (arquivo) {
            arquivo.value = "";
        }
        if (corpo.foto_url) {
            aplicarPreview(painel, corpo.foto_url, true);
        }
        status(painel, "Foto facial salva para a portaria.");
    }

    async function abrirCamera(painel) {
        const caixa = painel.querySelector(".js-camera");
        const video = painel.querySelector(".js-video");
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            status(painel, "Este aparelho não liberou a câmera. Use Escolher Arquivo de Foto.");
            return;
        }
        pararCamera(painel);
        try {
            const stream = await navigator.mediaDevices.getUserMedia({
                video: { facingMode: "user" },
                audio: false,
            });
            video.srcObject = stream;
            caixa.classList.remove("d-none");
            status(painel, "Centralize o rosto no guia oval e capture a foto.");
        } catch (erro) {
            status(painel, "Não foi possível abrir a câmera. Use Escolher Arquivo de Foto.");
        }
    }

    function capturar(painel) {
        const video = painel.querySelector(".js-video");
        const base64 = painel.querySelector(".js-foto-base64");
        if (!video || !video.videoWidth) {
            status(painel, "Aguarde a câmera iniciar.");
            return;
        }
        const quadro = document.createElement("canvas");
        quadro.width = video.videoWidth;
        quadro.height = video.videoHeight;
        quadro.getContext("2d").drawImage(video, 0, 0, quadro.width, quadro.height);
        const data = quadro.toDataURL("image/jpeg", 0.92);
        if (base64) {
            base64.value = data;
        }
        const arquivo = painel.querySelector(".js-arquivo-foto");
        if (arquivo) {
            arquivo.value = "";
        }
        pararCamera(painel);
        aplicarPreview(painel, data, false);
        enviar(painel);
    }

    function escolherArquivo(painel, arquivo) {
        const file = arquivo.files && arquivo.files[0];
        if (!file) {
            return;
        }
        const base64 = painel.querySelector(".js-foto-base64");
        if (base64) {
            base64.value = "";
        }
        const leitor = new FileReader();
        leitor.onload = function () {
            aplicarPreview(painel, String(leitor.result || ""), false);
            enviar(painel);
        };
        leitor.readAsDataURL(file);
    }

    window.pararCameraFacial = pararCamera;

    window.htmlPainelFotoFacial = function (index) {
        return (
            '<div class="foto-facial-painel border-top px-3 py-3 d-none">' +
            '<div class="fw-semibold">Foto para Reconhecimento Facial (Portaria)</div>' +
            '<p class="small text-muted mb-2">Foto de frente, boa iluminação, sem óculos escuros ou boné.</p>' +
            '<div class="d-flex flex-wrap gap-2 mb-2">' +
            '<button type="button" class="btn btn-sm btn-primary js-selfie">📷 Tirar Selfie com a Câmera</button>' +
            '<button type="button" class="btn btn-sm btn-outline-secondary js-escolher-arquivo">📁 Escolher Arquivo de Foto</button>' +
            "</div>" +
            '<div class="js-camera d-none">' +
            '<div class="foto-facial-visor"><video class="js-video" autoplay playsinline muted></video><div class="foto-facial-guia"></div></div>' +
            '<div class="d-flex flex-wrap gap-2 mt-2">' +
            '<button type="button" class="btn btn-sm btn-success js-capturar">Capturar Foto</button>' +
            '<button type="button" class="btn btn-sm btn-link js-fechar-camera">Fechar câmera</button>' +
            "</div></div>" +
            '<img class="js-preview-foto d-none rounded mt-2" alt="Prévia da foto facial" width="160" height="160">' +
            '<input type="hidden" name="morador_' + index + '_foto_base64" class="js-foto-base64" value="">' +
            '<input type="file" name="morador_' + index + '_foto" class="d-none js-arquivo-foto" accept="image/*" capture="user">' +
            '<p class="small js-foto-status text-muted mb-0 mt-2"></p>' +
            "</div>"
        );
    };

    document.addEventListener("click", function (event) {
        const abrir = event.target.closest(".js-abrir-foto");
        if (abrir) {
            const painel = painelDo(abrir);
            if (!painel) {
                return;
            }
            painel.classList.toggle("d-none");
            if (painel.classList.contains("d-none")) {
                pararCamera(painel);
            } else {
                painel.scrollIntoView({ block: "nearest" });
            }
            return;
        }
        const selfie = event.target.closest(".js-selfie");
        if (selfie) {
            const painel = painelDo(selfie);
            if (painel) {
                abrirCamera(painel);
            }
            return;
        }
        const capturarBtn = event.target.closest(".js-capturar");
        if (capturarBtn) {
            const painel = painelDo(capturarBtn);
            if (painel) {
                capturar(painel);
            }
            return;
        }
        const fechar = event.target.closest(".js-fechar-camera");
        if (fechar) {
            pararCamera(painelDo(fechar));
            return;
        }
        const escolher = event.target.closest(".js-escolher-arquivo");
        if (escolher) {
            const painel = painelDo(escolher);
            const arquivo = painel && painel.querySelector(".js-arquivo-foto");
            if (arquivo) {
                arquivo.click();
            }
        }
    });

    document.addEventListener("change", function (event) {
        const arquivo = event.target.closest(".js-arquivo-foto");
        if (!arquivo) {
            return;
        }
        const painel = painelDo(arquivo);
        if (painel) {
            escolherArquivo(painel, arquivo);
        }
    });
})();
