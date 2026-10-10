"""API M2M dos equipamentos de acesso (catraca, RFID, facial).

As rotas ficam em /api/v1. O registro usa register(app) + add_url_rule,
no mesmo padrão dos demais módulos, para o nome do endpoint não ganhar prefixo.
"""

import hashlib
import hmac
from datetime import datetime
from functools import wraps

from flask import jsonify, request, url_for
from sqlalchemy.orm import joinedload, selectinload

from app import db
from app.models import (
    Condominio,
    CredencialAcesso,
    EquipamentoAcesso,
    Pessoa,
    StatusPessoa,
    StatusUnidade,
    Unidade,
)
from app.utils import BLOCO_SETORES, nome_foto_facial_seguro


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
        if unidade is not None and unidade.eh_setor_interno:
            continue
        item = {
            "tipo": credencial.tipo,
            "codigo": credencial.codigo_identificador,
            "morador": morador.nome_completo if morador is not None else "",
            "unidade": _rotulo_unidade(condominio, unidade) if unidade is not None else "",
        }
        if credencial.tipo in CredencialAcesso.TIPOS_FACIAL:
            nome_foto = nome_foto_facial_seguro(morador.foto_facial)
            if nome_foto:
                item["foto_url"] = (
                    request.host_url.rstrip("/")
                    + url_for("static", filename=f"uploads/faciais/{nome_foto}")
                )
        credenciais.append(item)
    return jsonify(
        {
            "condominio": condominio.nome,
            "total_credenciais": len(credenciais),
            "credenciais": credenciais,
        }
    )


def requer_agent_token(view):
    """Identifica o condomínio pelo Bearer token do Vizinsync Agent."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        header = request.headers.get("Authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        condominio = None
        if token:
            condominio = Condominio.query.filter_by(
                agent_api_token=token, ativo=True
            ).first()
        gravado = (condominio.agent_api_token or "") if condominio is not None else ""
        if (
            condominio is None
            or len(gravado) != len(token)
            or not hmac.compare_digest(gravado, token)
        ):
            return jsonify({"erro": "Unauthorized"}), 401
        return view(condominio, *args, **kwargs)

    return wrapped


def _hash_versao(nome, foto_em, tags):
    momento = foto_em.strftime("%Y-%m-%dT%H:%M:%S") if foto_em else ""
    base = "|".join([nome or "", momento, ",".join(sorted(tags))])
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


def _usuarios_agent(condominio):
    """Moradores aprovados que já têm alguma credencial ativa neste condomínio."""
    pessoas = (
        Pessoa.query.options(
            joinedload(Pessoa.unidade),
            selectinload(Pessoa.credenciais),
        )
        .join(Unidade, Pessoa.unidade_id == Unidade.id)
        .filter(
            Unidade.condominio_id == condominio.id,
            Pessoa.eh_morador.is_(True),
            Pessoa.status == StatusPessoa.APROVADO,
            Unidade.status.in_((StatusUnidade.APROVADA, StatusUnidade.REGISTRADA)),
        )
        .order_by(Unidade.bloco, Unidade.apartamento, Pessoa.nome_completo)
        .all()
    )
    usuarios = []
    for pessoa in pessoas:
        unidade = pessoa.unidade
        if unidade is None:
            continue
        ativas = [
            credencial
            for credencial in pessoa.credenciais
            if credencial.ativa and credencial.condominio_id == condominio.id
        ]
        if not ativas:
            continue
        tags = [
            credencial.codigo_identificador
            for credencial in ativas
            if credencial.tipo in CredencialAcesso.TIPOS_TAG_CARTAO
        ]
        foto = nome_foto_facial_seguro(pessoa.foto_facial)
        url_foto = ""
        if foto:
            url_foto = (
                request.host_url.rstrip("/")
                + url_for("static", filename=f"uploads/faciais/{foto}")
            )
        bloco = unidade.bloco or ""
        setor = bool(unidade.eh_setor_interno) or bloco == BLOCO_SETORES
        usuarios.append(
            {
                "id_interno": pessoa.id,
                "nome": pessoa.nome_completo,
                "cpf": pessoa.cpf or "",
                "bloco": bloco,
                "apartamento": unidade.apartamento or "",
                "setor_interno": setor,
                "url_foto_facial": url_foto,
                "tags_rfid": tags,
                "hash_versao": _hash_versao(
                    pessoa.nome_completo, pessoa.foto_atualizada_em, tags
                ),
            }
        )
    return usuarios


@requer_agent_token
def agent_sync_payload(condominio):
    """Pacote que o agente da portaria consulta a cada minuto."""
    condominio.agent_ultimo_ping = datetime.utcnow()
    equipamentos = (
        EquipamentoAcesso.query.filter_by(condominio_id=condominio.id, ativo=True)
        .order_by(EquipamentoAcesso.id)
        .all()
    )
    db.session.commit()
    return jsonify(
        {
            "condominio_id": condominio.id,
            "equipamentos": [
                {
                    "id": equipamento.id,
                    "nome": equipamento.nome,
                    "fabricante": equipamento.fabricante,
                    "ip_local": equipamento.ip_local,
                    "porta": equipamento.porta,
                    "usuario_equipamento": equipamento.usuario_equipamento,
                    "senha_equipamento": equipamento.senha_equipamento,
                    "bloco_escopo": (equipamento.bloco_escopo or "").strip(),
                }
                for equipamento in equipamentos
            ],
            "usuarios": _usuarios_agent(condominio),
        }
    )


@requer_agent_token
def agent_report(condominio):
    """Grava o resultado do envio de cada controladora."""
    dados = request.get_json(silent=True) or {}
    if not isinstance(dados, dict):
        return jsonify({"erro": "Corpo inválido."}), 400
    equipamento_id = dados.get("equipamento_id")
    try:
        equipamento_id = int(equipamento_id)
    except (TypeError, ValueError):
        return jsonify({"erro": "Informe o equipamento."}), 400
    equipamento = EquipamentoAcesso.query.filter_by(
        id=equipamento_id,
        condominio_id=condominio.id,
    ).first()
    if equipamento is None:
        return jsonify({"erro": "Equipamento não encontrado."}), 404
    mensagem = " ".join(str(dados.get("mensagem") or "").split())[:255]
    equipamento.ultima_sincronia = datetime.utcnow()
    equipamento.status_ultimo_envio = mensagem or (
        "Sincronizado." if dados.get("sucesso") else "Falha no envio."
    )
    db.session.commit()
    return jsonify({"ok": True})


def register(app):
    app.add_url_rule(
        "/api/v1/credenciais/ativas",
        "api_credenciais_ativas",
        credenciais_ativas,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/v1/agent/sync-payload",
        "api_agent_sync_payload",
        agent_sync_payload,
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/v1/agent/report",
        "api_agent_report",
        agent_report,
        methods=["POST"],
    )
