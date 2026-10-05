"""Financeiro do condomínio: caixa, plano, rateio, cobranças, acordos e boleto Itaú.

Não há contas a pagar nesta fase: o KPI correspondente permanece zerado.
"""

import copy
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from flask import Response, abort, flash, redirect, render_template, request, url_for
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from app import db
from app.auth import (
    admin_required,
    condominio_id_obrigatorio,
    get_current_user,
    get_unidade_logada,
    unidade_required,
)
from app.financeiro_boleto import (
    calcular_encargos_atraso,
    dividir_centavos,
    montar_boleto_itau,
    somar_meses_data,
)
from app.financeiro_cnab import (
    FORMA_RETORNO,
    RetornoInvalido,
    aplicar_retorno,
    montar_remessa_itau,
    montar_retorno_teste,
    nome_remessa,
    nome_retorno_teste,
    resumo_json,
)
from app.models import (
    AcordoFinanceiro,
    ArquivoCnabLog,
    CobrancaUnidade,
    Condominio,
    ContaBancaria,
    DespesaPagamento,
    EscopoRepasse,
    FundoFinanceiro,
    IndiceEconomico,
    PlanoConta,
    RateioCondominio,
    Role,
    StatusAcordo,
    StatusBanco,
    StatusCobranca,
    StatusDespesa,
    StatusRateio,
    StatusUnidade,
    TipoPlanoConta,
    Unidade,
    Pessoa,
)
from app.utils import get_blocos

_FUSO = ZoneInfo("America/Sao_Paulo")
_MESES = (
    "",
    "jan",
    "fev",
    "mar",
    "abr",
    "mai",
    "jun",
    "jul",
    "ago",
    "set",
    "out",
    "nov",
    "dez",
)
_MESES_LONGOS = (
    "",
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
)
_INDICES = ("UFIR-RJ", "IGP-M", "IPCA")
_CODIGOS_LINHA_INICIAL = ("1.1.1.11", "1.1.1.9", "1.1.1.10")


def _hoje():
    return datetime.now(_FUSO).date()


def _reais(valor):
    try:
        numero = float(valor or 0)
    except (TypeError, ValueError):
        numero = 0.0
    texto = f"{numero:,.2f}"
    return "R$ " + texto.replace(",", "X").replace(".", ",").replace("X", ".")


def _competencia_extenso(competencia):
    try:
        mes, ano = str(competencia or "").split("/")
        return f"{_MESES[int(mes)]}/{ano}"
    except (ValueError, IndexError):
        return competencia or ""


def _rotulo_criterio(criterio):
    if not criterio or criterio == "GERAL":
        return "Condomínio geral"
    return f"Bloco {criterio}"


def _badge_status(status):
    return {
        StatusCobranca.A_VENCER: "text-bg-primary",
        StatusCobranca.VENCIDA: "text-bg-danger",
        StatusCobranca.PAGA: "text-bg-success",
        StatusCobranca.CANCELADA: "text-bg-secondary",
        StatusCobranca.ACORDO: "text-bg-warning",
        StatusRateio.RASCUNHO: "text-bg-secondary",
        StatusRateio.RATEADO: "text-bg-success",
    }.get(status, "text-bg-secondary")


def _parse_valor(texto):
    bruto = str(texto or "").strip().replace("R$", "").replace(" ", "")
    if not bruto:
        raise ValueError("vazio")
    if "," in bruto and "." in bruto:
        bruto = bruto.replace(".", "").replace(",", ".")
    elif "," in bruto:
        bruto = bruto.replace(",", ".")
    valor = round(float(bruto), 2)
    if valor < 0:
        raise ValueError("negativo")
    return valor


def _parse_data(texto):
    bruto = (texto or "").strip()
    for formato in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(bruto, formato).date()
        except ValueError:
            continue
    return None


def _como_lista(valor):
    if not valor:
        return []
    if isinstance(valor, str):
        import json

        try:
            valor = json.loads(valor)
        except json.JSONDecodeError:
            return []
    if isinstance(valor, list):
        return valor
    return []


def _liquido(cobranca):
    return round(
        float(cobranca.valor_original or 0)
        + float(cobranca.valor_multa or 0)
        + float(cobranca.valor_juros or 0)
        + float(cobranca.valor_correcao or 0)
        + float(cobranca.valor_outros_acrescimos or 0)
        - float(cobranca.valor_desconto or 0),
        2,
    )


def _recebido(cobranca):
    if cobranca.valor_pago is not None:
        return round(float(cobranca.valor_pago), 2)
    return _liquido(cobranca)


def _marcado(nome):
    return "1" in request.form.getlist(nome)


def _condominio_atual():
    condominio_id = condominio_id_obrigatorio()
    condominio = db.session.get(Condominio, condominio_id) if condominio_id else None
    return condominio_id, condominio


def _atualizar_vencidas(condominio_id, hoje):
    alterou = (
        CobrancaUnidade.query.filter(
            CobrancaUnidade.condominio_id == condominio_id,
            CobrancaUnidade.status == StatusCobranca.A_VENCER,
            CobrancaUnidade.vencimento < hoje,
        ).update({CobrancaUnidade.status: StatusCobranca.VENCIDA}, synchronize_session=False)
    )
    if alterou:
        db.session.commit()


def _conta_ativa(condominio_id):
    return (
        ContaBancaria.query.filter_by(condominio_id=condominio_id, ativa=True)
        .order_by(ContaBancaria.principal.desc(), ContaBancaria.id.asc())
        .first()
    )


def _unidades_elegiveis(condominio_id, criterio):
    consulta = Unidade.query.filter(
        Unidade.condominio_id == condominio_id,
        Unidade.eh_setor_interno.is_(False),
        Unidade.status.in_((StatusUnidade.APROVADA, StatusUnidade.REGISTRADA)),
    )
    if criterio and criterio != "GERAL":
        consulta = consulta.filter(Unidade.bloco == criterio)
    return consulta.order_by(Unidade.bloco.asc(), Unidade.apartamento.asc()).all()


def _blocos_com_unidades(condominio_id):
    linhas = (
        db.session.query(Unidade.bloco)
        .filter(
            Unidade.condominio_id == condominio_id,
            Unidade.eh_setor_interno.is_(False),
            Unidade.status.in_((StatusUnidade.APROVADA, StatusUnidade.REGISTRADA)),
        )
        .distinct()
        .all()
    )
    presentes = {bloco for (bloco,) in linhas}
    return [bloco for bloco in get_blocos() if bloco in presentes]


def _pagador_da_unidade(unidade):
    nome_proprietario = (unidade.proprietario_nome or "").strip()
    if nome_proprietario:
        return (
            nome_proprietario[:200],
            (unidade.proprietario_cpf or "")[:20],
            (unidade.proprietario_email or "")[:120],
            (unidade.proprietario_telefone or "")[:20],
        )
    pessoas = unidade.pessoas.order_by(Pessoa.id.asc()).all()
    escolhida = next((pessoa for pessoa in pessoas if pessoa.eh_proprietario), None)
    if escolhida is None:
        escolhida = next((pessoa for pessoa in pessoas if pessoa.is_responsavel), None)
    if escolhida is None and pessoas:
        escolhida = pessoas[0]
    if escolhida is None:
        return ("", "", "", "")
    return (
        (escolhida.nome_completo or "")[:200],
        (escolhida.cpf or "")[:20],
        (escolhida.email or "")[:120],
        (escolhida.telefone or "")[:20],
    )


def _reservar_nossos_numeros(condominio_id, quantidade):
    atual = (
        db.session.query(func.max(CobrancaUnidade.nosso_numero))
        .filter(CobrancaUnidade.condominio_id == condominio_id)
        .scalar()
    )
    base = int(atual) if atual and str(atual).isdigit() else 0
    return [f"{base + indice:010d}" for indice in range(1, quantidade + 1)]


def _titulo_cobranca(rateio, itens):
    if len(itens) == 1:
        item = itens[0]
        total = int(item.get("total_parcelas") or 1)
        atual = int(item.get("parcela_atual") or 1)
        if total > 1:
            descricao = item.get("descricao") or rateio.titulo
            return f"{atual}/{total} - {descricao}"[:200]
    return (rateio.titulo or "Taxa de condomínio")[:200]


def gerar_cobrancas_do_rateio(rateio, hoje=None):
    """Cria um título por unidade elegível. Não faz commit."""
    if rateio.status == StatusRateio.RATEADO:
        return False, "Este rateio já gerou cobranças.", 0
    itens = _como_lista(rateio.itens_json)
    if not itens:
        return False, "O rateio não tem composição.", 0
    valor = round(sum(float(item.get("valor") or 0) for item in itens), 2)
    if valor <= 0:
        return False, "O valor do boleto precisa ser maior que zero.", 0
    unidades = _unidades_elegiveis(rateio.condominio_id, rateio.criterio_bloco)
    if not unidades:
        return False, "Não há unidades ativas neste critério de rateio.", 0
    conta = _conta_ativa(rateio.condominio_id)
    if conta is None:
        return False, "Cadastre uma conta bancária ativa antes de gerar as cobranças.", 0

    hoje = hoje or _hoje()
    numeros = _reservar_nossos_numeros(rateio.condominio_id, len(unidades))
    for unidade, numero in zip(unidades, numeros):
        nome, documento, email, telefone = _pagador_da_unidade(unidade)
        status = (
            StatusCobranca.A_VENCER
            if rateio.vencimento_padrao >= hoje
            else StatusCobranca.VENCIDA
        )
        db.session.add(
            CobrancaUnidade(
                condominio_id=rateio.condominio_id,
                unidade_id=unidade.id,
                conta_bancaria_id=conta.id,
                rateio_id=rateio.id,
                competencia=rateio.competencia,
                titulo=_titulo_cobranca(rateio, itens),
                nosso_numero=numero,
                vencimento=rateio.vencimento_padrao,
                pagador_nome=nome,
                pagador_documento=documento,
                pagador_email=email,
                pagador_telefone=telefone,
                composicao_json=copy.deepcopy(itens),
                valor_original=valor,
                status=status,
                remessa_gerada=False,
            )
        )
    rateio.valor_unitario = valor
    rateio.total_gerado = round(valor * len(unidades), 2)
    rateio.status = StatusRateio.RATEADO
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        return False, "Não foi possível reservar o nosso número. Tente novamente.", 0
    return True, "", len(unidades)


def _linhas_do_form(form, planos_por_id, bloco=None):
    itens = []
    indice = 0
    while indice < 20 and (
        f"linha_{indice}_plano" in form or f"linha_{indice}_valor" in form
    ):
        plano_bruto = (form.get(f"linha_{indice}_plano") or "").strip()
        valor_bruto = form.get(f"linha_{indice}_valor")
        if bloco:
            especifico = form.get(f"linha_{indice}_bloco_{bloco}")
            if especifico is not None and str(especifico).strip() != "":
                valor_bruto = especifico
        descricao = (form.get(f"linha_{indice}_descricao") or "").strip()
        parcela_bruta = form.get(f"linha_{indice}_parcela") or "1"
        total_bruto = form.get(f"linha_{indice}_total") or "1"
        if not str(valor_bruto or "").strip():
            indice += 1
            continue
        if not plano_bruto:
            return None, "Cada linha da composição precisa de um plano de contas."
        try:
            plano_id = int(plano_bruto)
        except ValueError:
            return None, "Plano de contas inválido."
        plano = planos_por_id.get(plano_id)
        if plano is None:
            return None, "Plano de contas inválido para este condomínio."
        try:
            valor = _parse_valor(valor_bruto)
        except (TypeError, ValueError):
            return None, "Informe um valor válido em cada linha da composição."
        try:
            parcela = max(1, min(120, int(str(parcela_bruta).strip() or "1")))
        except ValueError:
            parcela = 1
        try:
            total = max(parcela, min(120, int(str(total_bruto).strip() or "1")))
        except ValueError:
            total = parcela
        itens.append(
            {
                "plano_conta_id": plano.id,
                "descricao": (descricao or plano.nome)[:120],
                "fundo_nome": plano.fundo.nome if plano.fundo else "",
                "valor": valor,
                "parcela_atual": parcela,
                "total_parcelas": total,
            }
        )
        indice += 1
    if not itens:
        return None, "Inclua ao menos uma linha na composição do rateio."
    if round(sum(item["valor"] for item in itens), 2) <= 0:
        return None, "O valor do boleto precisa ser maior que zero."
    return itens, None


def _titulo_rateio(titulo_form, criterio, competencia, acrescentar_bloco=False):
    mes = _competencia_extenso(competencia)
    base = (titulo_form or "").strip()
    if criterio == "GERAL":
        return (base or f"TAXA CONDOMÍNIO - {mes}")[:200]
    if acrescentar_bloco and base:
        return f"{base} - Bloco {criterio}"[:200]
    if base:
        return base[:200]
    return f"TAXA BLOCO {criterio} - {mes}"[:200]


def _montar_rateios(condominio_id, form, planos_por_id):
    competencia = (form.get("competencia") or "").strip()
    if len(competencia) != 7 or competencia[2] != "/":
        return None, "Informe a competência no formato MM/AAAA."
    try:
        mes = int(competencia[:2])
        ano = int(competencia[3:])
    except ValueError:
        return None, "Informe a competência no formato MM/AAAA."
    if mes < 1 or mes > 12 or ano < 2000 or ano > 2100:
        return None, "Competência inválida."
    vencimento = _parse_data(form.get("vencimento"))
    if vencimento is None:
        return None, "Informe o vencimento da taxa."
    escopo = (form.get("escopo") or "").strip()
    if escopo not in ("BLOCO", "TODOS", "GERAL"):
        return None, "Escolha o escopo do rateio."

    if escopo == "TODOS":
        criterios = _blocos_com_unidades(condominio_id)
        if not criterios:
            return None, "Não há unidades ativas para ratear por bloco."
    elif escopo == "BLOCO":
        bloco = (form.get("bloco") or "").strip()
        if bloco not in get_blocos():
            return None, "Escolha o bloco do rateio."
        criterios = [bloco]
    else:
        criterios = ["GERAL"]

    titulo_form = form.get("titulo")
    rateios = []
    for criterio in criterios:
        itens, erro = _linhas_do_form(
            form, planos_por_id, bloco=criterio if escopo == "TODOS" else None
        )
        if erro:
            return None, erro
        rateios.append(
            RateioCondominio(
                condominio_id=condominio_id,
                titulo=_titulo_rateio(
                    titulo_form, criterio, competencia, acrescentar_bloco=escopo == "TODOS"
                ),
                competencia=competencia,
                vencimento_padrao=vencimento,
                criterio_bloco=criterio,
                itens_json=itens,
                valor_unitario=round(sum(item["valor"] for item in itens), 2),
                total_gerado=0.0,
                status=StatusRateio.RASCUNHO,
            )
        )
    for rateio in rateios:
        db.session.add(rateio)
    return rateios, None


def _linhas_iniciais(planos, form=None):
    if form is not None and any(chave.startswith("linha_") for chave in form.keys()):
        linhas = []
        indice = 0
        while indice < 20 and (
            f"linha_{indice}_plano" in form or f"linha_{indice}_valor" in form
        ):
            linhas.append(
                {
                    "plano_id": form.get(f"linha_{indice}_plano") or "",
                    "descricao": form.get(f"linha_{indice}_descricao") or "",
                    "valor": form.get(f"linha_{indice}_valor") or "",
                    "parcela": form.get(f"linha_{indice}_parcela") or "1",
                    "total": form.get(f"linha_{indice}_total") or "1",
                }
            )
            indice += 1
        if linhas:
            return linhas
    por_codigo = {plano.codigo: plano for plano in planos}
    linhas = []
    for codigo in _CODIGOS_LINHA_INICIAL:
        plano = por_codigo.get(codigo)
        if plano is None:
            continue
        linhas.append(
            {
                "plano_id": str(plano.id),
                "descricao": plano.nome,
                "valor": "",
                "parcela": "1",
                "total": "1",
            }
        )
    if not linhas and planos:
        linhas.append(
            {
                "plano_id": str(planos[0].id),
                "descricao": planos[0].nome,
                "valor": "",
                "parcela": "1",
                "total": "1",
            }
        )
    return linhas


def _contexto_rateios(condominio_id, condominio, abrir_form=False, form=None):
    planos = (
        PlanoConta.query.filter_by(condominio_id=condominio_id)
        .order_by(PlanoConta.codigo.asc())
        .all()
    )
    rateios = (
        RateioCondominio.query.filter_by(condominio_id=condominio_id)
        .order_by(RateioCondominio.criado_em.desc(), RateioCondominio.id.desc())
        .all()
    )
    contagens = dict(
        db.session.query(CobrancaUnidade.rateio_id, func.count(CobrancaUnidade.id))
        .filter(
            CobrancaUnidade.condominio_id == condominio_id,
            CobrancaUnidade.rateio_id.isnot(None),
        )
        .group_by(CobrancaUnidade.rateio_id)
        .all()
    )
    return {
        "condominio_fin": condominio,
        "planos": planos,
        "rateios": rateios,
        "contagens": contagens,
        "blocos": get_blocos(),
        "linhas": _linhas_iniciais(planos, form),
        "abrir_form": abrir_form,
        "escopo_atual": (form.get("escopo") if form else None)
        or ("GERAL" if condominio and not condominio.fin_repasses_ativos else "BLOCO"),
        "bloco_atual": (form.get("bloco") if form else None) or (get_blocos()[0] if get_blocos() else ""),
        "competencia_atual": (form.get("competencia") if form else "") or "",
        "vencimento_atual": (form.get("vencimento") if form else "") or "",
        "titulo_atual": (form.get("titulo") if form else "") or "",
    }


def _aplicar_principal(conta):
    if not conta.principal:
        return
    (
        ContaBancaria.query.filter(
            ContaBancaria.condominio_id == conta.condominio_id,
            ContaBancaria.id != conta.id,
        ).update({ContaBancaria.principal: False}, synchronize_session=False)
    )


def _meses_janela(hoje, quantidade=6):
    ano, mes = hoje.year, hoje.month
    janela = []
    for _ in range(quantidade):
        janela.append((ano, mes))
        mes += 1
        if mes == 13:
            mes = 1
            ano += 1
    return janela


@admin_required
def admin_financeiro():
    condominio_id, condominio = _condominio_atual()
    hoje = _hoje()
    _atualizar_vencidas(condominio_id, hoje)
    saldo = (
        db.session.query(func.coalesce(func.sum(ContaBancaria.saldo_atual), 0.0))
        .filter(
            ContaBancaria.condominio_id == condominio_id,
            ContaBancaria.ativa.is_(True),
        )
        .scalar()
    )
    cobrancas = CobrancaUnidade.query.filter_by(condominio_id=condominio_id).all()
    inicio_mes = hoje.replace(day=1)
    if hoje.month == 12:
        fim_mes = date(hoje.year + 1, 1, 1)
    else:
        fim_mes = date(hoje.year, hoje.month + 1, 1)

    a_receber_mes = 0.0
    vencido = 0.0
    base = 0.0
    for cobranca in cobrancas:
        if cobranca.status not in StatusCobranca.CONSIDERADAS:
            continue
        liquido = _liquido(cobranca)
        if cobranca.status == StatusCobranca.PAGA:
            base += _recebido(cobranca)
        else:
            base += liquido
        if cobranca.status == StatusCobranca.VENCIDA:
            vencido += liquido
        if (
            cobranca.status in StatusCobranca.ABERTAS
            and cobranca.vencimento
            and inicio_mes <= cobranca.vencimento < fim_mes
        ):
            a_receber_mes += liquido

    despesas = DespesaPagamento.query.filter(
        DespesaPagamento.condominio_id == condominio_id,
        DespesaPagamento.status != StatusDespesa.CANCELADO,
    ).all()
    limite = hoje + timedelta(days=30)
    a_pagar = 0.0
    a_pagar_qtd = 0
    for despesa in despesas:
        if despesa.status not in StatusDespesa.ABERTAS or despesa.vencimento is None:
            continue
        if despesa.vencimento > limite:
            continue
        restante = round(
            float(despesa.valor_original or 0)
            - float(despesa.valor_impostos or 0)
            - float(despesa.valor_descontos or 0)
            + float(despesa.valor_acrescimos or 0)
            - float(despesa.valor_pago or 0),
            2,
        )
        if restante <= 0:
            continue
        a_pagar = round(a_pagar + restante, 2)
        a_pagar_qtd += 1

    import calendar

    ultimo_dia = calendar.monthrange(hoje.year, hoje.month)[1]
    dias = [date(hoje.year, hoje.month, dia) for dia in range(1, ultimo_dia + 1)]
    entradas = {dia: 0.0 for dia in dias}
    saidas = {dia: 0.0 for dia in dias}
    previsto_entrada = {dia: 0.0 for dia in dias}
    previsto_saida = {dia: 0.0 for dia in dias}
    for cobranca in cobrancas:
        if cobranca.status == StatusCobranca.PAGA and cobranca.data_pagamento in entradas:
            entradas[cobranca.data_pagamento] = round(
                entradas[cobranca.data_pagamento] + _recebido(cobranca), 2
            )
        elif cobranca.status in StatusCobranca.ABERTAS and cobranca.vencimento in entradas:
            liquido = _liquido(cobranca)
            entradas[cobranca.vencimento] = round(entradas[cobranca.vencimento] + liquido, 2)
            previsto_entrada[cobranca.vencimento] = round(
                previsto_entrada[cobranca.vencimento] + liquido, 2
            )
    for despesa in despesas:
        if despesa.status == StatusDespesa.PAGO and despesa.data_pagamento in saidas:
            saidas[despesa.data_pagamento] = round(
                saidas[despesa.data_pagamento] + float(despesa.valor_pago or 0), 2
            )
        elif despesa.status in StatusDespesa.ABERTAS and despesa.vencimento in saidas:
            restante = round(
                float(despesa.valor_original or 0)
                - float(despesa.valor_impostos or 0)
                - float(despesa.valor_descontos or 0)
                + float(despesa.valor_acrescimos or 0)
                - float(despesa.valor_pago or 0),
                2,
            )
            saidas[despesa.vencimento] = round(saidas[despesa.vencimento] + max(restante, 0), 2)
            previsto_saida[despesa.vencimento] = round(
                previsto_saida[despesa.vencimento] + max(restante, 0), 2
            )
    saldo_linha = round(float(saldo or 0), 2)
    fluxo = []
    for dia in dias:
        if dia >= hoje:
            saldo_linha = round(
                saldo_linha + previsto_entrada[dia] - previsto_saida[dia], 2
            )
            saldo_ponto = saldo_linha
        else:
            saldo_ponto = None
        fluxo.append(
            {
                "rotulo": f"{dia.day:02d}",
                "entrada": entradas[dia],
                "saida": saidas[dia],
                "saldo": saldo_ponto,
            }
        )
    por_plano = {}
    for despesa in despesas:
        if despesa.competencia != _competencia_de(hoje):
            continue
        nome = despesa.plano_conta.nome if despesa.plano_conta else "Sem plano"
        valor_mes = float(despesa.valor_pago or 0) if despesa.status == StatusDespesa.PAGO else round(
            float(despesa.valor_original or 0)
            - float(despesa.valor_impostos or 0)
            - float(despesa.valor_descontos or 0)
            + float(despesa.valor_acrescimos or 0),
            2,
        )
        por_plano[nome] = round(por_plano.get(nome, 0.0) + valor_mes, 2)
    maior_plano = max(por_plano.values()) if por_plano else 0.0
    top_despesas = [
        {
            "nome": nome,
            "valor": valor,
            "percentual": round((valor / maior_plano) * 100, 1) if maior_plano else 0,
        }
        for nome, valor in sorted(por_plano.items(), key=lambda item: item[1], reverse=True)[:6]
    ]
    proximos = []
    abertas = [
        despesa
        for despesa in despesas
        if despesa.status in StatusDespesa.ABERTAS and despesa.vencimento is not None
    ]
    abertas.sort(key=lambda despesa: (despesa.vencimento, despesa.id))
    for despesa in abertas[:8]:
        restante = round(
            float(despesa.valor_original or 0)
            - float(despesa.valor_impostos or 0)
            - float(despesa.valor_descontos or 0)
            + float(despesa.valor_acrescimos or 0)
            - float(despesa.valor_pago or 0),
            2,
        )
        proximos.append(
            {
                "dia": f"{despesa.vencimento.day:02d}",
                "mes": _MESES[despesa.vencimento.month].upper(),
                "titulo": despesa.titulo,
                "fornecedor": despesa.fornecedor_nome or "",
                "valor": max(restante, 0),
            }
        )
    inadimplencia = round((vencido / base) * 100, 1) if base else 0.0
    competencia_mes = _competencia_de(hoje)
    do_mes = [
        cobranca
        for cobranca in cobrancas
        if cobranca.competencia == competencia_mes
        and cobranca.status != StatusCobranca.CANCELADA
    ]
    pagos_cnab = [
        cobranca
        for cobranca in do_mes
        if cobranca.status == StatusCobranca.PAGA
        and cobranca.forma_pagamento == FORMA_RETORNO
    ]
    conciliacao = round((len(pagos_cnab) / len(do_mes)) * 100, 1) if do_mes else 0.0
    return render_template(
        "admin/financeiro/dashboard.html",
        condominio_fin=condominio,
        saldo=round(float(saldo or 0), 2),
        inadimplencia=inadimplencia,
        conciliacao=conciliacao,
        a_receber_mes=round(a_receber_mes, 2),
        a_pagar=a_pagar,
        a_pagar_qtd=a_pagar_qtd,
        mes_rotulo=f"{_MESES[hoje.month]}/{hoje.year}",
        fluxo=fluxo,
        top_despesas=top_despesas,
        proximos=proximos,
    )


@admin_required
def admin_financeiro_bancos():
    condominio_id, condominio = _condominio_atual()
    aba = request.args.get("aba") or "contas"
    if aba not in ("contas", "planos", "indices"):
        aba = "contas"
    contas = (
        ContaBancaria.query.filter_by(condominio_id=condominio_id)
        .order_by(ContaBancaria.principal.desc(), ContaBancaria.id.asc())
        .all()
    )
    fundos = (
        FundoFinanceiro.query.filter_by(condominio_id=condominio_id)
        .order_by(FundoFinanceiro.codigo.asc())
        .all()
    )
    planos = (
        PlanoConta.query.filter_by(condominio_id=condominio_id)
        .order_by(PlanoConta.codigo.asc())
        .all()
    )
    indices = (
        IndiceEconomico.query.filter_by(condominio_id=condominio_id)
        .order_by(IndiceEconomico.ano_mes.desc(), IndiceEconomico.sigla.asc())
        .all()
    )
    return render_template(
        "admin/financeiro/bancos.html",
        condominio_fin=condominio,
        aba=aba,
        contas=contas,
        fundos=fundos,
        planos=planos,
        indices=indices,
        indices_siglas=_INDICES,
    )


def _ler_conta(form, conta):
    nome = (form.get("nome_banco") or "").strip()
    codigo = (form.get("codigo_banco") or "").strip()
    agencia = (form.get("agencia") or "").strip()
    conta_numero = (form.get("conta") or "").strip()
    if not nome or not codigo or not agencia or not conta_numero:
        return "Informe banco, código, agência e conta."
    try:
        saldo_inicial = _parse_valor(form.get("saldo_inicial") or "0")
        saldo_atual = _parse_valor(form.get("saldo_atual") or "0")
    except (TypeError, ValueError):
        return "Informe saldos válidos."
    conta.nome_banco = nome[:80]
    conta.codigo_banco = codigo[:10]
    conta.agencia = agencia[:10]
    conta.agencia_dv = (form.get("agencia_dv") or "").strip()[:2] or None
    conta.conta = conta_numero[:20]
    conta.conta_dv = (form.get("conta_dv") or "").strip()[:2] or None
    conta.carteira = (form.get("carteira") or "").strip()[:10] or None
    conta.convenio = (form.get("convenio") or "").strip()[:20] or None
    conta.saldo_inicial = saldo_inicial
    conta.saldo_atual = saldo_atual
    conta.principal = _marcado("principal")
    conta.ativa = _marcado("ativa")
    return None


@admin_required
def admin_financeiro_conta_salvar():
    condominio_id, _condominio = _condominio_atual()
    conta_id = request.form.get("conta_id", type=int)
    if conta_id:
        conta = ContaBancaria.query.filter_by(
            id=conta_id, condominio_id=condominio_id
        ).first_or_404()
    else:
        conta = ContaBancaria(condominio_id=condominio_id)
        db.session.add(conta)
    erro = _ler_conta(request.form, conta)
    if erro:
        db.session.rollback()
        flash(erro, "danger")
        return redirect(url_for("admin_financeiro_bancos", aba="contas"))
    db.session.flush()
    _aplicar_principal(conta)
    db.session.commit()
    flash("Conta bancária salva.", "success")
    return redirect(url_for("admin_financeiro_bancos", aba="contas"))


@admin_required
def admin_financeiro_parametros():
    condominio_id, condominio = _condominio_atual()
    if condominio is None:
        flash("Condomínio não encontrado.", "danger")
        return redirect(url_for("admin_financeiro_bancos"))
    try:
        multa = _parse_valor(request.form.get("fin_multa_percentual") or "0")
        juros = _parse_valor(request.form.get("fin_juros_mensal") or "0")
    except (TypeError, ValueError):
        flash("Informe multa e juros válidos.", "danger")
        return redirect(url_for("admin_financeiro_bancos", aba="contas"))
    indice = (request.form.get("fin_indice_correcao") or "").strip()
    if indice not in _INDICES:
        flash("Escolha o índice de correção.", "danger")
        return redirect(url_for("admin_financeiro_bancos", aba="contas"))
    condominio.fin_repasses_ativos = _marcado("fin_repasses_ativos")
    condominio.fin_multa_percentual = multa
    condominio.fin_juros_mensal = juros
    condominio.fin_indice_correcao = indice
    db.session.commit()
    flash("Parâmetros financeiros salvos.", "success")
    return redirect(url_for("admin_financeiro_bancos", aba="contas"))


@admin_required
def admin_financeiro_fundo_salvar():
    condominio_id, _condominio = _condominio_atual()
    fundo_id = request.form.get("fundo_id", type=int)
    codigo = (request.form.get("codigo") or "").strip()
    nome = (request.form.get("nome") or "").strip()
    if not codigo or not nome:
        flash("Informe o código e o nome do fundo.", "danger")
        return redirect(url_for("admin_financeiro_bancos", aba="planos"))
    codigo = codigo[:10]
    nome = nome[:120]
    fundo = None
    if fundo_id:
        fundo = FundoFinanceiro.query.filter_by(
            id=fundo_id, condominio_id=condominio_id
        ).first_or_404()
    with db.session.no_autoflush:
        conflito = FundoFinanceiro.query.filter_by(
            condominio_id=condominio_id, codigo=codigo
        ).first()
    if conflito is not None and (fundo is None or conflito.id != fundo.id):
        flash("Já existe um fundo com este código.", "warning")
        return redirect(url_for("admin_financeiro_bancos", aba="planos"))
    if fundo is None:
        fundo = FundoFinanceiro(condominio_id=condominio_id, codigo=codigo, nome=nome)
        db.session.add(fundo)
    else:
        fundo.codigo = codigo
        fundo.nome = nome
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Já existe um fundo com este código.", "warning")
        return redirect(url_for("admin_financeiro_bancos", aba="planos"))
    flash("Fundo salvo.", "success")
    return redirect(url_for("admin_financeiro_bancos", aba="planos"))


@admin_required
def admin_financeiro_plano_salvar():
    condominio_id, _condominio = _condominio_atual()
    plano_id = request.form.get("plano_id", type=int)
    codigo = (request.form.get("codigo") or "").strip()
    nome = (request.form.get("nome") or "").strip()
    tipo = (request.form.get("tipo") or "").strip()
    escopo = (request.form.get("escopo_repasse") or "").strip()
    fundo_id = request.form.get("fundo_id", type=int)
    if not codigo or not nome:
        flash("Informe o código e o nome do plano de contas.", "danger")
        return redirect(url_for("admin_financeiro_bancos", aba="planos"))
    if tipo not in TipoPlanoConta.CHOICES or escopo not in EscopoRepasse.CHOICES:
        flash("Tipo ou escopo de repasse inválido.", "danger")
        return redirect(url_for("admin_financeiro_bancos", aba="planos"))
    fundo = FundoFinanceiro.query.filter_by(
        id=fundo_id, condominio_id=condominio_id
    ).first()
    if fundo is None:
        flash("Escolha um fundo deste condomínio.", "danger")
        return redirect(url_for("admin_financeiro_bancos", aba="planos"))
    codigo = codigo[:20]
    nome = nome[:120]
    plano = None
    if plano_id:
        plano = PlanoConta.query.filter_by(
            id=plano_id, condominio_id=condominio_id
        ).first_or_404()
    with db.session.no_autoflush:
        conflito = PlanoConta.query.filter_by(
            condominio_id=condominio_id, codigo=codigo
        ).first()
    if conflito is not None and (plano is None or conflito.id != plano.id):
        flash("Já existe um plano com este código.", "warning")
        return redirect(url_for("admin_financeiro_bancos", aba="planos"))
    if plano is None:
        plano = PlanoConta(
            condominio_id=condominio_id,
            codigo=codigo,
            nome=nome,
            tipo=tipo,
            fundo_id=fundo.id,
            escopo_repasse=escopo,
        )
        db.session.add(plano)
    else:
        plano.codigo = codigo
        plano.nome = nome
        plano.tipo = tipo
        plano.fundo_id = fundo.id
        plano.escopo_repasse = escopo
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Já existe um plano com este código.", "warning")
        return redirect(url_for("admin_financeiro_bancos", aba="planos"))
    flash("Plano de contas salvo.", "success")
    return redirect(url_for("admin_financeiro_bancos", aba="planos"))


@admin_required
def admin_financeiro_indice_salvar():
    condominio_id, _condominio = _condominio_atual()
    sigla = (request.form.get("sigla") or "").strip()
    ano_mes = (request.form.get("ano_mes") or "").strip()
    if sigla not in _INDICES:
        flash("Escolha UFIR-RJ, IGP-M ou IPCA.", "danger")
        return redirect(url_for("admin_financeiro_bancos", aba="indices"))
    if len(ano_mes) != 7 or ano_mes[4] != "-":
        flash("Informe o mês de referência como AAAA-MM.", "danger")
        return redirect(url_for("admin_financeiro_bancos", aba="indices"))
    try:
        ano = int(ano_mes[:4])
        mes = int(ano_mes[5:])
        fator = _parse_valor(request.form.get("fator_mensal") or "0")
    except (TypeError, ValueError):
        flash("Mês ou fator inválido.", "danger")
        return redirect(url_for("admin_financeiro_bancos", aba="indices"))
    if mes < 1 or mes > 12 or ano < 1980 or ano > 2100:
        flash("Mês de referência inválido.", "danger")
        return redirect(url_for("admin_financeiro_bancos", aba="indices"))
    referencia_bruta = (request.form.get("valor_referencia") or "").strip()
    referencia = None
    if referencia_bruta:
        try:
            referencia = _parse_valor(referencia_bruta)
        except (TypeError, ValueError):
            flash("Valor de referência inválido.", "danger")
            return redirect(url_for("admin_financeiro_bancos", aba="indices"))
    indice = IndiceEconomico.query.filter_by(
        condominio_id=condominio_id, sigla=sigla, ano_mes=ano_mes
    ).first()
    if indice is None:
        indice = IndiceEconomico(
            condominio_id=condominio_id, sigla=sigla, ano_mes=ano_mes
        )
        db.session.add(indice)
    indice.fator_mensal = fator
    indice.valor_referencia = referencia
    db.session.commit()
    flash("Índice de correção salvo.", "success")
    return redirect(url_for("admin_financeiro_bancos", aba="indices"))


@admin_required
def admin_financeiro_rateios():
    condominio_id, condominio = _condominio_atual()
    if request.method == "POST":
        planos = PlanoConta.query.filter_by(condominio_id=condominio_id).all()
        planos_por_id = {plano.id: plano for plano in planos}
        rateios, erro = _montar_rateios(condominio_id, request.form, planos_por_id)
        if erro:
            db.session.rollback()
            flash(erro, "danger")
            return render_template(
                "admin/financeiro/rateios.html",
                **_contexto_rateios(
                    condominio_id, condominio, abrir_form=True, form=request.form
                ),
            )
        acao = (request.form.get("acao") or "rascunho").strip()
        if acao == "gerar":
            from app.routes import _registrar_auditoria

            db.session.flush()
            usuario = get_current_user()
            total = 0
            for rateio in rateios:
                ok, mensagem, quantidade = gerar_cobrancas_do_rateio(rateio)
                if not ok:
                    db.session.rollback()
                    flash(mensagem, "warning")
                    return redirect(url_for("admin_financeiro_rateios"))
                if usuario is not None:
                    _registrar_auditoria(
                        usuario,
                        f"Rateio gerado: {rateio.titulo} ({quantidade} cobranças).",
                    )
                total += quantidade
            db.session.commit()
            flash(f"Cobranças geradas: {total}.", "success")
            return redirect(url_for("admin_financeiro_cobrancas"))
        db.session.commit()
        flash("Rateio salvo como rascunho.", "success")
        return redirect(url_for("admin_financeiro_rateios"))
    return render_template(
        "admin/financeiro/rateios.html",
        **_contexto_rateios(condominio_id, condominio),
    )


@admin_required
def admin_financeiro_rateio_gerar(rateio_id):
    from app.routes import _registrar_auditoria

    condominio_id, _condominio = _condominio_atual()
    rateio = RateioCondominio.query.filter_by(
        id=rateio_id, condominio_id=condominio_id
    ).first_or_404()
    ok, mensagem, quantidade = gerar_cobrancas_do_rateio(rateio)
    if not ok:
        db.session.rollback()
        flash(mensagem, "warning")
        return redirect(url_for("admin_financeiro_rateios"))
    usuario = get_current_user()
    if usuario is not None:
        _registrar_auditoria(
            usuario, f"Rateio gerado: {rateio.titulo} ({quantidade} cobranças)."
        )
    db.session.commit()
    flash(f"Cobranças geradas: {quantidade}.", "success")
    return redirect(url_for("admin_financeiro_cobrancas"))


@admin_required
def admin_financeiro_rateio_excluir(rateio_id):
    condominio_id, _condominio = _condominio_atual()
    rateio = RateioCondominio.query.filter_by(
        id=rateio_id, condominio_id=condominio_id
    ).first_or_404()
    if rateio.status != StatusRateio.RASCUNHO:
        flash("Só um rateio em rascunho pode ser excluído.", "warning")
        return redirect(url_for("admin_financeiro_rateios"))
    possui = CobrancaUnidade.query.filter_by(
        rateio_id=rateio.id, condominio_id=condominio_id
    ).first()
    if possui is not None:
        flash("Este rateio já possui cobranças.", "warning")
        return redirect(url_for("admin_financeiro_rateios"))
    db.session.delete(rateio)
    db.session.commit()
    flash("Rascunho excluído.", "success")
    return redirect(url_for("admin_financeiro_rateios"))


def _competencia_de(data_ref):
    return f"{data_ref.month:02d}/{data_ref.year}"


def _deslocar_competencia(competencia, delta):
    try:
        mes = int(competencia[:2])
        ano = int(competencia[3:])
    except (TypeError, ValueError):
        hoje = _hoje()
        mes, ano = hoje.month, hoje.year
    mes += delta
    while mes < 1:
        mes += 12
        ano -= 1
    while mes > 12:
        mes -= 12
        ano += 1
    return f"{mes:02d}/{ano}"


def _rotulo_competencia_longo(competencia):
    try:
        mes = int(str(competencia)[:2])
        ano = str(competencia)[3:]
        return f"{_MESES_LONGOS[mes]} / {ano}"
    except (ValueError, IndexError):
        return "Todas as competências"


def _indices_do_condominio(condominio):
    if condominio is None:
        return {}
    sigla = (condominio.fin_indice_correcao or "").strip()
    if not sigla:
        return {}
    linhas = IndiceEconomico.query.filter_by(
        condominio_id=condominio.id, sigla=sigla
    ).all()
    return {linha.ano_mes: float(linha.fator_mensal or 0) for linha in linhas}


def _composicao_exibicao(cobranca, planos):
    linhas = []
    for item in _como_lista(cobranca.composicao_json):
        plano = planos.get(item.get("plano_conta_id"))
        if plano is not None:
            plano_rotulo = f"{plano.codigo} — {plano.nome}"
        else:
            plano_rotulo = item.get("codigo_plano") or ""
        linhas.append(
            {
                "descricao": item.get("descricao") or "",
                "plano": plano_rotulo,
                "fundo": item.get("fundo_nome") or "",
                "valor": round(float(item.get("valor") or 0), 2),
            }
        )
    return linhas


def _enriquecer_cobrancas(cobrancas, condominio, planos, indices):
    hoje = _hoje()
    linhas = []
    for cobranca in cobrancas:
        encargos = calcular_encargos_atraso(
            cobranca, hoje, condominio=condominio, indices_por_mes=indices
        )
        vencimento_boleto = hoje if encargos["vencida"] else cobranca.vencimento
        boleto = None
        if cobranca.conta_bancaria is not None and vencimento_boleto:
            try:
                boleto = montar_boleto_itau(
                    cobranca.conta_bancaria,
                    cobranca.nosso_numero,
                    vencimento_boleto,
                    encargos["total_atualizado"],
                )
            except ValueError:
                boleto = None
        linhas.append(
            {
                "cobranca": cobranca,
                "encargos": encargos,
                "composicao": _composicao_exibicao(cobranca, planos),
                "boleto": boleto,
                "vencimento_boleto": vencimento_boleto,
                "whatsapp": _link_whatsapp(cobranca, boleto, condominio),
            }
        )
    return linhas


def _cobranca_do_tenant(cobranca_id, condominio_id):
    return CobrancaUnidade.query.filter_by(
        id=cobranca_id, condominio_id=condominio_id
    ).first_or_404()


def _cobranca_autorizada(cobranca_id):
    """Admin do condomínio ou o morador da própria unidade. Anônimo vai ao login."""
    cobranca = CobrancaUnidade.query.filter_by(id=cobranca_id).first()
    if cobranca is None:
        abort(404)
    unidade = get_unidade_logada()
    if (
        unidade is not None
        and unidade.id == cobranca.unidade_id
        and unidade.condominio_id == cobranca.condominio_id
    ):
        return cobranca
    usuario = get_current_user()
    if (
        usuario is not None
        and usuario.role == Role.ADMIN
        and usuario.condominio_id == cobranca.condominio_id
    ):
        return cobranca
    if unidade is None and usuario is None:
        from app.auth import _redirect_login_tenant

        return _redirect_login_tenant()
    abort(404)


def _telefone_whatsapp(telefone):
    digitos = "".join(caractere for caractere in str(telefone or "") if caractere.isdigit())
    if len(digitos) in (10, 11):
        digitos = "55" + digitos
    if len(digitos) < 12:
        return None
    return digitos


def _unidade_por_bloco_apto(condominio_id, bloco, apartamento):
    return Unidade.query.filter_by(
        condominio_id=condominio_id,
        bloco=(bloco or "").strip(),
        apartamento=(apartamento or "").strip(),
        eh_setor_interno=False,
    ).first()


def _anexar_observacao(cobranca, texto):
    atual = (cobranca.observacoes or "").strip()
    cobranca.observacoes = f"{atual}\n{texto}".strip() if atual else texto


def _sincronizar_acordo(acordo):
    if acordo is None or acordo.status != StatusAcordo.ATIVO:
        return
    parcelas = CobrancaUnidade.query.filter_by(
        condominio_id=acordo.condominio_id, acordo_id=acordo.id
    ).all()
    if parcelas and all(parcela.status == StatusCobranca.PAGA for parcela in parcelas):
        acordo.status = StatusAcordo.QUITADO


@admin_required
def admin_financeiro_cobrancas():
    condominio_id, condominio = _condominio_atual()
    hoje = _hoje()
    _atualizar_vencidas(condominio_id, hoje)
    status = (request.args.get("status") or "").strip()
    competencia_param = request.args.get("competencia")
    if competencia_param is None:
        competencia = _competencia_de(hoje)
    elif competencia_param == "todas":
        competencia = ""
    else:
        competencia = competencia_param.strip()
    bloco = (request.args.get("bloco") or "").strip()
    busca = (request.args.get("q") or "").strip()
    consulta = CobrancaUnidade.query.filter(CobrancaUnidade.condominio_id == condominio_id)
    if status in (
        StatusCobranca.A_VENCER,
        StatusCobranca.VENCIDA,
        StatusCobranca.PAGA,
        StatusCobranca.CANCELADA,
        StatusCobranca.ACORDO,
    ):
        consulta = consulta.filter(CobrancaUnidade.status == status)
    if competencia:
        consulta = consulta.filter(CobrancaUnidade.competencia == competencia)
    precisa_unidade = bool(bloco or busca)
    if precisa_unidade:
        consulta = consulta.join(Unidade, CobrancaUnidade.unidade_id == Unidade.id).filter(
            Unidade.condominio_id == condominio_id
        )
    if bloco:
        consulta = consulta.filter(Unidade.bloco == bloco)
    if busca:
        termo = f"%{busca}%"
        consulta = consulta.filter(
            or_(
                CobrancaUnidade.pagador_nome.ilike(termo),
                CobrancaUnidade.nosso_numero.ilike(termo),
                CobrancaUnidade.titulo.ilike(termo),
                Unidade.apartamento.ilike(termo),
                Unidade.bloco == busca,
            )
        )
    cobrancas = consulta.order_by(
        CobrancaUnidade.vencimento.desc(), CobrancaUnidade.id.desc()
    ).all()
    recebido = 0.0
    a_receber = 0.0
    indices = _indices_do_condominio(condominio)
    planos = {
        plano.id: plano
        for plano in PlanoConta.query.filter_by(condominio_id=condominio_id).all()
    }
    for cobranca in cobrancas:
        encargos = calcular_encargos_atraso(
            cobranca, hoje, condominio=condominio, indices_por_mes=indices
        )
        if cobranca.status == StatusCobranca.PAGA:
            recebido += _recebido(cobranca)
        elif cobranca.status in (
            StatusCobranca.A_VENCER,
            StatusCobranca.VENCIDA,
            StatusCobranca.ACORDO,
        ):
            a_receber += encargos["total_atualizado"]
    referencia = competencia or _competencia_de(hoje)
    return render_template(
        "admin/financeiro/cobrancas.html",
        condominio_fin=condominio,
        linhas=_enriquecer_cobrancas(cobrancas[:200], condominio, planos, indices),
        total_lista=len(cobrancas),
        recebido=round(recebido, 2),
        a_receber=round(a_receber, 2),
        total=round(recebido + a_receber, 2),
        filtro_status=status,
        filtro_competencia=competencia,
        filtro_bloco=bloco,
        filtro_q=busca,
        competencia_rotulo=_rotulo_competencia_longo(referencia) if competencia else "Todas as competências",
        competencia_anterior=_deslocar_competencia(referencia, -1),
        competencia_proxima=_deslocar_competencia(referencia, 1),
        blocos=get_blocos(),
        planos=list(planos.values()),
        abrir_id=request.args.get("abrir", type=int),
        statuses=(
            ("", "Todos"),
            (StatusCobranca.A_VENCER, "A Vencer"),
            (StatusCobranca.VENCIDA, "Vencida"),
            (StatusCobranca.PAGA, "Paga"),
            (StatusCobranca.ACORDO, "Acordo"),
            (StatusCobranca.CANCELADA, "Cancelada"),
        ),
    )


@admin_required
def admin_financeiro_cobranca_avulsa():
    condominio_id, _condominio = _condominio_atual()
    unidade = _unidade_por_bloco_apto(
        condominio_id, request.form.get("bloco"), request.form.get("apartamento")
    )
    if unidade is None:
        flash("Unidade não encontrada neste condomínio.", "danger")
        return redirect(url_for("admin_financeiro_cobrancas"))
    vencimento = _parse_data(request.form.get("vencimento"))
    competencia = (request.form.get("competencia") or "").strip()
    titulo = (request.form.get("titulo") or "").strip()
    if vencimento is None or len(competencia) != 7 or not titulo:
        flash("Informe título, competência e vencimento.", "danger")
        return redirect(url_for("admin_financeiro_cobrancas"))
    planos = {
        plano.id: plano
        for plano in PlanoConta.query.filter_by(condominio_id=condominio_id).all()
    }
    itens, erro = _linhas_do_form(request.form, planos)
    if erro:
        flash(erro, "danger")
        return redirect(url_for("admin_financeiro_cobrancas"))
    conta = _conta_ativa(condominio_id)
    if conta is None:
        flash("Cadastre uma conta bancária ativa antes de lançar a cobrança.", "danger")
        return redirect(url_for("admin_financeiro_cobrancas"))
    nome, documento, email, telefone = _pagador_da_unidade(unidade)
    numero = _reservar_nossos_numeros(condominio_id, 1)[0]
    status = (
        StatusCobranca.A_VENCER if vencimento >= _hoje() else StatusCobranca.VENCIDA
    )
    db.session.add(
        CobrancaUnidade(
            condominio_id=condominio_id,
            unidade_id=unidade.id,
            conta_bancaria_id=conta.id,
            competencia=competencia,
            titulo=titulo[:200],
            nosso_numero=numero,
            vencimento=vencimento,
            pagador_nome=nome,
            pagador_documento=documento,
            pagador_email=email,
            pagador_telefone=telefone,
            composicao_json=itens,
            valor_original=round(sum(item["valor"] for item in itens), 2),
            status=status,
            observacoes=(request.form.get("observacoes") or "").strip() or None,
            remessa_gerada=False,
        )
    )
    db.session.commit()
    flash("Cobrança avulsa lançada.", "success")
    return redirect(url_for("admin_financeiro_cobrancas", competencia=competencia))


@admin_required
def admin_financeiro_cobranca_pagar(cobranca_id):
    from app.routes import _registrar_auditoria

    condominio_id, condominio = _condominio_atual()
    cobranca = _cobranca_do_tenant(cobranca_id, condominio_id)
    if cobranca.status in (StatusCobranca.PAGA, StatusCobranca.CANCELADA):
        flash("Esta cobrança não aceita baixa.", "warning")
        return redirect(url_for("admin_financeiro_cobrancas", abrir=cobranca.id))
    data_pagamento = _parse_data(request.form.get("data_pagamento"))
    if data_pagamento is None:
        flash("Informe a data do pagamento.", "danger")
        return redirect(url_for("admin_financeiro_cobrancas", abrir=cobranca.id))
    forma = (request.form.get("forma_pagamento") or "").strip()
    if forma not in ("Boleto - Título", "PIX"):
        flash("Escolha a forma de pagamento.", "danger")
        return redirect(url_for("admin_financeiro_cobrancas", abrir=cobranca.id))
    indices = _indices_do_condominio(condominio)
    encargos = calcular_encargos_atraso(
        cobranca, data_pagamento, condominio=condominio, indices_por_mes=indices
    )
    valor_informado = (request.form.get("valor_pago") or "").strip()
    if valor_informado:
        try:
            valor_pago = _parse_valor(valor_informado)
        except (TypeError, ValueError):
            flash("Informe um valor pago válido.", "danger")
            return redirect(url_for("admin_financeiro_cobrancas", abrir=cobranca.id))
    else:
        valor_pago = encargos["total_atualizado"]
    cobranca.valor_correcao = encargos["correcao"]
    cobranca.valor_multa = encargos["multa"]
    cobranca.valor_juros = encargos["juros"]
    cobranca.valor_pago = valor_pago
    cobranca.data_pagamento = data_pagamento
    cobranca.data_extrato = _parse_data(request.form.get("data_extrato"))
    cobranca.forma_pagamento = forma
    cobranca.status = StatusCobranca.PAGA
    if cobranca.acordo_id:
        acordo = AcordoFinanceiro.query.filter_by(
            id=cobranca.acordo_id, condominio_id=condominio_id
        ).first()
        _sincronizar_acordo(acordo)
    usuario = get_current_user()
    if usuario is not None:
        _registrar_auditoria(usuario, f"Baixa da cobrança #{cobranca.id}.")
    db.session.commit()
    flash("Baixa registrada.", "success")
    return redirect(url_for("admin_financeiro_cobrancas", abrir=cobranca.id))


@admin_required
def admin_financeiro_cobranca_email(cobranca_id):
    from app.email_service import enviar_email_boleto

    condominio_id, condominio = _condominio_atual()
    cobranca = _cobranca_do_tenant(cobranca_id, condominio_id)
    if not (cobranca.pagador_email or "").strip():
        flash("Esta cobrança não tem e-mail do pagador.", "warning")
        return redirect(url_for("admin_financeiro_cobrancas", abrir=cobranca.id))
    pacote = _preparar_documento(cobranca, condominio)
    boleto = pacote["boleto"]
    if boleto is None:
        flash("A linha digitável está disponível para contas Itaú 341.", "warning")
        return redirect(url_for("admin_financeiro_cobrancas", abrir=cobranca.id))
    try:
        enviar_email_boleto(
            cobranca.pagador_email.strip(),
            condominio.nome if condominio else "Condomínio",
            cobranca.vencimento.strftime("%d/%m/%Y"),
            _reais(pacote["encargos"]["total_atualizado"]),
            boleto["linha_digitavel"],
            url_for("financeiro_boleto", cobranca_id=cobranca.id, _external=True),
        )
    except Exception:
        flash("Não foi possível enviar o e-mail agora.", "danger")
        return redirect(url_for("admin_financeiro_cobrancas", abrir=cobranca.id))
    flash("E-mail do boleto enviado.", "success")
    return redirect(url_for("admin_financeiro_cobrancas", abrir=cobranca.id))


def _preparar_documento(cobranca, condominio=None):
    if condominio is None:
        condominio = db.session.get(Condominio, cobranca.condominio_id)
    hoje = _hoje()
    indices = _indices_do_condominio(condominio)
    encargos = calcular_encargos_atraso(
        cobranca, hoje, condominio=condominio, indices_por_mes=indices
    )
    vencimento_boleto = hoje if encargos["vencida"] else cobranca.vencimento
    boleto = None
    if cobranca.conta_bancaria is not None and vencimento_boleto:
        try:
            boleto = montar_boleto_itau(
                cobranca.conta_bancaria,
                cobranca.nosso_numero,
                vencimento_boleto,
                encargos["total_atualizado"],
            )
        except ValueError:
            boleto = None
    planos = {
        plano.id: plano
        for plano in PlanoConta.query.filter_by(condominio_id=cobranca.condominio_id).all()
    }
    return {
        "cobranca": cobranca,
        "condominio": condominio,
        "encargos": encargos,
        "boleto": boleto,
        "composicao": _composicao_exibicao(cobranca, planos),
        "vencimento_boleto": vencimento_boleto,
        "whatsapp": _link_whatsapp(cobranca, boleto, condominio),
    }


def _link_whatsapp(cobranca, boleto, condominio):
    numero = _telefone_whatsapp(cobranca.pagador_telefone)
    if not numero or boleto is None:
        return None
    link = url_for("financeiro_boleto", cobranca_id=cobranca.id, _external=True)
    nome = condominio.nome if condominio else "Condomínio"
    texto = (
        f"Boleto {nome}\n"
        f"Vencimento: {cobranca.vencimento.strftime('%d/%m/%Y')}\n"
        f"Linha digitável: {boleto['linha_digitavel']}\n"
        f"{link}"
    )
    return f"https://wa.me/{numero}?text={quote(texto)}"


def _endereco_condominio(condominio):
    if condominio is None:
        return ""
    partes = [
        condominio.logradouro,
        condominio.numero,
        condominio.complemento,
        condominio.bairro,
        condominio.cidade,
        condominio.uf,
        condominio.cep,
    ]
    return ", ".join(parte.strip() for parte in partes if parte and str(parte).strip())


def financeiro_boleto(cobranca_id):
    cobranca = _cobranca_autorizada(cobranca_id)
    if not isinstance(cobranca, CobrancaUnidade):
        return cobranca
    if cobranca.status == StatusCobranca.CANCELADA:
        abort(404)
    pacote = _preparar_documento(cobranca)
    return render_template(
        "financeiro/boleto.html",
        endereco=_endereco_condominio(pacote["condominio"]),
        **pacote,
    )


def financeiro_recibo(cobranca_id):
    cobranca = _cobranca_autorizada(cobranca_id)
    if not isinstance(cobranca, CobrancaUnidade):
        return cobranca
    if cobranca.status != StatusCobranca.PAGA:
        abort(404)
    pacote = _preparar_documento(cobranca)
    return render_template(
        "financeiro/recibo.html",
        endereco=_endereco_condominio(pacote["condominio"]),
        **pacote,
    )


@unidade_required
def morador_boletos(unidade):
    _atualizar_vencidas(unidade.condominio_id, _hoje())
    condominio = db.session.get(Condominio, unidade.condominio_id)
    cobrancas = (
        CobrancaUnidade.query.filter(
            CobrancaUnidade.condominio_id == unidade.condominio_id,
            CobrancaUnidade.unidade_id == unidade.id,
            CobrancaUnidade.status.in_(
                (
                    StatusCobranca.A_VENCER,
                    StatusCobranca.VENCIDA,
                    StatusCobranca.PAGA,
                )
            ),
        )
        .order_by(CobrancaUnidade.vencimento.desc())
        .all()
    )
    indices = _indices_do_condominio(condominio)
    planos = {
        plano.id: plano
        for plano in PlanoConta.query.filter_by(condominio_id=unidade.condominio_id).all()
    }
    return render_template(
        "morador/boletos.html",
        linhas=_enriquecer_cobrancas(cobrancas, condominio, planos, indices),
    )


@admin_required
def admin_financeiro_acordos():
    condominio_id, condominio = _condominio_atual()
    hoje = _hoje()
    _atualizar_vencidas(condominio_id, hoje)
    bloco = (request.args.get("bloco") or "").strip()
    apartamento = (request.args.get("apartamento") or "").strip()
    unidade = None
    abertas = []
    if bloco and apartamento:
        unidade = _unidade_por_bloco_apto(condominio_id, bloco, apartamento)
        if unidade is None:
            flash("Unidade não encontrada neste condomínio.", "warning")
        else:
            indices = _indices_do_condominio(condominio)
            cobrancas = (
                CobrancaUnidade.query.filter(
                    CobrancaUnidade.condominio_id == condominio_id,
                    CobrancaUnidade.unidade_id == unidade.id,
                    CobrancaUnidade.status.in_(StatusCobranca.ABERTAS),
                )
                .order_by(CobrancaUnidade.vencimento.asc())
                .all()
            )
            for cobranca in cobrancas:
                abertas.append(
                    {
                        "cobranca": cobranca,
                        "encargos": calcular_encargos_atraso(
                            cobranca, hoje, condominio=condominio, indices_por_mes=indices
                        ),
                    }
                )
    acordos = (
        AcordoFinanceiro.query.filter_by(condominio_id=condominio_id)
        .order_by(AcordoFinanceiro.criado_em.desc())
        .limit(100)
        .all()
    )
    return render_template(
        "admin/financeiro/acordos.html",
        condominio_fin=condominio,
        unidade=unidade,
        abertas=abertas,
        acordos=acordos,
        blocos=get_blocos(),
        bloco=bloco,
        apartamento=apartamento,
    )


@admin_required
def admin_financeiro_acordo_criar():
    from app.routes import _registrar_auditoria

    condominio_id, condominio = _condominio_atual()
    unidade = _unidade_por_bloco_apto(
        condominio_id, request.form.get("bloco"), request.form.get("apartamento")
    )
    if unidade is None:
        flash("Unidade não encontrada neste condomínio.", "danger")
        return redirect(url_for("admin_financeiro_acordos"))
    try:
        parcelas = int(request.form.get("qtd_parcelas") or "0")
    except ValueError:
        parcelas = 0
    if parcelas < 1 or parcelas > 36:
        flash("Escolha de 1 a 36 parcelas.", "danger")
        return redirect(
            url_for(
                "admin_financeiro_acordos",
                bloco=unidade.bloco,
                apartamento=unidade.apartamento,
            )
        )
    primeiro = _parse_data(request.form.get("primeiro_vencimento"))
    if primeiro is None:
        flash("Informe o vencimento da primeira parcela.", "danger")
        return redirect(
            url_for(
                "admin_financeiro_acordos",
                bloco=unidade.bloco,
                apartamento=unidade.apartamento,
            )
        )
    try:
        judiciais = _parse_valor(request.form.get("valor_judiciais") or "0")
        desconto = _parse_valor(request.form.get("valor_desconto") or "0")
    except (TypeError, ValueError):
        flash("Informe custas e desconto válidos.", "danger")
        return redirect(
            url_for(
                "admin_financeiro_acordos",
                bloco=unidade.bloco,
                apartamento=unidade.apartamento,
            )
        )
    ids = []
    for bruto in request.form.getlist("cobranca_id"):
        try:
            ids.append(int(bruto))
        except ValueError:
            continue
    cobrancas = (
        CobrancaUnidade.query.filter(
            CobrancaUnidade.id.in_(ids),
            CobrancaUnidade.condominio_id == condominio_id,
            CobrancaUnidade.unidade_id == unidade.id,
            CobrancaUnidade.status.in_(StatusCobranca.ABERTAS),
        ).all()
        if ids
        else []
    )
    if not cobrancas:
        flash("Selecione ao menos uma cobrança em aberto desta unidade.", "warning")
        return redirect(
            url_for(
                "admin_financeiro_acordos",
                bloco=unidade.bloco,
                apartamento=unidade.apartamento,
            )
        )
    plano = PlanoConta.query.filter_by(condominio_id=condominio_id, codigo="1.2.2").first()
    if plano is None:
        flash("Cadastre o plano 1.2.2 - Acordos antes de parcelar.", "danger")
        return redirect(url_for("admin_financeiro_acordos"))
    conta = _conta_ativa(condominio_id)
    if conta is None:
        flash("Cadastre uma conta bancária ativa antes de gerar as parcelas.", "danger")
        return redirect(url_for("admin_financeiro_acordos"))
    indices = _indices_do_condominio(condominio)
    hoje = _hoje()
    principal = correcao = juros = multa = 0.0
    for cobranca in cobrancas:
        encargos = calcular_encargos_atraso(
            cobranca, hoje, condominio=condominio, indices_por_mes=indices
        )
        principal = round(principal + float(cobranca.valor_original or 0), 2)
        correcao = round(correcao + encargos["correcao"], 2)
        juros = round(juros + encargos["juros"], 2)
        multa = round(multa + encargos["multa"], 2)
    bruto = round(principal + correcao + juros + multa + judiciais, 2)
    if desconto > bruto:
        flash("O desconto não pode passar do valor atualizado.", "danger")
        return redirect(
            url_for(
                "admin_financeiro_acordos",
                bloco=unidade.bloco,
                apartamento=unidade.apartamento,
            )
        )
    total = round(bruto - desconto, 2)
    sigla = (condominio.fin_indice_correcao if condominio else "") or "UFIR-RJ"
    indice_rotulo = f"{sigla} (SEFAZ-RJ)" if sigla.startswith("UFIR") else sigla
    acordo = AcordoFinanceiro(
        condominio_id=condominio_id,
        unidade_id=unidade.id,
        cobrancas_originais_ids=[cobranca.id for cobranca in cobrancas],
        indice_correcao=indice_rotulo[:40],
        valor_principal=principal,
        valor_correcao=correcao,
        valor_juros=juros,
        valor_multa=multa,
        valor_judiciais=judiciais,
        valor_desconto=desconto,
        valor_total_acordo=total,
        qtd_parcelas=parcelas,
        primeiro_vencimento=primeiro,
        status=StatusAcordo.ATIVO,
        observacoes=(request.form.get("observacoes") or "").strip() or None,
    )
    db.session.add(acordo)
    db.session.flush()
    ids_texto = ", ".join(f"#{cobranca.id}" for cobranca in cobrancas)
    for cobranca in cobrancas:
        cobranca.status = StatusCobranca.CANCELADA
        _anexar_observacao(cobranca, f"Substituída pelo Acordo #{acordo.id}.")
    partes = {
        "Principal": dividir_centavos(principal, parcelas),
        "Correção Monetária": dividir_centavos(correcao, parcelas),
        "Juros": dividir_centavos(juros, parcelas),
        "Multa": dividir_centavos(multa, parcelas),
        "Judiciais": dividir_centavos(judiciais, parcelas),
        "Desconto": dividir_centavos(desconto, parcelas),
    }
    fundo_nome = plano.fundo.nome if plano.fundo else "1 - CAIXA"
    nome, documento, email, telefone = _pagador_da_unidade(unidade)
    numeros = _reservar_nossos_numeros(condominio_id, parcelas)
    for indice in range(parcelas):
        linhas = []
        soma = 0.0
        for descricao in (
            "Principal",
            "Juros",
            "Judiciais",
            "Correção Monetária",
            "Multa",
        ):
            valor = partes[descricao][indice]
            if valor <= 0:
                continue
            soma = round(soma + valor, 2)
            linhas.append(
                {
                    "plano_conta_id": plano.id,
                    "codigo_plano": plano.codigo,
                    "descricao": descricao,
                    "fundo_nome": fundo_nome,
                    "valor": valor,
                    "parcela_atual": indice + 1,
                    "total_parcelas": parcelas,
                }
            )
        desconto_parcela = partes["Desconto"][indice]
        if desconto_parcela > 0:
            linhas.append(
                {
                    "plano_conta_id": plano.id,
                    "codigo_plano": plano.codigo,
                    "descricao": "Desconto",
                    "fundo_nome": fundo_nome,
                    "valor": round(-desconto_parcela, 2),
                    "parcela_atual": indice + 1,
                    "total_parcelas": parcelas,
                }
            )
            soma = round(soma - desconto_parcela, 2)
        vencimento = somar_meses_data(primeiro, indice)
        db.session.add(
            CobrancaUnidade(
                condominio_id=condominio_id,
                unidade_id=unidade.id,
                conta_bancaria_id=conta.id,
                acordo_id=acordo.id,
                competencia=_competencia_de(vencimento),
                titulo=f"{indice + 1}/{parcelas} - Parcela {indice + 1}",
                nosso_numero=numeros[indice],
                vencimento=vencimento,
                pagador_nome=nome,
                pagador_documento=documento,
                pagador_email=email,
                pagador_telefone=telefone,
                composicao_json=linhas,
                valor_original=soma,
                valor_desconto=0.0,
                status=(
                    StatusCobranca.A_VENCER
                    if vencimento >= hoje
                    else StatusCobranca.VENCIDA
                ),
                observacoes=f"Cobranças canceladas por acordo: {ids_texto}",
                remessa_gerada=False,
            )
        )
    usuario = get_current_user()
    if usuario is not None:
        _registrar_auditoria(
            usuario,
            f"Acordo #{acordo.id} criado com {parcelas} parcelas.",
        )
    db.session.commit()
    flash(f"Acordo #{acordo.id} gerado com {parcelas} parcelas.", "success")
    return redirect(url_for("admin_financeiro_cobrancas", competencia="todas"))


def _conta_itau(condominio_id):
    conta = _conta_ativa(condominio_id)
    if conta is not None and str(conta.codigo_banco or "").strip() == "341":
        return conta
    return (
        ContaBancaria.query.filter_by(
            condominio_id=condominio_id, codigo_banco="341", ativa=True
        )
        .order_by(ContaBancaria.principal.desc(), ContaBancaria.id.asc())
        .first()
    )


def _proximo_sequencial_cnab(condominio_id, conta_id, tipo):
    atual = (
        db.session.query(func.max(ArquivoCnabLog.sequencial))
        .filter_by(condominio_id=condominio_id, conta_bancaria_id=conta_id, tipo=tipo)
        .scalar()
    )
    return int(atual or 0) + 1


def _anexo_cnab(nome, conteudo):
    return Response(
        conteudo.encode("ascii"),
        mimetype="text/plain; charset=ascii",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


def _usuario_nome():
    usuario = get_current_user()
    if usuario is None or not usuario.username:
        return ""
    return usuario.username[:80]


@admin_required
def admin_financeiro_conciliacao():
    import json

    condominio_id, condominio = _condominio_atual()
    hoje = _hoje()
    _atualizar_vencidas(condominio_id, hoje)
    conta = _conta_itau(condominio_id)
    competencia_param = request.args.get("competencia")
    if competencia_param is None:
        competencia = _competencia_de(hoje)
    elif competencia_param == "todas":
        competencia = ""
    else:
        competencia = competencia_param.strip()
    bloco = (request.args.get("bloco") or "").strip()
    aguardando = CobrancaUnidade.query.filter(
        CobrancaUnidade.condominio_id == condominio_id,
        CobrancaUnidade.remessa_gerada.is_(False),
        CobrancaUnidade.status.in_(StatusCobranca.ABERTAS),
    )
    registrados = CobrancaUnidade.query.filter(
        CobrancaUnidade.condominio_id == condominio_id,
        CobrancaUnidade.status.in_(StatusCobranca.ABERTAS),
        CobrancaUnidade.status_banco.in_(
            (StatusBanco.REMESSA_GERADA, StatusBanco.REGISTRADO)
        ),
    ).count()
    competencia_mes = _competencia_de(hoje)
    do_mes = CobrancaUnidade.query.filter(
        CobrancaUnidade.condominio_id == condominio_id,
        CobrancaUnidade.competencia == competencia_mes,
        CobrancaUnidade.status != StatusCobranca.CANCELADA,
    )
    total_mes = do_mes.count()
    conciliados_mes = do_mes.filter(
        CobrancaUnidade.status == StatusCobranca.PAGA,
        CobrancaUnidade.forma_pagamento == FORMA_RETORNO,
    ).count()
    taxa = round((conciliados_mes / total_mes) * 100, 1) if total_mes else 0.0
    consulta = aguardando
    if competencia:
        consulta = consulta.filter(CobrancaUnidade.competencia == competencia)
    if bloco:
        consulta = consulta.join(Unidade, CobrancaUnidade.unidade_id == Unidade.id).filter(
            Unidade.condominio_id == condominio_id,
            Unidade.bloco == bloco,
        )
    cobrancas = consulta.order_by(
        CobrancaUnidade.vencimento.asc(), CobrancaUnidade.id.asc()
    ).limit(300).all()
    historico = (
        ArquivoCnabLog.query.filter_by(condominio_id=condominio_id)
        .order_by(ArquivoCnabLog.criado_em.desc(), ArquivoCnabLog.id.desc())
        .limit(50)
        .all()
    )
    lote = None
    detalhes = None
    lote_id = request.args.get("lote", type=int)
    if lote_id:
        lote = ArquivoCnabLog.query.filter_by(
            id=lote_id, condominio_id=condominio_id
        ).first()
        if lote is not None and lote.tipo == "RETORNO" and lote.conteudo_texto:
            try:
                detalhes = json.loads(lote.conteudo_texto)
            except json.JSONDecodeError:
                detalhes = None
    referencia = competencia or _competencia_de(hoje)
    return render_template(
        "admin/financeiro/conciliacao.html",
        condominio_fin=condominio,
        conta=conta,
        cobrancas=cobrancas,
        aguardando_total=aguardando.count(),
        registrados=registrados,
        conciliados_mes=conciliados_mes,
        taxa=taxa,
        historico=historico,
        lote=lote,
        detalhes=detalhes,
        filtro_competencia=competencia,
        filtro_bloco=bloco,
        competencia_rotulo=(
            _rotulo_competencia_longo(referencia) if competencia else "Todas as competências"
        ),
        competencia_anterior=_deslocar_competencia(referencia, -1),
        competencia_proxima=_deslocar_competencia(referencia, 1),
        blocos=get_blocos(),
    )


@admin_required
def admin_financeiro_conciliacao_remessa():
    from app.routes import _registrar_auditoria

    condominio_id, condominio = _condominio_atual()
    conta = _conta_itau(condominio_id)
    if conta is None:
        flash("Cadastre uma conta Itaú 341 ativa antes de gerar a remessa.", "danger")
        return redirect(url_for("admin_financeiro_conciliacao"))
    ids = []
    for bruto in request.form.getlist("cobranca_id"):
        try:
            ids.append(int(bruto))
        except ValueError:
            continue
    cobrancas = (
        CobrancaUnidade.query.filter(
            CobrancaUnidade.id.in_(ids),
            CobrancaUnidade.condominio_id == condominio_id,
            CobrancaUnidade.remessa_gerada.is_(False),
            CobrancaUnidade.status.in_(StatusCobranca.ABERTAS),
            CobrancaUnidade.conta_bancaria_id == conta.id,
        )
        .order_by(CobrancaUnidade.id.asc())
        .all()
        if ids
        else []
    )
    if not cobrancas:
        flash("Selecione cobranças em aberto que ainda não entraram numa remessa.", "warning")
        return redirect(url_for("admin_financeiro_conciliacao"))
    hoje = _hoje()
    try:
        texto, total = montar_remessa_itau(
            conta,
            condominio,
            cobrancas,
            hoje,
            juros_mensal=float(condominio.fin_juros_mensal or 1.0) if condominio else 1.0,
        )
    except ValueError as erro:
        flash(str(erro), "danger")
        return redirect(url_for("admin_financeiro_conciliacao"))
    nome = nome_remessa(hoje)
    log = ArquivoCnabLog(
        condominio_id=condominio_id,
        conta_bancaria_id=conta.id,
        tipo="REMESSA",
        layout="CNAB400_ITAU",
        nome_arquivo=nome,
        sequencial=_proximo_sequencial_cnab(condominio_id, conta.id, "REMESSA"),
        qtd_titulos=len(cobrancas),
        valor_total=total,
        conteudo_texto=texto,
        usuario_nome=_usuario_nome(),
    )
    db.session.add(log)
    db.session.flush()
    for cobranca in cobrancas:
        cobranca.remessa_gerada = True
        cobranca.status_banco = StatusBanco.REMESSA_GERADA
        cobranca.remessa_lote_id = log.id
    usuario = get_current_user()
    if usuario is not None:
        _registrar_auditoria(usuario, f"Remessa CNAB #{log.id} com {len(cobrancas)} títulos.")
    db.session.commit()
    return _anexo_cnab(nome, texto)


@admin_required
def admin_financeiro_conciliacao_arquivo(log_id):
    condominio_id, _condominio = _condominio_atual()
    log = ArquivoCnabLog.query.filter_by(id=log_id, condominio_id=condominio_id).first_or_404()
    if log.tipo != "REMESSA" or not log.conteudo_texto:
        return redirect(url_for("admin_financeiro_conciliacao", lote=log.id))
    return _anexo_cnab(log.nome_arquivo, log.conteudo_texto)


@admin_required
def admin_financeiro_conciliacao_retorno():
    from app.routes import _registrar_auditoria

    condominio_id, _condominio = _condominio_atual()
    conta = _conta_itau(condominio_id)
    if conta is None:
        flash("Cadastre uma conta Itaú 341 ativa antes de importar o retorno.", "danger")
        return redirect(url_for("admin_financeiro_conciliacao"))
    arquivo = request.files.get("arquivo")
    if arquivo is None or not arquivo.filename:
        flash("Envie um arquivo .RET, .CRT ou CSV.", "warning")
        return redirect(url_for("admin_financeiro_conciliacao"))
    bruto = arquivo.read()
    if not bruto:
        flash("O arquivo está vazio.", "warning")
        return redirect(url_for("admin_financeiro_conciliacao"))
    try:
        texto = bruto.decode("latin-1")
    except UnicodeError:
        texto = bruto.decode("utf-8", errors="replace")
    try:
        relatorio = aplicar_retorno(condominio_id, texto)
    except RetornoInvalido as erro:
        db.session.rollback()
        flash(str(erro), "danger")
        return redirect(url_for("admin_financeiro_conciliacao"))
    nome = (arquivo.filename or "retorno.ret").rsplit("\\", 1)[-1].rsplit("/", 1)[-1][:80]
    log = ArquivoCnabLog(
        condominio_id=condominio_id,
        conta_bancaria_id=conta.id,
        tipo="RETORNO",
        layout=relatorio["layout"],
        nome_arquivo=nome,
        sequencial=_proximo_sequencial_cnab(condominio_id, conta.id, "RETORNO"),
        qtd_titulos=relatorio["qtd_titulos"],
        valor_total=relatorio["valor_creditado"],
        qtd_liquidados=len(relatorio["liquidados"]),
        qtd_confirmados=len(relatorio["confirmados"]),
        qtd_nao_encontrados=len(relatorio["nao_encontrados"]),
        conteudo_texto=resumo_json(relatorio),
        usuario_nome=_usuario_nome(),
    )
    db.session.add(log)
    db.session.flush()
    usuario = get_current_user()
    if usuario is not None:
        _registrar_auditoria(
            usuario,
            f"Retorno CNAB #{log.id}: {len(relatorio['liquidados'])} liquidados.",
        )
    db.session.commit()
    flash(
        f"Retorno processado: {len(relatorio['liquidados'])} liquidados, "
        f"{len(relatorio['confirmados'])} confirmados.",
        "success",
    )
    return redirect(url_for("admin_financeiro_conciliacao", lote=log.id))


@admin_required
def admin_financeiro_conciliacao_retorno_teste():
    condominio_id, _condominio = _condominio_atual()
    hoje = _hoje()
    cobrancas = (
        CobrancaUnidade.query.filter(
            CobrancaUnidade.condominio_id == condominio_id,
            CobrancaUnidade.remessa_gerada.is_(True),
            CobrancaUnidade.status.in_(StatusCobranca.ABERTAS),
        )
        .order_by(CobrancaUnidade.id.asc())
        .limit(80)
        .all()
    )
    if not cobrancas:
        cobrancas = (
            CobrancaUnidade.query.filter(
                CobrancaUnidade.condominio_id == condominio_id,
                CobrancaUnidade.status.in_(StatusCobranca.ABERTAS),
            )
            .order_by(CobrancaUnidade.id.asc())
            .limit(80)
            .all()
        )
    if not cobrancas:
        flash("Não há cobrança em aberto para simular o retorno.", "warning")
        return redirect(url_for("admin_financeiro_conciliacao"))
    texto = montar_retorno_teste(cobrancas, hoje)
    return _anexo_cnab(nome_retorno_teste(hoje), texto)


def register(app):
    from app.blueprints.financeiro_pagamentos import register as register_pagamentos

    register_pagamentos(app)

    app.add_template_filter(_reais, "reais")
    app.add_template_filter(_competencia_extenso, "competencia_extenso")
    app.add_template_filter(_rotulo_criterio, "rotulo_criterio")
    app.add_template_filter(_badge_status, "badge_status")
    app.add_template_filter(_como_lista, "como_lista")

    app.add_url_rule("/admin/financeiro", "admin_financeiro", admin_financeiro, methods=["GET"])
    app.add_url_rule(
        "/admin/financeiro/bancos",
        "admin_financeiro_bancos",
        admin_financeiro_bancos,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/financeiro/bancos/conta",
        "admin_financeiro_conta_salvar",
        admin_financeiro_conta_salvar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/bancos/parametros",
        "admin_financeiro_parametros",
        admin_financeiro_parametros,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/bancos/fundo",
        "admin_financeiro_fundo_salvar",
        admin_financeiro_fundo_salvar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/bancos/plano",
        "admin_financeiro_plano_salvar",
        admin_financeiro_plano_salvar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/bancos/indice",
        "admin_financeiro_indice_salvar",
        admin_financeiro_indice_salvar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/rateios",
        "admin_financeiro_rateios",
        admin_financeiro_rateios,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/rateios/<int:rateio_id>/gerar",
        "admin_financeiro_rateio_gerar",
        admin_financeiro_rateio_gerar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/rateios/<int:rateio_id>/excluir",
        "admin_financeiro_rateio_excluir",
        admin_financeiro_rateio_excluir,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/cobrancas",
        "admin_financeiro_cobrancas",
        admin_financeiro_cobrancas,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/financeiro/cobrancas/avulsa",
        "admin_financeiro_cobranca_avulsa",
        admin_financeiro_cobranca_avulsa,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/cobrancas/<int:cobranca_id>/pagar",
        "admin_financeiro_cobranca_pagar",
        admin_financeiro_cobranca_pagar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/cobrancas/<int:cobranca_id>/email",
        "admin_financeiro_cobranca_email",
        admin_financeiro_cobranca_email,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/acordos",
        "admin_financeiro_acordos",
        admin_financeiro_acordos,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/financeiro/acordos/criar",
        "admin_financeiro_acordo_criar",
        admin_financeiro_acordo_criar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/financeiro/boleto/<int:cobranca_id>",
        "financeiro_boleto",
        financeiro_boleto,
        methods=["GET"],
    )
    app.add_url_rule(
        "/financeiro/recibo/<int:cobranca_id>",
        "financeiro_recibo",
        financeiro_recibo,
        methods=["GET"],
    )
    app.add_url_rule(
        "/boletos",
        "morador_boletos",
        morador_boletos,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/financeiro/conciliacao",
        "admin_financeiro_conciliacao",
        admin_financeiro_conciliacao,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/financeiro/conciliacao/remessa",
        "admin_financeiro_conciliacao_remessa",
        admin_financeiro_conciliacao_remessa,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/conciliacao/arquivo/<int:log_id>",
        "admin_financeiro_conciliacao_arquivo",
        admin_financeiro_conciliacao_arquivo,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/financeiro/conciliacao/retorno",
        "admin_financeiro_conciliacao_retorno",
        admin_financeiro_conciliacao_retorno,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/conciliacao/retorno-teste",
        "admin_financeiro_conciliacao_retorno_teste",
        admin_financeiro_conciliacao_retorno_teste,
        methods=["GET"],
    )
