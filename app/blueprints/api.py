"""API M2M dos equipamentos de acesso (catraca, RFID, facial).

As rotas ficam em /api/v1. O registro usa register(app) + add_url_rule,
no mesmo padrão dos demais módulos, para o nome do endpoint não ganhar prefixo.
"""

from functools import wraps

from flask import jsonify, request

from app.models import Condominio, CredencialAcesso


def requer_api_key(view):
    """Exige o header X-API-Key de um condomínio ativo."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        chave = (request.headers.get("X-API-Key") or "").strip()
        condominio = None
        if chave:
            condominio = Condominio.query.filter_by(api_key=chave, ativo=True).first()
        if condominio is None:
            return jsonify({"erro": "Unauthorized"}), 401
        return view(condominio, *args, **kwargs)

    return wrapped


def _rotulo_unidade(condominio, unidade):
    configuracao = condominio.configuracao
    agrupamento = "Bloco"
    unidade_label = "Apto"
    if configuracao is not None:
        if configuracao.label_agrupamento:
            agrupamento = configuracao.label_agrupamento
        if configuracao.label_unidade:
            unidade_label = configuracao.label_unidade
    return f"{agrupamento} {unidade.bloco} - {unidade_label} {unidade.apartamento}"


@requer_api_key
def credenciais_ativas(condominio):
    """Lista as credenciais ativas do condomínio dono da chave."""
    registros = (
        CredencialAcesso.query.filter_by(condominio_id=condominio.id, ativa=True)
        .order_by(CredencialAcesso.id)
        .all()
    )
    credenciais = []
    for credencial in registros:
        morador = credencial.morador
        if morador is None or not morador.eh_morador:
            continue
        unidade = morador.unidade
        credenciais.append(
            {
                "tipo": credencial.tipo,
                "codigo": credencial.codigo_identificador,
                "morador": morador.nome_completo if morador is not None else "",
                "unidade": _rotulo_unidade(condominio, unidade) if unidade is not None else "",
            }
        )
    return jsonify(
        {
            "condominio": condominio.nome,
            "total_credenciais": len(credenciais),
            "credenciais": credenciais,
        }
    )


def register(app):
    app.add_url_rule(
        "/api/v1/credenciais/ativas",
        "api_credenciais_ativas",
        credenciais_ativas,
        methods=["GET"],
    )
