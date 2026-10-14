"""Apura o que a administração retém e o que cada bloco tem a receber.

Só entram boletos pagos da competência. Plano ADM_GERAL fica na administração.
Plano BLOCO segue o bloco da unidade. Despesa e repasse já feitos abatem o saldo.
"""

from app.models import (
    CobrancaUnidade,
    DespesaPagamento,
    EscopoRepasse,
    PlanoConta,
    RepasseBloco,
    StatusCobranca,
    StatusDespesa,
    StatusRepasse,
    StatusUnidade,
    Unidade,
)


def _valor_item(item):
    try:
        return round(float(item.get("valor") or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def apurar_repasses_blocos(condominio_id, competencia, blocos):
    """Devolve o resumo da competência e uma linha por bloco informado."""
    planos = {
        plano.id: plano
        for plano in PlanoConta.query.filter_by(condominio_id=condominio_id).all()
    }
    cobrancas = (
        CobrancaUnidade.query.join(Unidade, CobrancaUnidade.unidade_id == Unidade.id)
        .filter(
            CobrancaUnidade.condominio_id == condominio_id,
            CobrancaUnidade.competencia == competencia,
            CobrancaUnidade.status == StatusCobranca.PAGA,
            Unidade.condominio_id == condominio_id,
            Unidade.eh_setor_interno.is_(False),
            Unidade.status.in_((StatusUnidade.APROVADA, StatusUnidade.REGISTRADA)),
        )
        .all()
    )
    despesas = DespesaPagamento.query.filter(
        DespesaPagamento.condominio_id == condominio_id,
        DespesaPagamento.competencia == competencia,
        DespesaPagamento.status == StatusDespesa.PAGO,
    ).all()
    repasses = RepasseBloco.query.filter(
        RepasseBloco.condominio_id == condominio_id,
        RepasseBloco.competencia == competencia,
        RepasseBloco.status == StatusRepasse.REALIZADO,
    ).all()

    retido = 0.0
    por_bloco = {
        bloco: {
            "bloco": bloco,
            "boletos_pagos": 0,
            "arrecadado": 0.0,
            "despesas": 0.0,
            "repassado": 0.0,
            "saldo": 0.0,
            "unidades": [],
        }
        for bloco in blocos
    }
    total_arrecadado = 0.0
    for cobranca in cobrancas:
        bloco = cobranca.unidade.bloco if cobranca.unidade else ""
        if bloco not in por_bloco:
            continue
        pago = round(float(cobranca.valor_pago or 0), 2)
        total_arrecadado = round(total_arrecadado + pago, 2)
        composicao = cobranca.composicao_json or []
        if isinstance(composicao, str):
            composicao = []
        bruto = round(sum(_valor_item(item) for item in composicao), 2)
        fator = (pago / bruto) if bruto else 0.0
        parte_bloco = 0.0
        for item in composicao:
            plano = planos.get(item.get("plano_conta_id"))
            escopo = plano.escopo_repasse if plano is not None else ""
            valor = round(_valor_item(item) * fator, 2)
            if escopo == EscopoRepasse.ADM_GERAL:
                retido = round(retido + valor, 2)
            elif escopo == EscopoRepasse.BLOCO:
                parte_bloco = round(parte_bloco + valor, 2)
        linha = por_bloco[bloco]
        linha["boletos_pagos"] += 1
        linha["arrecadado"] = round(linha["arrecadado"] + parte_bloco, 2)
        linha["unidades"].append(
            {
                "unidade_id": cobranca.unidade_id,
                "bloco": bloco,
                "apartamento": cobranca.unidade.apartamento if cobranca.unidade else "",
                "valor_bloco": parte_bloco,
                "cobranca_id": cobranca.id,
            }
        )

    for despesa in despesas:
        bloco = (despesa.bloco_alocado or "").strip()
        if bloco not in por_bloco:
            continue
        por_bloco[bloco]["despesas"] = round(
            por_bloco[bloco]["despesas"] + float(despesa.valor_pago or 0), 2
        )
    for repasse in repasses:
        if repasse.bloco not in por_bloco:
            continue
        por_bloco[repasse.bloco]["repassado"] = round(
            por_bloco[repasse.bloco]["repassado"] + float(repasse.valor_repassado or 0), 2
        )

    destinado = 0.0
    pendente = 0.0
    linhas = []
    for bloco in blocos:
        linha = por_bloco[bloco]
        linha["saldo"] = round(
            linha["arrecadado"] - linha["despesas"] - linha["repassado"], 2
        )
        destinado = round(destinado + linha["arrecadado"], 2)
        if linha["saldo"] > 0:
            pendente = round(pendente + linha["saldo"], 2)
        linhas.append(linha)
    return {
        "total_arrecadado": total_arrecadado,
        "retido_adm": retido,
        "destinado_blocos": destinado,
        "saldo_pendente": pendente,
        "blocos": linhas,
    }
