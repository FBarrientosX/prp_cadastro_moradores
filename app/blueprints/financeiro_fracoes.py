"""Frações e regras de rateio. Só o admin local do condomínio."""

from datetime import datetime

from flask import flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from app import db
from app.auth import admin_required, condominio_id_obrigatorio, get_current_user
from app.financeiro_fracao import (
    TITULO_IGUALITARIA,
    coeficiente_percentual,
    incluir_unidades,
    peso_efetivo,
    unidades_residenciais,
)
from app.models import (
    GrupoFracao,
    ItemFracaoUnidade,
    ModoFracao,
    RateioCondominio,
    TipoIsencaoFracao,
    Unidade,
)
from app.utils import get_blocos


def _parse_peso(texto):
    bruto = (texto or "").strip().replace(" ", "")
    if not bruto:
        raise ValueError("Informe o peso.")
    if "," in bruto and "." in bruto:
        bruto = bruto.replace(".", "").replace(",", ".")
    else:
        bruto = bruto.replace(",", ".")
    valor = float(bruto)
    if valor < 0 or valor > 1_000_000:
        raise ValueError("Peso fora do intervalo.")
    return round(valor, 6)


def _marcado(nome):
    return "1" in request.form.getlist(nome)


def _grupo_do_tenant(grupo_id, condominio_id):
    return GrupoFracao.query.filter_by(id=grupo_id, condominio_id=condominio_id).first_or_404()


def _auditar(mensagem):
    from app.routes import _registrar_auditoria

    usuario = get_current_user()
    if usuario is not None:
        _registrar_auditoria(usuario, mensagem)


def _nome_responsavel(unidade):
    from app.blueprints.financeiro import _pagador_da_unidade

    nome, _documento, _email, _telefone = _pagador_da_unidade(unidade)
    return nome or ""


def _definir_padrao(grupo):
    (
        GrupoFracao.query.filter(
            GrupoFracao.condominio_id == grupo.condominio_id,
            GrupoFracao.id != grupo.id,
        ).update({GrupoFracao.padrao: False}, synchronize_session=False)
    )
    grupo.padrao = True


@admin_required
def admin_financeiro_fracoes():
    condominio_id = condominio_id_obrigatorio()
    grupos = (
        GrupoFracao.query.filter_by(condominio_id=condominio_id)
        .order_by(GrupoFracao.padrao.desc(), GrupoFracao.titulo.asc())
        .all()
    )
    grupo_id = request.args.get("grupo", type=int)
    grupo = next((item for item in grupos if item.id == grupo_id), None)
    if grupo is None:
        grupo = next((item for item in grupos if item.padrao), None) or (grupos[0] if grupos else None)

    linhas = []
    novas = []
    resumo = {"ativas": 0, "soma": 0.0, "isencoes": 0, "inativas": 0}
    if grupo is not None:
        itens = (
            ItemFracaoUnidade.query.filter_by(grupo_fracao_id=grupo.id)
            .join(Unidade, Unidade.id == ItemFracaoUnidade.unidade_id)
            .filter(Unidade.condominio_id == condominio_id, Unidade.eh_setor_interno.is_(False))
            .order_by(Unidade.bloco.asc(), Unidade.apartamento.asc())
            .all()
        )
        soma = sum(float(item.valor_base or 0) for item in itens if item.ativa_no_rateio)
        presentes = set()
        for item in itens:
            unidade = item.unidade
            presentes.add(unidade.id)
            ativa = bool(item.ativa_no_rateio)
            tem_isencao = (item.isencao_tipo or TipoIsencaoFracao.NENHUMA) != TipoIsencaoFracao.NENHUMA and float(
                item.isencao_valor or 0
            ) > 0
            if ativa:
                resumo["ativas"] += 1
                resumo["soma"] += float(item.valor_base or 0)
            else:
                resumo["inativas"] += 1
            if tem_isencao:
                resumo["isencoes"] += 1
            nome = _nome_responsavel(unidade)
            if not nome:
                from app.planta_unidades import nome_pagador_publico

                nome = nome_pagador_publico(unidade)
            linhas.append(
                {
                    "item": item,
                    "bloco": unidade.bloco,
                    "apartamento": unidade.apartamento,
                    "responsavel": nome,
                    "busca": f"{unidade.bloco} {unidade.apartamento} {nome}".lower(),
                    "coeficiente": coeficiente_percentual(
                        item.valor_base, ativa, grupo.modo_calculo, soma
                    ),
                    "peso_efetivo": peso_efetivo(
                        item.valor_base, ativa, item.isencao_tipo, item.isencao_valor
                    ),
                }
            )
        novas = [
            unidade
            for unidade in unidades_residenciais(condominio_id)
            if unidade.id not in presentes
        ]
    return render_template(
        "admin/financeiro/fracoes.html",
        grupos=grupos,
        grupo=grupo,
        linhas=linhas,
        novas=novas,
        resumo=resumo,
        blocos=get_blocos(),
        titulo_sugerido=TITULO_IGUALITARIA,
    )


@admin_required
def admin_financeiro_fracao_nova():
    condominio_id = condominio_id_obrigatorio()
    titulo = (request.form.get("titulo") or "").strip()
    if not titulo:
        flash("Informe o título da fração.", "warning")
        return redirect(url_for("admin_financeiro_fracoes"))
    modo = (request.form.get("modo_calculo") or ModoFracao.VALOR).strip()
    if modo not in (ModoFracao.VALOR, ModoFracao.PROPORCAO):
        modo = ModoFracao.VALOR
    agora = datetime.utcnow()
    grupo = GrupoFracao(
        condominio_id=condominio_id,
        titulo=titulo[:160],
        modo_calculo=modo,
        redistribuir_isencoes=True,
        padrao=False,
        criado_em=agora,
        atualizado_em=agora,
    )
    db.session.add(grupo)
    db.session.flush()
    if _marcado("padrao"):
        _definir_padrao(grupo)
    incluir_unidades(grupo, unidades_residenciais(condominio_id), 1.0)
    _auditar(f"Fração #{grupo.id} criada.")
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Não foi possível criar a fração.", "danger")
        return redirect(url_for("admin_financeiro_fracoes"))
    flash("Fração criada com peso 1,0 em todas as unidades residenciais.", "success")
    return redirect(url_for("admin_financeiro_fracoes", grupo=grupo.id))


@admin_required
def admin_financeiro_fracao_salvar(grupo_id):
    condominio_id = condominio_id_obrigatorio()
    grupo = _grupo_do_tenant(grupo_id, condominio_id)
    titulo = (request.form.get("titulo") or "").strip()
    if not titulo:
        flash("Informe o título da fração.", "warning")
        return redirect(url_for("admin_financeiro_fracoes", grupo=grupo.id))
    modo = (request.form.get("modo_calculo") or ModoFracao.VALOR).strip()
    if modo not in (ModoFracao.VALOR, ModoFracao.PROPORCAO):
        flash("Modo de cálculo inválido.", "warning")
        return redirect(url_for("admin_financeiro_fracoes", grupo=grupo.id))
    grupo.titulo = titulo[:160]
    grupo.modo_calculo = modo
    grupo.redistribuir_isencoes = _marcado("redistribuir_isencoes")
    grupo.atualizado_em = datetime.utcnow()
    if _marcado("padrao"):
        _definir_padrao(grupo)
    elif grupo.padrao:
        grupo.padrao = False

    itens = ItemFracaoUnidade.query.filter_by(grupo_fracao_id=grupo.id).all()
    try:
        for item in itens:
            if f"item_{item.id}_valor" not in request.form:
                continue
            item.valor_base = _parse_peso(request.form.get(f"item_{item.id}_valor"))
            item.ativa_no_rateio = _marcado(f"item_{item.id}_ativa")
            tipo = (request.form.get(f"item_{item.id}_isencao") or TipoIsencaoFracao.NENHUMA).strip()
            if tipo not in (
                TipoIsencaoFracao.NENHUMA,
                TipoIsencaoFracao.PERCENTUAL,
                TipoIsencaoFracao.VALOR_FIXO,
            ):
                raise ValueError("Tipo de isenção inválido.")
            item.isencao_tipo = tipo
            if tipo == TipoIsencaoFracao.NENHUMA:
                item.isencao_valor = 0.0
                item.motivo_isencao = None
            else:
                item.isencao_valor = _parse_peso(request.form.get(f"item_{item.id}_isencao_valor") or "0")
                motivo = (request.form.get(f"item_{item.id}_motivo") or "").strip()
                item.motivo_isencao = motivo[:120] or None
    except ValueError:
        db.session.rollback()
        flash("Revise os pesos e os valores de isenção. Use número com vírgula ou ponto.", "warning")
        return redirect(url_for("admin_financeiro_fracoes", grupo=grupo.id))
    _auditar(f"Fração #{grupo.id} atualizada.")
    db.session.commit()
    flash("Alterações da fração salvas.", "success")
    return redirect(url_for("admin_financeiro_fracoes", grupo=grupo.id))


@admin_required
def admin_financeiro_fracao_excluir(grupo_id):
    condominio_id = condominio_id_obrigatorio()
    grupo = _grupo_do_tenant(grupo_id, condominio_id)
    usado = RateioCondominio.query.filter_by(
        condominio_id=condominio_id, grupo_fracao_id=grupo.id
    ).first()
    if usado is not None:
        flash("Esta fração já foi usada em um rateio e não pode ser excluída.", "warning")
        return redirect(url_for("admin_financeiro_fracoes", grupo=grupo.id))
    identificador = grupo.id
    ItemFracaoUnidade.query.filter_by(grupo_fracao_id=grupo.id).delete(synchronize_session=False)
    db.session.delete(grupo)
    _auditar(f"Fração #{identificador} excluída.")
    db.session.commit()
    flash("Fração excluída.", "success")
    return redirect(url_for("admin_financeiro_fracoes"))


def _itens_residenciais(grupo, condominio_id):
    return (
        ItemFracaoUnidade.query.filter_by(grupo_fracao_id=grupo.id)
        .join(Unidade, Unidade.id == ItemFracaoUnidade.unidade_id)
        .filter(Unidade.condominio_id == condominio_id, Unidade.eh_setor_interno.is_(False))
        .all()
    )


@admin_required
def admin_financeiro_fracao_peso_igual(grupo_id):
    condominio_id = condominio_id_obrigatorio()
    grupo = _grupo_do_tenant(grupo_id, condominio_id)
    grupo.modo_calculo = ModoFracao.VALOR
    for item in _itens_residenciais(grupo, condominio_id):
        item.valor_base = 1.0
        item.ativa_no_rateio = True
    grupo.atualizado_em = datetime.utcnow()
    _auditar(f"Fração #{grupo.id} atualizada.")
    db.session.commit()
    flash("Peso 1,0 aplicado e todas as unidades desta fração foram reativadas.", "success")
    return redirect(url_for("admin_financeiro_fracoes", grupo=grupo.id))


@admin_required
def admin_financeiro_fracao_ativar_bloco(grupo_id):
    condominio_id = condominio_id_obrigatorio()
    grupo = _grupo_do_tenant(grupo_id, condominio_id)
    bloco = (request.form.get("bloco") or "").strip()
    if bloco not in get_blocos():
        flash("Escolha um bloco válido.", "warning")
        return redirect(url_for("admin_financeiro_fracoes", grupo=grupo.id))
    itens = (
        ItemFracaoUnidade.query.filter_by(grupo_fracao_id=grupo.id)
        .join(Unidade, Unidade.id == ItemFracaoUnidade.unidade_id)
        .filter(Unidade.condominio_id == condominio_id)
        .all()
    )
    for item in itens:
        item.ativa_no_rateio = item.unidade.bloco == bloco
    grupo.atualizado_em = datetime.utcnow()
    _auditar(f"Fração #{grupo.id} atualizada.")
    db.session.commit()
    flash(f"Nesta fração, só o bloco {bloco} permanece ativo.", "success")
    return redirect(url_for("admin_financeiro_fracoes", grupo=grupo.id))


@admin_required
def admin_financeiro_fracao_ativar_todas(grupo_id):
    condominio_id = condominio_id_obrigatorio()
    grupo = _grupo_do_tenant(grupo_id, condominio_id)
    quantidade = 0
    for item in _itens_residenciais(grupo, condominio_id):
        item.ativa_no_rateio = True
        quantidade += 1
    grupo.atualizado_em = datetime.utcnow()
    _auditar(f"Fração #{grupo.id} atualizada.")
    db.session.commit()
    flash(f"Todas as {quantidade} unidades residenciais desta fração foram reativadas.", "success")
    return redirect(url_for("admin_financeiro_fracoes", grupo=grupo.id))


@admin_required
def admin_financeiro_fracao_incluir_novas(grupo_id):
    condominio_id = condominio_id_obrigatorio()
    grupo = _grupo_do_tenant(grupo_id, condominio_id)
    quantidade = incluir_unidades(grupo, unidades_residenciais(condominio_id), 1.0)
    if quantidade:
        grupo.atualizado_em = datetime.utcnow()
        _auditar(f"Fração #{grupo.id} atualizada.")
        db.session.commit()
        flash(f"{quantidade} unidade(s) incluída(s) com peso 1,0.", "success")
    else:
        flash("Não há unidade nova para incluir.", "info")
    return redirect(url_for("admin_financeiro_fracoes", grupo=grupo.id))


def register(app):
    app.add_url_rule(
        "/admin/financeiro/fracoes",
        "admin_financeiro_fracoes",
        admin_financeiro_fracoes,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/financeiro/fracoes/nova",
        "admin_financeiro_fracao_nova",
        admin_financeiro_fracao_nova,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/fracoes/<int:grupo_id>/salvar",
        "admin_financeiro_fracao_salvar",
        admin_financeiro_fracao_salvar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/fracoes/<int:grupo_id>/excluir",
        "admin_financeiro_fracao_excluir",
        admin_financeiro_fracao_excluir,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/fracoes/<int:grupo_id>/peso-igual",
        "admin_financeiro_fracao_peso_igual",
        admin_financeiro_fracao_peso_igual,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/fracoes/<int:grupo_id>/ativar-bloco",
        "admin_financeiro_fracao_ativar_bloco",
        admin_financeiro_fracao_ativar_bloco,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/fracoes/<int:grupo_id>/ativar-todas",
        "admin_financeiro_fracao_ativar_todas",
        admin_financeiro_fracao_ativar_todas,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/fracoes/<int:grupo_id>/incluir-novas",
        "admin_financeiro_fracao_incluir_novas",
        admin_financeiro_fracao_incluir_novas,
        methods=["POST"],
    )
