"""Contas a pagar, repasse por bloco e prestação de contas do síndico."""

import os
import uuid

from flask import (
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from sqlalchemy import or_

from app import db
from app.auth import (
    admin_required,
    get_current_user,
    sindico_required,
)
from app.blueprints.financeiro import (
    _competencia_de,
    _condominio_atual,
    _conta_ativa,
    _deslocar_competencia,
    _hoje,
    _parse_data,
    _parse_valor,
    _rotulo_competencia_longo,
)
from app.financeiro_repasse import apurar_repasses_blocos
from app.models import (
    ContaBancaria,
    DespesaPagamento,
    FundoFinanceiro,
    PlanoConta,
    RepasseBloco,
    Role,
    StatusDespesa,
    StatusRepasse,
    StatusUnidade,
    Unidade,
    Fornecedor,
    CobrancaUnidade,
    StatusCobranca,
)
from app.utils import get_blocos

_OPERACOES = ("PIX", "Boleto - Título", "TED", "Débito Automático")
_EXTENSOES = {"pdf", "png", "jpg", "jpeg"}


def _resumo(despesa):
    valor = round(float(despesa.valor_original or 0), 2)
    impostos = round(float(despesa.valor_impostos or 0), 2)
    descontos = round(float(despesa.valor_descontos or 0), 2)
    acrescimos = round(float(despesa.valor_acrescimos or 0), 2)
    a_pagar = round(valor - impostos - descontos + acrescimos, 2)
    pago = round(float(despesa.valor_pago or 0), 2)
    return {
        "despesa": despesa,
        "valor": valor,
        "impostos": impostos,
        "descontos": descontos,
        "acrescimos": acrescimos,
        "a_pagar": a_pagar,
        "pago": pago,
        "divida": round(a_pagar - pago, 2),
    }


def _atualizar_despesas_vencidas(condominio_id, hoje):
    (
        DespesaPagamento.query.filter(
            DespesaPagamento.condominio_id == condominio_id,
            DespesaPagamento.status == StatusDespesa.A_VENCER,
            DespesaPagamento.vencimento < hoje,
        ).update({DespesaPagamento.status: StatusDespesa.VENCIDO}, synchronize_session=False)
    )
    db.session.commit()


def _pasta_anexos():
    pasta = current_app.config.get("UPLOAD_FINANCEIRO_FOLDER")
    if not pasta:
        pasta = os.path.join(current_app.root_path, "static", "uploads", "financeiro")
    os.makedirs(pasta, exist_ok=True)
    return os.path.realpath(pasta)


def _salvar_anexo(arquivo):
    if arquivo is None or not getattr(arquivo, "filename", ""):
        return None
    extensao = arquivo.filename.rsplit(".", 1)[-1].lower() if "." in arquivo.filename else ""
    if extensao not in _EXTENSOES:
        raise ValueError("Anexe um PDF ou uma imagem (png, jpg).")
    nome = f"{uuid.uuid4().hex}.{extensao}"
    arquivo.save(os.path.join(_pasta_anexos(), nome))
    return nome


def _caminho_anexo(nome):
    if not nome:
        abort(404)
    base = _pasta_anexos()
    caminho = os.path.realpath(os.path.join(base, os.path.basename(nome)))
    if os.path.commonpath([base, caminho]) != base or not os.path.isfile(caminho):
        abort(404)
    return caminho


def _blocos_do_sindico(usuario):
    permitidos = usuario.get_blocos_permitidos()
    blocos = get_blocos()
    if permitidos is None:
        return blocos
    return [bloco for bloco in blocos if bloco in permitidos]


def _repasse_do_tenant(repasse_id, condominio_id):
    return RepasseBloco.query.filter_by(id=repasse_id, condominio_id=condominio_id).first_or_404()


def _pode_ver_repasse(repasse):
    usuario = get_current_user()
    if usuario is None or usuario.condominio_id != repasse.condominio_id:
        abort(404)
    if usuario.role == Role.ADMIN:
        return usuario
    if usuario.role == Role.SINDICO and repasse.bloco in _blocos_do_sindico(usuario):
        return usuario
    abort(404)


@admin_required
def admin_financeiro_pagamentos():
    condominio_id, condominio = _condominio_atual()
    hoje = _hoje()
    _atualizar_despesas_vencidas(condominio_id, hoje)
    competencia_param = request.args.get("competencia")
    if competencia_param is None:
        competencia = _competencia_de(hoje)
    elif competencia_param == "todas":
        competencia = ""
    else:
        competencia = competencia_param.strip()
    busca = (request.args.get("q") or "").strip()
    consulta = DespesaPagamento.query.filter(DespesaPagamento.condominio_id == condominio_id)
    if competencia:
        consulta = consulta.filter(DespesaPagamento.competencia == competencia)
    if busca:
        termo = f"%{busca}%"
        consulta = consulta.filter(
            or_(
                DespesaPagamento.titulo.ilike(termo),
                DespesaPagamento.fornecedor_nome.ilike(termo),
            )
        )
    despesas = consulta.order_by(
        DespesaPagamento.vencimento.asc(), DespesaPagamento.id.desc()
    ).limit(300).all()
    linhas = [_resumo(despesa) for despesa in despesas]
    pago = round(sum(linha["pago"] for linha in linhas if linha["despesa"].status == StatusDespesa.PAGO), 2)
    pago_qtd = sum(1 for linha in linhas if linha["despesa"].status == StatusDespesa.PAGO)
    a_pagar = round(
        sum(linha["divida"] for linha in linhas if linha["despesa"].status in StatusDespesa.ABERTAS),
        2,
    )
    a_pagar_qtd = sum(1 for linha in linhas if linha["despesa"].status in StatusDespesa.ABERTAS)
    referencia = competencia or _competencia_de(hoje)
    return render_template(
        "admin/financeiro/pagamentos.html",
        condominio_fin=condominio,
        linhas=linhas,
        pago=pago,
        pago_qtd=pago_qtd,
        a_pagar=a_pagar,
        a_pagar_qtd=a_pagar_qtd,
        total=round(pago + a_pagar, 2),
        filtro_competencia=competencia,
        filtro_q=busca,
        competencia_rotulo=(
            _rotulo_competencia_longo(referencia) if competencia else "Todas as competências"
        ),
        competencia_anterior=_deslocar_competencia(referencia, -1),
        competencia_proxima=_deslocar_competencia(referencia, 1),
        abrir_id=request.args.get("abrir", type=int),
        fornecedores=Fornecedor.query.filter_by(condominio_id=condominio_id, ativo=True)
        .order_by(Fornecedor.nome.asc())
        .all(),
        planos=PlanoConta.query.filter_by(condominio_id=condominio_id).order_by(PlanoConta.codigo).all(),
        fundos=FundoFinanceiro.query.filter_by(condominio_id=condominio_id).order_by(FundoFinanceiro.codigo).all(),
        contas=ContaBancaria.query.filter_by(condominio_id=condominio_id, ativa=True).all(),
        blocos=get_blocos(),
        operacoes=_OPERACOES,
    )


@admin_required
def admin_financeiro_fornecedor_salvar():
    condominio_id, _condominio = _condominio_atual()
    nome = (request.form.get("nome") or "").strip()
    if not nome:
        flash("Informe o nome do fornecedor.", "danger")
        return redirect(url_for("admin_financeiro_pagamentos"))
    fornecedor = Fornecedor(
        condominio_id=condominio_id,
        nome=nome[:200],
        documento=(request.form.get("documento") or "").strip()[:20] or None,
        chave_pix=(request.form.get("chave_pix") or "").strip()[:120] or None,
        telefone=(request.form.get("telefone") or "").strip()[:20] or None,
        email=(request.form.get("email") or "").strip()[:120] or None,
        categoria_padrao=(request.form.get("categoria_padrao") or "").strip()[:80] or None,
        ativo=True,
    )
    db.session.add(fornecedor)
    db.session.commit()
    flash("Fornecedor cadastrado.", "success")
    return redirect(url_for("admin_financeiro_pagamentos"))


@admin_required
def admin_financeiro_pagamento_salvar():
    condominio_id, _condominio = _condominio_atual()
    titulo = (request.form.get("titulo") or "").strip()
    vencimento = _parse_data(request.form.get("vencimento"))
    competencia = (request.form.get("competencia") or "").strip()
    operacao = (request.form.get("operacao") or "").strip()
    if not titulo or vencimento is None or len(competencia) != 7 or operacao not in _OPERACOES:
        flash("Informe título, competência, vencimento e operação.", "danger")
        return redirect(url_for("admin_financeiro_pagamentos"))
    try:
        valor = _parse_valor(request.form.get("valor_original") or "0")
        impostos = _parse_valor(request.form.get("valor_impostos") or "0")
        descontos = _parse_valor(request.form.get("valor_descontos") or "0")
        acrescimos = _parse_valor(request.form.get("valor_acrescimos") or "0")
    except (TypeError, ValueError):
        flash("Informe valores válidos.", "danger")
        return redirect(url_for("admin_financeiro_pagamentos"))
    if round(valor - impostos - descontos + acrescimos, 2) <= 0:
        flash("O valor a pagar precisa ser maior que zero.", "danger")
        return redirect(url_for("admin_financeiro_pagamentos"))
    conta = ContaBancaria.query.filter_by(
        id=request.form.get("conta_bancaria_id", type=int),
        condominio_id=condominio_id,
        ativa=True,
    ).first()
    plano = PlanoConta.query.filter_by(
        id=request.form.get("plano_conta_id", type=int),
        condominio_id=condominio_id,
    ).first()
    fundo = FundoFinanceiro.query.filter_by(
        id=request.form.get("fundo_id", type=int),
        condominio_id=condominio_id,
    ).first()
    if conta is None or plano is None or fundo is None:
        flash("Escolha conta, plano de contas e fundo deste condomínio.", "danger")
        return redirect(url_for("admin_financeiro_pagamentos"))
    bloco = (request.form.get("bloco_alocado") or "GERAL").strip()
    if bloco != "GERAL" and bloco not in get_blocos():
        bloco = "GERAL"
    fornecedor = None
    fornecedor_id = request.form.get("fornecedor_id", type=int)
    if fornecedor_id:
        fornecedor = Fornecedor.query.filter_by(
            id=fornecedor_id, condominio_id=condominio_id, ativo=True
        ).first()
    nome_fornecedor = (request.form.get("fornecedor_nome") or "").strip()
    if fornecedor is not None:
        nome_fornecedor = fornecedor.nome
    try:
        anexo = _salvar_anexo(request.files.get("arquivo_anexo"))
    except ValueError as erro:
        flash(str(erro), "danger")
        return redirect(url_for("admin_financeiro_pagamentos"))
    status = StatusDespesa.A_VENCER if vencimento >= _hoje() else StatusDespesa.VENCIDO
    db.session.add(
        DespesaPagamento(
            condominio_id=condominio_id,
            fornecedor_id=fornecedor.id if fornecedor else None,
            fornecedor_nome=nome_fornecedor[:200] or None,
            conta_bancaria_id=conta.id,
            plano_conta_id=plano.id,
            fundo_id=fundo.id,
            titulo=titulo[:200],
            competencia=competencia,
            vencimento=vencimento,
            operacao=operacao,
            bloco_alocado=bloco,
            valor_original=valor,
            valor_impostos=impostos,
            valor_descontos=descontos,
            valor_acrescimos=acrescimos,
            valor_pago=0.0,
            status=status,
            arquivo_anexo=anexo,
            observacoes=(request.form.get("observacoes") or "").strip() or None,
        )
    )
    db.session.commit()
    flash("Despesa lançada.", "success")
    return redirect(url_for("admin_financeiro_pagamentos", competencia=competencia))


@admin_required
def admin_financeiro_pagamento_pagar(despesa_id):
    from app.routes import _registrar_auditoria

    condominio_id, _condominio = _condominio_atual()
    despesa = DespesaPagamento.query.filter_by(
        id=despesa_id, condominio_id=condominio_id
    ).first_or_404()
    if despesa.status not in StatusDespesa.ABERTAS:
        flash("Esta despesa não aceita baixa.", "warning")
        return redirect(url_for("admin_financeiro_pagamentos", abrir=despesa.id))
    data_pagamento = _parse_data(request.form.get("data_pagamento"))
    if data_pagamento is None:
        flash("Informe a data do pagamento.", "danger")
        return redirect(url_for("admin_financeiro_pagamentos", abrir=despesa.id))
    resumo = _resumo(despesa)
    bruto = (request.form.get("valor_pago") or "").strip()
    if bruto:
        try:
            valor = _parse_valor(bruto)
        except (TypeError, ValueError):
            flash("Informe um valor pago válido.", "danger")
            return redirect(url_for("admin_financeiro_pagamentos", abrir=despesa.id))
    else:
        valor = resumo["divida"]
    if valor <= 0:
        flash("Informe um valor pago maior que zero.", "danger")
        return redirect(url_for("admin_financeiro_pagamentos", abrir=despesa.id))
    despesa.valor_pago = round(float(despesa.valor_pago or 0) + valor, 2)
    despesa.data_pagamento = data_pagamento
    despesa.data_extrato = _parse_data(request.form.get("data_extrato"))
    if despesa.valor_pago + 0.009 >= resumo["a_pagar"]:
        despesa.status = StatusDespesa.PAGO
    elif despesa.vencimento < _hoje():
        despesa.status = StatusDespesa.VENCIDO
    if despesa.conta_bancaria is not None:
        despesa.conta_bancaria.saldo_atual = round(
            float(despesa.conta_bancaria.saldo_atual or 0) - valor, 2
        )
    usuario = get_current_user()
    if usuario is not None:
        _registrar_auditoria(usuario, f"Baixa da despesa #{despesa.id}.")
    db.session.commit()
    flash("Baixa registrada.", "success")
    return redirect(url_for("admin_financeiro_pagamentos", abrir=despesa.id, competencia=despesa.competencia))


@admin_required
def admin_financeiro_pagamento_anexo(despesa_id):
    condominio_id, _condominio = _condominio_atual()
    despesa = DespesaPagamento.query.filter_by(
        id=despesa_id, condominio_id=condominio_id
    ).first_or_404()
    try:
        nome = _salvar_anexo(request.files.get("arquivo_anexo"))
    except ValueError as erro:
        flash(str(erro), "danger")
        return redirect(url_for("admin_financeiro_pagamentos", abrir=despesa.id))
    if not nome:
        flash("Escolha o boleto ou a nota fiscal.", "warning")
        return redirect(url_for("admin_financeiro_pagamentos", abrir=despesa.id))
    despesa.arquivo_anexo = nome
    db.session.commit()
    flash("Documento anexado.", "success")
    return redirect(url_for("admin_financeiro_pagamentos", abrir=despesa.id))


@admin_required
def admin_financeiro_pagamento_arquivo(despesa_id):
    condominio_id, _condominio = _condominio_atual()
    despesa = DespesaPagamento.query.filter_by(
        id=despesa_id, condominio_id=condominio_id
    ).first_or_404()
    return send_file(_caminho_anexo(despesa.arquivo_anexo))


@admin_required
def admin_financeiro_repasses():
    condominio_id, condominio = _condominio_atual()
    hoje = _hoje()
    competencia_param = request.args.get("competencia")
    competencia = (competencia_param or "").strip() or _competencia_de(hoje)
    apuracao = None
    historico = []
    if condominio is not None and condominio.fin_repasses_ativos:
        apuracao = apurar_repasses_blocos(condominio_id, competencia, get_blocos())
        historico = (
            RepasseBloco.query.filter_by(
                condominio_id=condominio_id, competencia=competencia
            )
            .order_by(RepasseBloco.data_repasse.desc(), RepasseBloco.id.desc())
            .all()
        )
    return render_template(
        "admin/financeiro/repasses.html",
        condominio_fin=condominio,
        apuracao=apuracao,
        historico=historico,
        filtro_competencia=competencia,
        competencia_rotulo=_rotulo_competencia_longo(competencia),
        competencia_anterior=_deslocar_competencia(competencia, -1),
        competencia_proxima=_deslocar_competencia(competencia, 1),
        conta=_conta_ativa(condominio_id),
    )


@admin_required
def admin_financeiro_repasse_salvar():
    from app.routes import _registrar_auditoria

    condominio_id, condominio = _condominio_atual()
    if condominio is None or not condominio.fin_repasses_ativos:
        flash("Os repasses por bloco estão desligados neste condomínio.", "warning")
        return redirect(url_for("admin_financeiro_repasses"))
    bloco = (request.form.get("bloco") or "").strip()
    competencia = (request.form.get("competencia") or "").strip()
    if bloco not in get_blocos() or len(competencia) != 7:
        flash("Informe o bloco e a competência.", "danger")
        return redirect(url_for("admin_financeiro_repasses"))
    data_repasse = _parse_data(request.form.get("data_repasse"))
    if data_repasse is None:
        flash("Informe a data do repasse.", "danger")
        return redirect(url_for("admin_financeiro_repasses", competencia=competencia))
    try:
        valor = _parse_valor(request.form.get("valor_repassado") or "0")
    except (TypeError, ValueError):
        flash("Informe um valor válido.", "danger")
        return redirect(url_for("admin_financeiro_repasses", competencia=competencia))
    apuracao = apurar_repasses_blocos(condominio_id, competencia, [bloco])
    saldo = apuracao["blocos"][0]["saldo"] if apuracao["blocos"] else 0.0
    if valor <= 0 or valor > round(saldo + 0.009, 2):
        flash("O repasse não pode passar do saldo deste bloco.", "danger")
        return redirect(url_for("admin_financeiro_repasses", competencia=competencia))
    conta = _conta_ativa(condominio_id)
    if conta is None:
        flash("Cadastre uma conta bancária ativa antes de repassar.", "danger")
        return redirect(url_for("admin_financeiro_repasses", competencia=competencia))
    forma = (request.form.get("forma_transferencia") or "PIX").strip()
    if forma not in _OPERACOES:
        forma = "PIX"
    try:
        anexo = _salvar_anexo(request.files.get("comprovante_anexo"))
    except ValueError as erro:
        flash(str(erro), "danger")
        return redirect(url_for("admin_financeiro_repasses", competencia=competencia))
    linha = apuracao["blocos"][0]
    repasse = RepasseBloco(
        condominio_id=condominio_id,
        conta_bancaria_id=conta.id,
        bloco=bloco,
        competencia=competencia,
        valor_arrecadado_epoca=linha["arrecadado"],
        valor_descontos_despesas=linha["despesas"],
        valor_repassado=valor,
        data_repasse=data_repasse,
        forma_transferencia=forma,
        favorecido_descricao=(request.form.get("favorecido_descricao") or "").strip()[:200] or None,
        comprovante_anexo=anexo,
        status=StatusRepasse.REALIZADO,
        observacoes=(request.form.get("observacoes") or "").strip() or None,
    )
    conta.saldo_atual = round(float(conta.saldo_atual or 0) - valor, 2)
    db.session.add(repasse)
    usuario = get_current_user()
    if usuario is not None:
        _registrar_auditoria(usuario, f"Repasse do bloco {bloco} registrado.")
    db.session.commit()
    flash("Repasse registrado.", "success")
    return redirect(url_for("admin_financeiro_repasses", competencia=competencia))


def financeiro_repasse_demonstrativo(repasse_id):
    usuario = get_current_user()
    if usuario is None:
        from app.auth import _redirect_login_tenant

        return _redirect_login_tenant()
    repasse = RepasseBloco.query.filter_by(id=repasse_id).first()
    if repasse is None:
        abort(404)
    _pode_ver_repasse(repasse)
    apuracao = apurar_repasses_blocos(repasse.condominio_id, repasse.competencia, [repasse.bloco])
    linha = apuracao["blocos"][0] if apuracao["blocos"] else None
    despesas = DespesaPagamento.query.filter(
        DespesaPagamento.condominio_id == repasse.condominio_id,
        DespesaPagamento.competencia == repasse.competencia,
        DespesaPagamento.bloco_alocado == repasse.bloco,
        DespesaPagamento.status == StatusDespesa.PAGO,
    ).all()
    return render_template(
        "financeiro/repasse.html",
        repasse=repasse,
        linha=linha,
        despesas=despesas,
        condominio=repasse.condominio,
    )


def financeiro_repasse_comprovante(repasse_id):
    usuario = get_current_user()
    if usuario is None:
        from app.auth import _redirect_login_tenant

        return _redirect_login_tenant()
    repasse = RepasseBloco.query.filter_by(id=repasse_id).first()
    if repasse is None:
        abort(404)
    _pode_ver_repasse(repasse)
    if not repasse.comprovante_anexo:
        abort(404)
    return send_file(_caminho_anexo(repasse.comprovante_anexo))


@sindico_required
def sindico_financeiro():
    usuario = get_current_user()
    condominio_id = usuario.condominio_id
    condominio = usuario.condominio
    hoje = _hoje()
    blocos = _blocos_do_sindico(usuario)
    competencia_param = request.args.get("competencia")
    competencia = (competencia_param or "").strip() or _competencia_de(hoje)
    apuracao = apurar_repasses_blocos(condominio_id, competencia, blocos) if blocos else {
        "total_arrecadado": 0.0,
        "retido_adm": 0.0,
        "destinado_blocos": 0.0,
        "saldo_pendente": 0.0,
        "blocos": [],
    }
    unidades = []
    if blocos:
        unidades = (
            Unidade.query.filter(
                Unidade.condominio_id == condominio_id,
                Unidade.bloco.in_(blocos),
                Unidade.eh_setor_interno.is_(False),
                Unidade.status.in_((StatusUnidade.APROVADA, StatusUnidade.REGISTRADA)),
            )
            .order_by(Unidade.bloco.asc(), Unidade.apartamento.asc())
            .all()
        )
    cobrancas = []
    if unidades:
        cobrancas = CobrancaUnidade.query.filter(
            CobrancaUnidade.condominio_id == condominio_id,
            CobrancaUnidade.competencia == competencia,
            CobrancaUnidade.unidade_id.in_([unidade.id for unidade in unidades]),
            CobrancaUnidade.status != StatusCobranca.CANCELADA,
        ).all()
    por_unidade = {}
    for cobranca in cobrancas:
        por_unidade.setdefault(cobranca.unidade_id, []).append(cobranca)
    linhas_unidades = []
    em_dia = 0
    em_aberto = 0
    for unidade in unidades:
        titulo = por_unidade.get(unidade.id) or []
        if not titulo:
            situacao = "Sem boleto"
        elif any(item.status in StatusCobranca.ABERTAS for item in titulo):
            situacao = "Em aberto"
            em_aberto += 1
        else:
            situacao = "Em dia"
            em_dia += 1
        linhas_unidades.append({"unidade": unidade, "situacao": situacao})
    base = em_dia + em_aberto
    inadimplencia = round((em_aberto / base) * 100, 1) if base else 0.0
    despesas = []
    repasses = []
    if blocos:
        despesas = (
            DespesaPagamento.query.filter(
                DespesaPagamento.condominio_id == condominio_id,
                DespesaPagamento.competencia == competencia,
                DespesaPagamento.bloco_alocado.in_(blocos),
                DespesaPagamento.status != StatusDespesa.CANCELADO,
            )
            .order_by(DespesaPagamento.vencimento.asc())
            .all()
        )
        repasses = (
            RepasseBloco.query.filter(
                RepasseBloco.condominio_id == condominio_id,
                RepasseBloco.bloco.in_(blocos),
                RepasseBloco.status == StatusRepasse.REALIZADO,
            )
            .order_by(RepasseBloco.data_repasse.desc())
            .limit(40)
            .all()
        )
    return render_template(
        "sindico/financeiro.html",
        condominio_fin=condominio,
        apuracao=apuracao,
        linhas_unidades=linhas_unidades,
        inadimplencia=inadimplencia,
        em_dia=em_dia,
        em_aberto=em_aberto,
        despesas=[_resumo(despesa) for despesa in despesas],
        repasses=repasses,
        filtro_competencia=competencia,
        competencia_rotulo=_rotulo_competencia_longo(competencia),
        competencia_anterior=_deslocar_competencia(competencia, -1),
        competencia_proxima=_deslocar_competencia(competencia, 1),
        blocos=blocos,
    )


def register(app):
    app.add_url_rule(
        "/admin/financeiro/pagamentos",
        "admin_financeiro_pagamentos",
        admin_financeiro_pagamentos,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/financeiro/pagamentos/fornecedor",
        "admin_financeiro_fornecedor_salvar",
        admin_financeiro_fornecedor_salvar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/pagamentos/novo",
        "admin_financeiro_pagamento_salvar",
        admin_financeiro_pagamento_salvar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/pagamentos/<int:despesa_id>/pagar",
        "admin_financeiro_pagamento_pagar",
        admin_financeiro_pagamento_pagar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/pagamentos/<int:despesa_id>/anexo",
        "admin_financeiro_pagamento_anexo",
        admin_financeiro_pagamento_anexo,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/pagamentos/<int:despesa_id>/arquivo",
        "admin_financeiro_pagamento_arquivo",
        admin_financeiro_pagamento_arquivo,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/financeiro/repasses",
        "admin_financeiro_repasses",
        admin_financeiro_repasses,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/financeiro/repasses/registrar",
        "admin_financeiro_repasse_salvar",
        admin_financeiro_repasse_salvar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/financeiro/repasse/<int:repasse_id>",
        "financeiro_repasse_demonstrativo",
        financeiro_repasse_demonstrativo,
        methods=["GET"],
    )
    app.add_url_rule(
        "/financeiro/repasse/<int:repasse_id>/comprovante",
        "financeiro_repasse_comprovante",
        financeiro_repasse_comprovante,
        methods=["GET"],
    )
    app.add_url_rule(
        "/sindico/financeiro",
        "sindico_financeiro",
        sindico_financeiro,
        methods=["GET"],
    )
