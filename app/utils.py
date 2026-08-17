"""Utilitários de validação e estrutura do condomínio."""

import os
import random
import re
import string
from html import escape
from html.parser import HTMLParser

from flask import current_app, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.utils import secure_filename

SALT_RECUPERACAO_MORADOR = "recuperacao-morador"
SALT_RECUPERACAO_PARCEIRO = "recuperacao-parceiro"

PARCEIRO_LOGO_EXTENSOES = {"png", "jpg", "jpeg", "webp"}
PARCEIRO_LOGO_MAX_BYTES = 2 * 1024 * 1024

BLOCOS_ANDARES = {
    "1": 7,
    "2": 7,
    "3": 8,
    "4": 8,
    "5": 8,
    "6": 8,
    "7": 8,
    "8": 8,
}

APARTAMENTOS_POR_ANDAR = 8


def _apartamentos_do_andar(andar):
    return [str(andar * 100 + apt) for apt in range(1, APARTAMENTOS_POR_ANDAR + 1)]


def get_apartamentos_bloco(bloco):
    bloco = str(bloco).strip()
    num_andares = BLOCOS_ANDARES.get(bloco)
    if not num_andares:
        return []

    apartamentos = []
    for andar in range(1, num_andares + 1):
        apartamentos.extend(_apartamentos_do_andar(andar))
    return apartamentos


def get_condominio_estrutura():
    """Estrutura completa bloco -> andar -> apartamentos para uso no frontend."""
    estrutura = {}
    for bloco, num_andares in BLOCOS_ANDARES.items():
        estrutura[bloco] = {
            str(andar): _apartamentos_do_andar(andar)
            for andar in range(1, num_andares + 1)
        }
    return estrutura


def get_blocos():
    return list(BLOCOS_ANDARES.keys())


def normalizar_bloco_codigo(bloco):
    """Converte 'Bloco 1' ou '1' para o código numérico usado nas unidades."""
    bloco = str(bloco).strip()
    if bloco.lower().startswith("bloco "):
        return bloco.split(" ", 1)[1].strip()
    return bloco


def blocos_equivalentes(bloco_a, bloco_b):
    return normalizar_bloco_codigo(bloco_a) == normalizar_bloco_codigo(bloco_b)


def normalizar_bloco_apartamento(bloco, apartamento):
    return normalizar_bloco_codigo(bloco), str(apartamento).strip()


def validar_unidade(bloco, apartamento):
    bloco, apartamento = normalizar_bloco_apartamento(bloco, apartamento)
    if bloco not in BLOCOS_ANDARES:
        return False
    return apartamento in get_apartamentos_bloco(bloco)


def _get_token_serializer():
    secret_key = current_app.config["SECRET_KEY"]
    return URLSafeTimedSerializer(secret_key)


def gerar_token_redefinicao(email, salt, condominio_id=None):
    """
    Gera token de redefinição amarrado ao e-mail e (quando aplicável) ao
    condomínio/tenant vigente no momento da solicitação — evita que o link
    seja resolvido depois contra o tenant errado (ver verificar_token_redefinicao).
    """
    email_normalizado = str(email).strip().lower()
    payload = {"email": email_normalizado, "condominio_id": condominio_id}
    return _get_token_serializer().dumps(payload, salt=salt)


def verificar_token_redefinicao(token, salt, max_age=3600):
    """
    Retorna (email, condominio_id, emitido_em) ou (None, None, None) se o
    token for inválido/expirado.

    `condominio_id` é o tenant amarrado no momento da emissão do token — use-o
    para resolver a unidade/usuário, nunca o tenant da sessão atual (pode ter
    mudado entre a solicitação e o clique no link).

    `emitido_em` (datetime UTC aware) permite ao chamador rejeitar reuso: um
    token emitido antes da última troca de senha já foi consumido ou está
    obsoleto.

    Aceita também o formato legado (payload = e-mail em texto puro, sem
    tenant), para não invalidar imediatamente links antigos ainda dentro da
    janela de validade — nesse caso condominio_id volta como None.
    """
    try:
        payload, emitido_em = _get_token_serializer().loads(
            token, salt=salt, max_age=max_age, return_timestamp=True
        )
    except (BadSignature, SignatureExpired):
        return None, None, None

    if isinstance(payload, dict):
        email = payload.get("email")
        condominio_id = payload.get("condominio_id")
    else:
        email = payload
        condominio_id = None

    email = str(email).strip().lower() if email else None
    return email, condominio_id, emitido_em


def salvar_logo_parceiro(arquivo, prefixo="parceiro"):
    """
    Salva logotipo do parceiro.
    Retorna (filename, erro): sem arquivo (None, None); inválido (None, msg); ok (nome, None).
    """
    if not arquivo or not arquivo.filename:
        return None, None

    nome_seguro = secure_filename(arquivo.filename)
    if not nome_seguro or "." not in nome_seguro:
        return None, "Envie um logotipo em PNG, JPG, JPEG ou WEBP."

    extensao = nome_seguro.rsplit(".", 1)[-1].lower()
    if extensao not in PARCEIRO_LOGO_EXTENSOES:
        return None, "Envie um logotipo em PNG, JPG, JPEG ou WEBP."

    tamanho_header = getattr(arquivo, "content_length", None)
    if tamanho_header and tamanho_header > PARCEIRO_LOGO_MAX_BYTES:
        return None, "O logotipo deve ter no máximo 2 MB."

    arquivo.stream.seek(0, os.SEEK_END)
    tamanho = arquivo.stream.tell()
    arquivo.stream.seek(0)
    if tamanho > PARCEIRO_LOGO_MAX_BYTES:
        return None, "O logotipo deve ter no máximo 2 MB."

    pasta = current_app.config.get("UPLOAD_PARCEIROS_FOLDER") or os.path.join(
        current_app.root_path, "static", "uploads", "parceiros"
    )
    os.makedirs(pasta, exist_ok=True)
    token = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    nome_final = f"{secure_filename(prefixo)}_{token}.{extensao}"
    arquivo.save(os.path.join(pasta, nome_final))
    return nome_final, None


def link_rede_social(valor):
    texto = (valor or "").strip()
    return texto[:255] if texto else None


class _SanitizadorHtmlRico(HTMLParser):
    """Mantém apenas tags permitidas pelo editor (negrito, itálico, sublinhado e quebras)."""

    _PERMITIDAS = frozenset({"p", "br", "strong", "b", "em", "i", "u"})

    def __init__(self):
        super().__init__()
        self._partes = []

    def handle_starttag(self, tag, attrs):
        if tag not in self._PERMITIDAS:
            return
        self._partes.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag not in self._PERMITIDAS or tag == "br":
            return
        self._partes.append(f"</{tag}>")

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self._partes.append("<br>")

    def handle_data(self, data):
        self._partes.append(escape(data))

    def resultado(self):
        return "".join(self._partes)


def html_rico_form(nome_campo):
    """Lê HTML do Quill, sanitiza e trata editor vazio como string vazia."""
    bruto = (request.form.get(nome_campo) or "").strip()
    if not bruto:
        return ""
    parser = _SanitizadorHtmlRico()
    parser.feed(bruto)
    parser.close()
    limpo = parser.resultado().strip()
    texto = re.sub(r"<[^>]+>", "", limpo).replace("&nbsp;", " ").strip()
    if not texto:
        return ""
    return limpo
