"""Previsão orçamentária: metas mensais e comparativo com o realizado."""

import json
from datetime import datetime

from flask import flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from app import db
from app.auth import admin_required, get_current_user
from app.financeiro_orcamento import (
    ESCOPO_CONSOLIDADO,
    ESCOPO_GERAL,
    MESES,
    NOMES_MESES,
    ler_meses,
    montar_comparativo,
    realizado_por_plano,
)
from app.models import PlanoConta, PrevisaoOrcamentaria
from app.utils import get_blocos


def _numero(texto):
    bruto = str(texto or "").strip().replace(" ", "")
    if not bruto:
        return None
    if "," in bruto and "." in bruto:
        bruto = bruto.replace(".", "").replace(",", ".")
    else:
        bruto = bruto.replace(",", ".")
    valor = float(bruto)
    if valor < 0 or valor > 100_000_000:
        raise ValueError("fora do intervalo")
    return round(valor, 2)


def _ano_valido(texto):
    try:
        ano = int(texto)
    except (TypeError, ValueError):
        return None
    if ano < 2000 or ano > 2100:
        return None
    return ano


def _escopo_valido(texto, gravavel=False):
    escopo = (texto or "").strip()
    if escopo == ESCOPO_CONSOLIDADO:
        return None if gravavel else ESCOPO_CONSOLIDADO
    if escopo == ESCOPO_GERAL or escopo in get_blocos():
        return escopo
    return None


def _destino(ano, mes, escopo):
    return redirect(url_for("admin_financeiro_orcamento", ano=ano, mes=mes, escopo=escopo))


def _auditar(mensagem):
    from app.routes import _registrar_auditoria

    usuario = get_current_user()
    if usuario is not None:
        _registrar_auditoria(usuario, mensagem)


@admin_required
def painel_orcamento():
    usuario = get_current_user()
    hoje = datetime.utcnow()
    ano = _ano_valido(request.args.get("ano")) or hoje.year
    mes = (request.args.get("mes") or f"{hoje.month:02d}").strip()
    if mes != "anual" and mes not in MESES:
        mes = f"{hoje.month:02d}"
    escopo = _escopo_valido(request.args.get("escopo")) or ESCOPO_CONSOLIDADO
    comparativo = montar_comparativo(usuario.condominio_id, ano, mes, escopo)
    planos = (
        PlanoConta.query.filter_by(condominio_id=usuario.condominio_id)
        .order_by(PlanoConta.codigo.asc())
        .all()
    )
    metas = {}
    if escopo != ESCOPO_CONSOLIDADO:
        for previsao in PrevisaoOrcamentaria.query.filter_by(
            condominio_id=usuario.condominio_id, ano=ano, bloco_escopo=escopo
        ).all():
            valores = ler_meses(previsao.valores_mensais_json)
            unicos = set(valores.values())
            metas[previsao.plano_conta_id] = {
                "uniforme": len(unicos) == 1,
                "valor": next(iter(unicos)) if len(unicos) == 1 else 0.0,
            }
    anos = {hoje.year - 1, hoje.year, hoje.year + 1, hoje.year + 2, ano}
    return render_template(
        "admin/financeiro/orcamento.html",
        ano=ano,
        mes=mes,
        escopo=escopo,
        anos=sorted(anos),
        meses=MESES,
        nomes_meses=NOMES_MESES,
        blocos=get_blocos(),
        planos=planos,
        metas=metas,
        comparativo=comparativo,
        pode_editar=escopo != ESCOPO_CONSOLIDADO,
    )


def _gravar_meses(usuario, ano, escopo, plano, valores):
    previsao = PrevisaoOrcamentaria.query.filter_by(
        condominio_id=usuario.condominio_id,
        ano=ano,
        plano_conta_id=plano.id,
        bloco_escopo=escopo,
    ).first()
    if previsao is None:
        previsao = PrevisaoOrcamentaria(
            condominio_id=usuario.condominio_id,
            ano=ano,
            plano_conta_id=plano.id,
            bloco_escopo=escopo,
        )
        db.session.add(previsao)
    previsao.valores_mensais_json = json.dumps(valores)
    previsao.valor_anual_total = round(sum(valores.values()), 2)
    previsao.atualizado_em = datetime.utcnow()
    return previsao


@admin_required
def salvar_orcamento():
    usuario = get_current_user()
    ano = _ano_valido(request.form.get("ano"))
    mes = (request.form.get("mes") or "anual").strip()
    escopo = _escopo_valido(request.form.get("escopo"), gravavel=True)
    if ano is None or escopo is None:
        flash("Escolha o ano e um escopo gravável: Administração Geral ou um bloco.", "warning")
        return _destino(ano or datetime.utcnow().year, mes if mes in MESES or mes == "anual" else "anual", ESCOPO_GERAL)
    planos = {
        plano.id: plano
        for plano in PlanoConta.query.filter_by(condominio_id=usuario.condominio_id).all()
    }
    acao = (request.form.get("acao") or "repetir").strip()
    try:
        if acao == "media":
            feito = realizado_por_plano(
                usuario.condominio_id, ano - 1, "anual", escopo, planos
            )
            gravados = 0
            for plano_id, total in feito.items():
                if total <= 0 or plano_id not in planos:
                    continue
                mensal = round(total / 12, 2)
                _gravar_meses(usuario, ano, escopo, planos[plano_id], {item: mensal for item in MESES})
                gravados += 1
            if not gravados:
                db.session.rollback()
                flash(f"Não há realizado em {ano - 1} neste escopo para calcular a média.", "warning")
                return _destino(ano, mes, escopo)
            _auditar(f"Orçamento {ano} do escopo {escopo} preenchido pela média de {ano - 1}.")
            db.session.commit()
            flash(f"{gravados} conta(s) preenchidas com a média mensal de {ano - 1}.", "success")
            return _destino(ano, mes, escopo)
        gravados = 0
        for plano_id, plano in planos.items():
            bruto = request.form.get(f"plano_{plano_id}")
            if bruto is None or not str(bruto).strip():
                continue
            mensal = _numero(bruto)
            if mensal is None:
                continue
            _gravar_meses(usuario, ano, escopo, plano, {item: mensal for item in MESES})
            gravados += 1
    except ValueError:
        db.session.rollback()
        flash("Revise os valores mensais. Use apenas números a partir de zero.", "danger")
        return _destino(ano, mes, escopo)
    except IntegrityError:
        db.session.rollback()
        flash("Não foi possível gravar a meta. Tente novamente.", "danger")
        return _destino(ano, mes, escopo)
    if not gravados:
        flash("Informe o valor mensal de ao menos uma conta.", "warning")
        return _destino(ano, mes, escopo)
    _auditar(f"Metas do orçamento {ano} gravadas no escopo {escopo}.")
    db.session.commit()
    flash("Metas repetidas nos 12 meses do ano.", "success")
    return _destino(ano, mes, escopo)


def register(app):
    app.add_url_rule(
        "/admin/financeiro/orcamento",
        "admin_financeiro_orcamento",
        painel_orcamento,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/financeiro/orcamento/salvar",
        "admin_financeiro_orcamento_salvar",
        salvar_orcamento,
        methods=["POST"],
    )
