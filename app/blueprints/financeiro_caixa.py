"""Livro-caixa unificado e fechamento mensal da prestação de contas."""

import json
from datetime import datetime

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from app import db
from app.auth import admin_required, get_current_user, sindico_required
from app.financeiro_fechamento import (
    apurar_competencia,
    competencia_esta_fechada,
    mensagem_competencia_fechada,
)
from app.models import (
    ContaBancaria,
    FechamentoMensal,
    FundoFinanceiro,
    LancamentoCaixaAvulso,
    PlanoConta,
    RepasseBloco,
    StatusCobranca,
    StatusDespesa,
    StatusRepasse,
    TipoLancamentoCaixa,
    CobrancaUnidade,
    DespesaPagamento,
)
from app.utils import get_blocos


def _parse_valor(texto):
    from app.blueprints.financeiro import _parse_valor as parse

    return parse(texto)


def _parse_data(texto):
    from app.blueprints.financeiro import _parse_data as parse

    return parse(texto)


def _hoje():
    from app.blueprints.financeiro import _hoje as hoje

    return hoje()


def _competencia_de(data_ref):
    from app.blueprints.financeiro import _competencia_de as competencia

    return competencia(data_ref)


def _deslocar(competencia, delta):
    from app.blueprints.financeiro import _deslocar_competencia

    return _deslocar_competencia(competencia, delta)


def _rotulo(competencia):
    from app.blueprints.financeiro import _rotulo_competencia_longo

    return _rotulo_competencia_longo(competencia)


def _auditar(mensagem):
    from app.routes import _registrar_auditoria

    usuario = get_current_user()
    if usuario is not None:
        _registrar_auditoria(usuario, mensagem)


def _competencia_valida(texto):
    bruto = (texto or "").strip()
    if len(bruto) != 7 or bruto[2] != "/":
        return None
    try:
        mes = int(bruto[:2])
        ano = int(bruto[3:])
    except ValueError:
        return None
    if mes < 1 or mes > 12 or ano < 2000 or ano > 2100:
        return None
    return f"{mes:02d}/{ano}"


def _nome_usuario():
    usuario = get_current_user()
    if usuario is None:
        return ""
    return (usuario.username or "")[:80]


def _recusar(condominio_id, competencia):
    if competencia_esta_fechada(condominio_id, competencia):
        flash(mensagem_competencia_fechada(competencia), "warning")
        return True
    return False


def _destino_lancamentos(competencia, conta="", fundo="", tipo="", bloco=""):
    return redirect(
        url_for(
            "admin_financeiro_lancamentos",
            competencia=competencia,
            conta=conta or None,
            fundo=fundo or None,
            tipo=tipo or None,
            bloco=bloco or None,
        )
    )


def _linhas_livro(condominio_id, competencia, conta_id, fundo_id, tipo, bloco, titulo_id=None):
    linhas = []

    def entra_tipo(grupo):
        return tipo in ("", "todos", grupo)

    def entra_conta(valor):
        return conta_id is None or valor == conta_id

    def entra_fundo(valor):
        return fundo_id is None or valor == fundo_id

    def entra_bloco(valor):
        return not bloco or str(valor or "") == bloco

    if entra_tipo("entradas"):
        for cobranca in CobrancaUnidade.query.filter_by(
            condominio_id=condominio_id,
            competencia=competencia,
            status=StatusCobranca.PAGA,
        ).all():
            bloco_unidade = cobranca.unidade.bloco if cobranca.unidade else ""
            if titulo_id is not None and cobranca.id != titulo_id:
                continue
            if not entra_conta(cobranca.conta_bancaria_id) or not entra_bloco(bloco_unidade):
                continue
            fundo_nome = ""
            fundo_linha = None
            composicao = cobranca.composicao_json or []
            if isinstance(composicao, list) and composicao and isinstance(composicao[0], dict):
                fundo_nome = composicao[0].get("fundo_nome") or ""
                plano = PlanoConta.query.filter_by(
                    id=composicao[0].get("plano_conta_id"), condominio_id=condominio_id
                ).first()
                if plano is not None:
                    fundo_linha = plano.fundo_id
                    if not fundo_nome and plano.fundo is not None:
                        fundo_nome = plano.fundo.nome
            if not entra_fundo(fundo_linha):
                continue
            linhas.append(
                {
                    "data": cobranca.data_pagamento or cobranca.vencimento,
                    "descricao": cobranca.titulo,
                    "badge": "Entrada (Boleto)",
                    "cor": "success",
                    "conta": cobranca.conta_bancaria.nome_banco if cobranca.conta_bancaria else "",
                    "fundo": fundo_nome,
                    "bloco": bloco_unidade,
                    "entrada": round(float(cobranca.valor_pago or 0), 2),
                    "saida": 0.0,
                    "transferencia": 0.0,
                }
            )
        if titulo_id is None:
            for avulso in LancamentoCaixaAvulso.query.filter_by(
                condominio_id=condominio_id,
                competencia=competencia,
                tipo=TipoLancamentoCaixa.ENTRADA,
            ).all():
                if not entra_conta(avulso.conta_bancaria_id) or not entra_fundo(avulso.fundo_id):
                    continue
                if not entra_bloco(avulso.bloco_escopo):
                    continue
                linhas.append(_linha_avulso(avulso, "Entrada (Avulso)", "primary"))

    if titulo_id is None and entra_tipo("saidas"):
        for despesa in DespesaPagamento.query.filter_by(
            condominio_id=condominio_id,
            competencia=competencia,
            status=StatusDespesa.PAGO,
        ).all():
            if not entra_conta(despesa.conta_bancaria_id) or not entra_fundo(despesa.fundo_id):
                continue
            if not entra_bloco(despesa.bloco_alocado):
                continue
            linhas.append(
                {
                    "data": despesa.data_pagamento or despesa.vencimento,
                    "descricao": despesa.titulo,
                    "badge": "Saída (Despesa)",
                    "cor": "danger",
                    "conta": despesa.conta_bancaria.nome_banco if despesa.conta_bancaria else "",
                    "fundo": despesa.fundo.nome if despesa.fundo else "",
                    "bloco": despesa.bloco_alocado,
                    "entrada": 0.0,
                    "saida": round(float(despesa.valor_pago or despesa.valor_original or 0), 2),
                    "transferencia": 0.0,
                }
            )
        for repasse in RepasseBloco.query.filter_by(
            condominio_id=condominio_id,
            competencia=competencia,
            status=StatusRepasse.REALIZADO,
        ).all():
            if not entra_conta(repasse.conta_bancaria_id) or not entra_bloco(repasse.bloco):
                continue
            if fundo_id is not None:
                caixa = FundoFinanceiro.query.filter_by(condominio_id=condominio_id, codigo="1").first()
                if caixa is None or caixa.id != fundo_id:
                    continue
            linhas.append(
                {
                    "data": repasse.data_repasse,
                    "descricao": f"Repasse bloco {repasse.bloco}",
                    "badge": "Saída (Repasse Bloco)",
                    "cor": "warning",
                    "conta": repasse.conta_bancaria.nome_banco if repasse.conta_bancaria else "",
                    "fundo": "1 - CAIXA",
                    "bloco": repasse.bloco,
                    "entrada": 0.0,
                    "saida": round(float(repasse.valor_repassado or 0), 2),
                    "transferencia": 0.0,
                }
            )
        for avulso in LancamentoCaixaAvulso.query.filter_by(
            condominio_id=condominio_id,
            competencia=competencia,
            tipo=TipoLancamentoCaixa.SAIDA,
        ).all():
            if not entra_conta(avulso.conta_bancaria_id) or not entra_fundo(avulso.fundo_id):
                continue
            if not entra_bloco(avulso.bloco_escopo):
                continue
            linhas.append(_linha_avulso(avulso, "Saída (Avulso)", "primary"))

    if titulo_id is None and entra_tipo("transferencias"):
        for avulso in LancamentoCaixaAvulso.query.filter_by(
            condominio_id=condominio_id,
            competencia=competencia,
            tipo=TipoLancamentoCaixa.TRANSFERENCIA_FUNDO,
        ).all():
            if not entra_conta(avulso.conta_bancaria_id) or not entra_bloco(avulso.bloco_escopo):
                continue
            if fundo_id is not None and fundo_id not in (avulso.fundo_id, avulso.fundo_destino_id):
                continue
            linhas.append(_linha_avulso(avulso, "Transferência entre fundos", "info"))

    linhas.sort(key=lambda item: (item["data"] or datetime.min.date(), item["descricao"]))
    return linhas


def _linha_avulso(avulso, badge, cor):
    origem = avulso.fundo.nome if avulso.fundo else ""
    if avulso.fundo_destino is not None:
        origem = f"{origem} → {avulso.fundo_destino.nome}"
    transferencia = avulso.tipo == TipoLancamentoCaixa.TRANSFERENCIA_FUNDO
    return {
        "data": avulso.data_lancamento,
        "descricao": avulso.descricao,
        "badge": badge,
        "cor": cor,
        "conta": avulso.conta_bancaria.nome_banco if avulso.conta_bancaria else "",
        "fundo": origem,
        "bloco": avulso.bloco_escopo,
        "entrada": round(avulso.valor, 2) if avulso.tipo == TipoLancamentoCaixa.ENTRADA else 0.0,
        "saida": round(avulso.valor, 2) if avulso.tipo == TipoLancamentoCaixa.SAIDA else 0.0,
        "transferencia": round(avulso.valor, 2) if transferencia else 0.0,
    }


@admin_required
def painel_lancamentos():
    usuario = get_current_user()
    condominio_id = usuario.condominio_id
    competencia = _competencia_valida(request.args.get("competencia")) or _competencia_de(_hoje())
    conta_id = request.args.get("conta", type=int)
    fundo_id = request.args.get("fundo", type=int)
    tipo = (request.args.get("tipo") or "todos").strip()
    if tipo not in ("todos", "entradas", "saidas", "transferencias"):
        tipo = "todos"
    bloco = (request.args.get("bloco") or "").strip()
    if bloco and bloco != "GERAL" and bloco not in get_blocos():
        bloco = ""
    titulo_id = request.args.get("titulo", type=int)
    return render_template(
        "admin/financeiro/lancamentos.html",
        competencia=competencia,
        competencia_rotulo=_rotulo(competencia),
        competencia_anterior=_deslocar(competencia, -1),
        competencia_proxima=_deslocar(competencia, 1),
        contas=ContaBancaria.query.filter_by(condominio_id=condominio_id, ativa=True).order_by(ContaBancaria.nome_banco.asc()).all(),
        fundos=FundoFinanceiro.query.filter_by(condominio_id=condominio_id).order_by(FundoFinanceiro.codigo.asc()).all(),
        planos=PlanoConta.query.filter_by(condominio_id=condominio_id).order_by(PlanoConta.codigo.asc()).all(),
        blocos=get_blocos(),
        conta_id=conta_id,
        fundo_id=fundo_id,
        tipo=tipo,
        bloco=bloco,
        linhas=_linhas_livro(
            condominio_id, competencia, conta_id, fundo_id, tipo, bloco, titulo_id
        ),
        fechada=competencia_esta_fechada(condominio_id, competencia),
    )


def _ler_comum(condominio_id):
    competencia = _competencia_valida(request.form.get("competencia"))
    data = _parse_data(request.form.get("data_lancamento"))
    descricao = (request.form.get("descricao") or "").strip()
    bloco = (request.form.get("bloco_escopo") or "GERAL").strip()
    if competencia is None or data is None or not descricao:
        raise ValueError("Informe competência, data e descrição.")
    if bloco != "GERAL" and bloco not in get_blocos():
        raise ValueError("Escolha um bloco válido.")
    try:
        valor = round(_parse_valor(request.form.get("valor") or "0"), 2)
    except (TypeError, ValueError):
        raise ValueError("Informe um valor válido.")
    if valor <= 0:
        raise ValueError("O valor precisa ser maior que zero.")
    conta = ContaBancaria.query.filter_by(
        id=request.form.get("conta_bancaria_id", type=int),
        condominio_id=condominio_id,
        ativa=True,
    ).first()
    fundo = FundoFinanceiro.query.filter_by(
        id=request.form.get("fundo_id", type=int),
        condominio_id=condominio_id,
    ).first()
    if conta is None or fundo is None:
        raise ValueError("Escolha a conta bancária e o fundo.")
    plano_id = request.form.get("plano_conta_id", type=int)
    if plano_id and PlanoConta.query.filter_by(id=plano_id, condominio_id=condominio_id).first() is None:
        plano_id = None
    return competencia, data, descricao[:200], bloco, valor, conta, fundo, plano_id


@admin_required
def salvar_lancamento():
    usuario = get_current_user()
    tipo = (request.form.get("tipo") or "").strip()
    if tipo not in (TipoLancamentoCaixa.ENTRADA, TipoLancamentoCaixa.SAIDA):
        flash("Escolha entrada ou saída.", "danger")
        return redirect(url_for("admin_financeiro_lancamentos"))
    try:
        competencia, data, descricao, bloco, valor, conta, fundo, plano_id = _ler_comum(
            usuario.condominio_id
        )
    except ValueError as erro:
        flash(str(erro), "danger")
        return redirect(url_for("admin_financeiro_lancamentos"))
    if _recusar(usuario.condominio_id, competencia):
        return _destino_lancamentos(competencia)
    lancamento = LancamentoCaixaAvulso(
        condominio_id=usuario.condominio_id,
        conta_bancaria_id=conta.id,
        plano_conta_id=plano_id,
        fundo_id=fundo.id,
        tipo=tipo,
        competencia=competencia,
        data_lancamento=data,
        descricao=descricao,
        bloco_escopo=bloco,
        valor=valor,
        criado_por=_nome_usuario(),
        criado_em=datetime.utcnow(),
    )
    sinal = 1 if tipo == TipoLancamentoCaixa.ENTRADA else -1
    conta.saldo_atual = round(float(conta.saldo_atual or 0) + (sinal * valor), 2)
    db.session.add(lancamento)
    _auditar(f"Lançamento de caixa {tipo} de {valor:.2f} na competência {competencia}.")
    db.session.commit()
    flash("Lançamento registrado no livro-caixa.", "success")
    return _destino_lancamentos(competencia)


@admin_required
def transferir_fundos():
    usuario = get_current_user()
    try:
        competencia, data, descricao, bloco, valor, conta, fundo, plano_id = _ler_comum(
            usuario.condominio_id
        )
    except ValueError as erro:
        flash(str(erro), "danger")
        return redirect(url_for("admin_financeiro_lancamentos"))
    destino = FundoFinanceiro.query.filter_by(
        id=request.form.get("fundo_destino_id", type=int),
        condominio_id=usuario.condominio_id,
    ).first()
    if destino is None or destino.id == fundo.id:
        flash("Escolha um fundo de destino diferente da origem.", "warning")
        return _destino_lancamentos(competencia)
    if _recusar(usuario.condominio_id, competencia):
        return _destino_lancamentos(competencia)
    db.session.add(
        LancamentoCaixaAvulso(
            condominio_id=usuario.condominio_id,
            conta_bancaria_id=conta.id,
            plano_conta_id=plano_id,
            fundo_id=fundo.id,
            fundo_destino_id=destino.id,
            tipo=TipoLancamentoCaixa.TRANSFERENCIA_FUNDO,
            competencia=competencia,
            data_lancamento=data,
            descricao=descricao,
            bloco_escopo=bloco,
            valor=valor,
            criado_por=_nome_usuario(),
            criado_em=datetime.utcnow(),
        )
    )
    _auditar(f"Transferência entre fundos na competência {competencia}.")
    db.session.commit()
    flash("Transferência registrada. O saldo bancário da conta não foi alterado.", "success")
    return _destino_lancamentos(competencia)


@admin_required
def painel_fechamento():
    usuario = get_current_user()
    hoje = _hoje()
    try:
        ano = int(request.args.get("ano") or hoje.year)
    except ValueError:
        ano = hoje.year
    if ano < 2000 or ano > 2100:
        ano = hoje.year
    competencia = _competencia_valida(request.args.get("competencia")) or _competencia_de(hoje)
    if int(competencia[3:]) != ano:
        competencia = f"{competencia[:2]}/{ano}"
    fechamentos = {
        item.competencia: item
        for item in FechamentoMensal.query.filter_by(condominio_id=usuario.condominio_id).all()
        if item.competencia.endswith(f"/{ano}")
    }
    meses = []
    for mes in range(1, 13):
        chave = f"{mes:02d}/{ano}"
        registro = fechamentos.get(chave)
        meses.append(
            {
                "competencia": chave,
                "rotulo": _rotulo(chave),
                "fechado": bool(registro and registro.fechado),
            }
        )
    return render_template(
        "admin/financeiro/fechamento.html",
        ano=ano,
        anos=sorted({hoje.year - 1, hoje.year, hoje.year + 1, ano}),
        meses=meses,
        competencia=competencia,
        competencia_rotulo=_rotulo(competencia),
        apuracao=apurar_competencia(usuario.condominio_id, competencia, hoje=hoje),
        registro=FechamentoMensal.query.filter_by(
            condominio_id=usuario.condominio_id, competencia=competencia
        ).first(),
    )


@admin_required
def fechar_competencia():
    usuario = get_current_user()
    competencia = _competencia_valida(request.form.get("competencia"))
    if competencia is None:
        flash("Informe a competência.", "danger")
        return redirect(url_for("admin_financeiro_fechamento"))
    if competencia_esta_fechada(usuario.condominio_id, competencia):
        flash("Esta competência já está fechada.", "warning")
        return redirect(url_for("admin_financeiro_fechamento", competencia=competencia))
    apuracao = apurar_competencia(usuario.condominio_id, competencia, hoje=_hoje())
    registro = FechamentoMensal.query.filter_by(
        condominio_id=usuario.condominio_id, competencia=competencia
    ).first()
    if registro is None:
        registro = FechamentoMensal(
            condominio_id=usuario.condominio_id, competencia=competencia
        )
        db.session.add(registro)
    registro.fechado = True
    registro.saldo_inicial_mes = apuracao["saldo_inicial"]
    registro.total_receitas = apuracao["total_receitas"]
    registro.total_despesas = apuracao["total_despesas"]
    registro.total_repasses = apuracao["total_repasses"]
    registro.saldo_final_mes = apuracao["saldo_final"]
    registro.resumo_snapshot_json = json.dumps(apuracao, ensure_ascii=False)
    registro.fechado_por = _nome_usuario()
    registro.fechado_em = datetime.utcnow()
    registro.motivo_reabertura = None
    try:
        _auditar(f"Competência {competencia} fechada para prestação de contas.")
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Não foi possível fechar a competência. Tente novamente.", "danger")
        return redirect(url_for("admin_financeiro_fechamento", competencia=competencia))
    flash(f"Competência {competencia} fechada.", "success")
    return redirect(url_for("admin_financeiro_fechamento", competencia=competencia))


@admin_required
def reabrir_competencia():
    usuario = get_current_user()
    competencia = _competencia_valida(request.form.get("competencia"))
    motivo = (request.form.get("motivo_reabertura") or "").strip()
    if competencia is None or not motivo:
        flash("Informe a competência e a justificativa da reabertura.", "warning")
        return redirect(url_for("admin_financeiro_fechamento"))
    registro = FechamentoMensal.query.filter_by(
        condominio_id=usuario.condominio_id, competencia=competencia, fechado=True
    ).first()
    if registro is None:
        flash("Esta competência não está fechada.", "warning")
        return redirect(url_for("admin_financeiro_fechamento", competencia=competencia))
    registro.fechado = False
    registro.motivo_reabertura = motivo[:300]
    _auditar(f"Competência {competencia} reaberta.")
    db.session.commit()
    flash(f"Competência {competencia} reaberta.", "success")
    return redirect(url_for("admin_financeiro_fechamento", competencia=competencia))


def _balancete(condominio, competencia, blocos, restrito):
    registro = FechamentoMensal.query.filter_by(
        condominio_id=condominio.id, competencia=competencia, fechado=True
    ).first()
    if registro is not None and registro.resumo_snapshot_json and not restrito:
        try:
            apuracao = json.loads(registro.resumo_snapshot_json)
        except json.JSONDecodeError:
            apuracao = apurar_competencia(condominio.id, competencia, hoje=_hoje())
    else:
        apuracao = apurar_competencia(condominio.id, competencia, blocos=blocos, hoje=_hoje())
    return render_template(
        "admin/financeiro/balancete.html",
        condominio=condominio,
        competencia=competencia,
        competencia_rotulo=_rotulo(competencia),
        emitido=_hoje().strftime("%d/%m/%Y"),
        apuracao=apuracao,
        restrito=restrito,
        blocos_rotulo=", ".join(blocos) if restrito and blocos else "",
    )


@admin_required
def balancete_admin(competencia):
    usuario = get_current_user()
    chave = _competencia_valida((competencia or "").replace("-", "/"))
    if chave is None:
        abort(404)
    return _balancete(usuario.condominio, chave, None, False)


@sindico_required
def balancete_sindico():
    usuario = get_current_user()
    chave = _competencia_valida(request.args.get("competencia")) or _competencia_de(_hoje())
    blocos = usuario.get_blocos_permitidos()
    if blocos is None:
        blocos = get_blocos()
    if not blocos:
        flash("Não há bloco no seu mandato para imprimir o demonstrativo.", "warning")
        return redirect(url_for("sindico_financeiro"))
    return _balancete(usuario.condominio, chave, blocos, True)


def register(app):
    app.add_url_rule(
        "/admin/financeiro/lancamentos",
        "admin_financeiro_lancamentos",
        painel_lancamentos,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/financeiro/lancamentos/avulso",
        "admin_financeiro_lancamento_avulso",
        salvar_lancamento,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/lancamentos/transferir",
        "admin_financeiro_lancamento_transferir",
        transferir_fundos,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/fechamento",
        "admin_financeiro_fechamento",
        painel_fechamento,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/financeiro/fechamento/fechar",
        "admin_financeiro_fechamento_fechar",
        fechar_competencia,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/fechamento/reabrir",
        "admin_financeiro_fechamento_reabrir",
        reabrir_competencia,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/fechamento/<competencia>/balancete",
        "admin_financeiro_balancete",
        balancete_admin,
        methods=["GET"],
    )
    app.add_url_rule(
        "/sindico/financeiro/balancete",
        "sindico_financeiro_balancete",
        balancete_sindico,
        methods=["GET"],
    )
