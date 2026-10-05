"""Orçado versus realizado por plano de contas, mês e escopo."""

import json

from app.models import (
    CobrancaUnidade,
    DespesaPagamento,
    EscopoRepasse,
    PlanoConta,
    PrevisaoOrcamentaria,
    StatusCobranca,
    StatusDespesa,
    TipoPlanoConta,
)

MESES = tuple(f"{mes:02d}" for mes in range(1, 13))
NOMES_MESES = (
    "",
    "Janeiro",
    "Fevereiro",
    "Março",
    "Abril",
    "Maio",
    "Junho",
    "Julho",
    "Agosto",
    "Setembro",
    "Outubro",
    "Novembro",
    "Dezembro",
)
ESCOPO_CONSOLIDADO = "CONSOLIDADO"
ESCOPO_GERAL = "GERAL"


def competencias(ano, mes):
    if mes == "anual":
        return [f"{item}/{ano}" for item in MESES]
    return [f"{mes}/{ano}"]


def meses_do_recorte(mes):
    if mes == "anual":
        return list(MESES)
    return [mes]


def ler_meses(texto):
    try:
        dados = json.loads(texto or "{}")
    except (TypeError, json.JSONDecodeError):
        dados = {}
    if not isinstance(dados, dict):
        dados = {}
    saida = {}
    for mes in MESES:
        try:
            saida[mes] = round(float(dados.get(mes) or 0), 2)
        except (TypeError, ValueError):
            saida[mes] = 0.0
    return saida


def soma_meses(texto, meses):
    valores = ler_meses(texto)
    return round(sum(valores[mes] for mes in meses), 2)


def _composicao(valor):
    if isinstance(valor, list):
        return [item for item in valor if isinstance(item, dict)]
    if isinstance(valor, str):
        try:
            dados = json.loads(valor)
        except json.JSONDecodeError:
            return []
        if isinstance(dados, list):
            return [item for item in dados if isinstance(item, dict)]
    return []


def _entra_receita(plano, bloco_unidade, escopo):
    if plano is None:
        return False
    if escopo == ESCOPO_CONSOLIDADO:
        return True
    if escopo == ESCOPO_GERAL:
        return plano.escopo_repasse == EscopoRepasse.ADM_GERAL
    return plano.escopo_repasse == EscopoRepasse.BLOCO and str(bloco_unidade or "") == escopo


def _entra_despesa(bloco_alocado, escopo):
    bloco = (bloco_alocado or ESCOPO_GERAL).strip() or ESCOPO_GERAL
    if escopo == ESCOPO_CONSOLIDADO:
        return True
    if escopo == ESCOPO_GERAL:
        return bloco == ESCOPO_GERAL
    return bloco == escopo


def realizado_por_plano(condominio_id, ano, mes, escopo, planos):
    """Soma receita paga e despesa lançada da competência, por plano."""
    chaves = competencias(ano, mes)
    totais = {}
    cobrancas = CobrancaUnidade.query.filter(
        CobrancaUnidade.condominio_id == condominio_id,
        CobrancaUnidade.status == StatusCobranca.PAGA,
        CobrancaUnidade.competencia.in_(chaves),
    ).all()
    for cobranca in cobrancas:
        bloco = cobranca.unidade.bloco if cobranca.unidade else ""
        for item in _composicao(cobranca.composicao_json):
            plano = planos.get(item.get("plano_conta_id"))
            if plano is None or plano.tipo != TipoPlanoConta.RECEITA:
                continue
            if not _entra_receita(plano, bloco, escopo):
                continue
            try:
                valor = float(item.get("valor") or 0)
            except (TypeError, ValueError):
                valor = 0.0
            totais[plano.id] = round(totais.get(plano.id, 0.0) + valor, 2)
    despesas = DespesaPagamento.query.filter(
        DespesaPagamento.condominio_id == condominio_id,
        DespesaPagamento.status != StatusDespesa.CANCELADO,
        DespesaPagamento.competencia.in_(chaves),
    ).all()
    for despesa in despesas:
        plano = planos.get(despesa.plano_conta_id)
        if plano is None or plano.tipo != TipoPlanoConta.DESPESA:
            continue
        if not _entra_despesa(despesa.bloco_alocado, escopo):
            continue
        totais[despesa.plano_conta_id] = round(
            totais.get(despesa.plano_conta_id, 0.0) + float(despesa.valor_original or 0),
            2,
        )
    return totais


def _rotulo_escopo(escopo):
    if escopo == ESCOPO_CONSOLIDADO:
        return "Consolidado"
    if escopo == ESCOPO_GERAL:
        return "Administração Geral"
    return f"Bloco {escopo}"


def _cor_despesa(orcado, realizado):
    if orcado <= 0 and realizado <= 0:
        return "secondary", 0.0
    if orcado <= 0:
        return "danger", 100.0
    percentual = realizado / orcado * 100
    if percentual <= 85:
        return "success", percentual
    if percentual <= 100:
        return "warning", percentual
    return "danger", percentual


def _linha(plano, escopo, orcado, realizado):
    orcado = round(orcado, 2)
    realizado = round(realizado, 2)
    if plano.tipo == TipoPlanoConta.DESPESA:
        cor, percentual = _cor_despesa(orcado, realizado)
    elif orcado <= 0 and realizado <= 0:
        cor, percentual = "secondary", 0.0
    elif orcado <= 0:
        cor, percentual = "primary", 100.0
    else:
        percentual = realizado / orcado * 100
        cor = "success" if percentual >= 100 else "primary"
    return {
        "codigo": plano.codigo,
        "nome": plano.nome,
        "escopo": _rotulo_escopo(escopo),
        "orcado": orcado,
        "realizado": realizado,
        "diferenca": round(realizado - orcado, 2),
        "cor": cor,
        "percentual": percentual,
        "largura": min(percentual, 100.0),
    }


def montar_comparativo(condominio_id, ano, mes, escopo):
    planos = {
        plano.id: plano
        for plano in PlanoConta.query.filter_by(condominio_id=condominio_id).all()
    }
    meses = meses_do_recorte(mes)
    consulta = PrevisaoOrcamentaria.query.filter_by(condominio_id=condominio_id, ano=ano)
    if escopo != ESCOPO_CONSOLIDADO:
        consulta = consulta.filter_by(bloco_escopo=escopo)
    orcado = {}
    for previsao in consulta.all():
        if previsao.plano_conta_id not in planos:
            continue
        orcado[previsao.plano_conta_id] = round(
            orcado.get(previsao.plano_conta_id, 0.0) + soma_meses(previsao.valores_mensais_json, meses),
            2,
        )
    feito = realizado_por_plano(condominio_id, ano, mes, escopo, planos)
    receitas = []
    despesas = []
    for plano_id in sorted(set(orcado) | set(feito), key=lambda item: planos[item].codigo):
        plano = planos[plano_id]
        linha = _linha(plano, escopo, orcado.get(plano_id, 0.0), feito.get(plano_id, 0.0))
        if plano.tipo == TipoPlanoConta.DESPESA:
            despesas.append(linha)
        else:
            receitas.append(linha)
    receita_orcada = round(sum(item["orcado"] for item in receitas), 2)
    receita_realizada = round(sum(item["realizado"] for item in receitas), 2)
    despesa_orcada = round(sum(item["orcado"] for item in despesas), 2)
    despesa_realizada = round(sum(item["realizado"] for item in despesas), 2)
    if receita_orcada > 0:
        receita_percentual = receita_realizada / receita_orcada * 100
    else:
        receita_percentual = 100.0 if receita_realizada > 0 else 0.0
    return {
        "receitas": receitas,
        "despesas": despesas,
        "receita_orcada": receita_orcada,
        "receita_realizada": receita_realizada,
        "receita_percentual": receita_percentual,
        "despesa_orcada": despesa_orcada,
        "despesa_realizada": despesa_realizada,
        "desvio": round(despesa_orcada - despesa_realizada, 2),
    }
