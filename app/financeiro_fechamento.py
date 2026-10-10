"""Fechamento mensal e o retrato usado no balancete."""

import json
from datetime import datetime

from app.models import (
    CobrancaUnidade,
    ContaBancaria,
    DespesaPagamento,
    FechamentoMensal,
    FundoFinanceiro,
    LancamentoCaixaAvulso,
    PlanoConta,
    RepasseBloco,
    StatusCobranca,
    StatusDespesa,
    StatusRepasse,
    TipoLancamentoCaixa,
    TipoPlanoConta,
)


def mensagem_competencia_fechada(competencia):
    return (
        f"🔒 A competência {competencia} está fechada para prestação de contas. "
        "Para alterar lançamentos deste período, reabra o mês em Fechamento do Mês."
    )


def competencia_esta_fechada(condominio_id, competencia):
    if not condominio_id or not competencia:
        return False
    return (
        FechamentoMensal.query.filter_by(
            condominio_id=condominio_id,
            competencia=competencia,
            fechado=True,
        ).first()
        is not None
    )


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


def _numero(valor):
    try:
        return round(float(valor or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _mes_anterior(competencia):
    mes = int(competencia[:2])
    ano = int(competencia[3:])
    mes -= 1
    if mes < 1:
        mes = 12
        ano -= 1
    return f"{mes:02d}/{ano}"


def _no_bloco(bloco, blocos):
    if blocos is None:
        return True
    return str(bloco or "") in blocos


def apurar_competencia(condominio_id, competencia, blocos=None, hoje=None):
    """Totais do mês. blocos=None considera o condomínio inteiro."""
    hoje = hoje or datetime.utcnow().date()
    planos = {
        plano.id: plano
        for plano in PlanoConta.query.filter_by(condominio_id=condominio_id).all()
    }
    fundos = FundoFinanceiro.query.filter_by(condominio_id=condominio_id).order_by(
        FundoFinanceiro.codigo.asc()
    ).all()
    boletos = CobrancaUnidade.query.filter_by(
        condominio_id=condominio_id, competencia=competencia
    ).all()
    despesas = DespesaPagamento.query.filter(
        DespesaPagamento.condominio_id == condominio_id,
        DespesaPagamento.competencia == competencia,
        DespesaPagamento.status != StatusDespesa.CANCELADO,
    ).all()
    repasses = RepasseBloco.query.filter(
        RepasseBloco.condominio_id == condominio_id,
        RepasseBloco.competencia == competencia,
        RepasseBloco.status == StatusRepasse.REALIZADO,
    ).all()
    avulsos = LancamentoCaixaAvulso.query.filter_by(
        condominio_id=condominio_id, competencia=competencia
    ).all()

    por_fundo = {
        fundo.id: {
            "codigo": fundo.codigo,
            "nome": fundo.nome,
            "entradas": 0.0,
            "saidas": 0.0,
            "transferencias_entrada": 0.0,
            "transferencias_saida": 0.0,
        }
        for fundo in fundos
    }
    por_plano = {}
    por_bloco = {}
    boletos_recebidos = 0
    despesas_pagas = 0
    despesas_vencidas = 0
    total_receitas = 0.0
    total_despesas = 0.0
    total_repasses = 0.0

    def linha_plano(plano, valor):
        if plano is None:
            return
        item = por_plano.setdefault(
            plano.id,
            {
                "codigo": plano.codigo,
                "nome": plano.nome,
                "tipo": plano.tipo,
                "valor": 0.0,
            },
        )
        item["valor"] = round(item["valor"] + valor, 2)

    def linha_bloco(bloco):
        chave = str(bloco or "")
        if not chave:
            return None
        return por_bloco.setdefault(
            chave,
            {"bloco": chave, "arrecadado": 0.0, "em_aberto": 0.0, "pago_qtd": 0, "aberto_qtd": 0, "repassado": 0.0},
        )

    from app.utils import get_blocos

    for codigo in (get_blocos() if blocos is None else list(blocos)):
        linha_bloco(codigo)

    for cobranca in boletos:
        bloco = cobranca.unidade.bloco if cobranca.unidade else ""
        if not _no_bloco(bloco, blocos):
            continue
        quadro = linha_bloco(bloco)
        if cobranca.status == StatusCobranca.PAGA:
            valor = _numero(cobranca.valor_pago)
            boletos_recebidos += 1
            total_receitas = round(total_receitas + valor, 2)
            if quadro is not None:
                quadro["arrecadado"] = round(quadro["arrecadado"] + valor, 2)
                quadro["pago_qtd"] += 1
            for item in _composicao(cobranca.composicao_json):
                plano = planos.get(item.get("plano_conta_id"))
                parte = _numero(item.get("valor"))
                linha_plano(plano, parte)
                if plano is not None and plano.fundo_id in por_fundo:
                    por_fundo[plano.fundo_id]["entradas"] = round(
                        por_fundo[plano.fundo_id]["entradas"] + parte, 2
                    )
        elif cobranca.status in StatusCobranca.ABERTAS:
            valor = _numero(cobranca.valor_original)
            if quadro is not None:
                quadro["em_aberto"] = round(quadro["em_aberto"] + valor, 2)
                quadro["aberto_qtd"] += 1

    for despesa in despesas:
        if not _no_bloco(despesa.bloco_alocado, blocos):
            continue
        if despesa.status == StatusDespesa.PAGO:
            valor = _numero(despesa.valor_pago) or _numero(despesa.valor_original)
            despesas_pagas += 1
            total_despesas = round(total_despesas + valor, 2)
            linha_plano(planos.get(despesa.plano_conta_id), valor)
            if despesa.fundo_id in por_fundo:
                por_fundo[despesa.fundo_id]["saidas"] = round(
                    por_fundo[despesa.fundo_id]["saidas"] + valor, 2
                )
        elif despesa.status == StatusDespesa.VENCIDO or (
            despesa.status == StatusDespesa.A_VENCER
            and despesa.vencimento
            and despesa.vencimento < hoje
        ):
            despesas_vencidas += 1

    for repasse in repasses:
        if not _no_bloco(repasse.bloco, blocos):
            continue
        valor = _numero(repasse.valor_repassado)
        total_repasses = round(total_repasses + valor, 2)
        quadro = linha_bloco(repasse.bloco)
        if quadro is not None:
            quadro["repassado"] = round(quadro["repassado"] + valor, 2)

    for avulso in avulsos:
        if not _no_bloco(avulso.bloco_escopo, blocos):
            continue
        valor = _numero(avulso.valor)
        if avulso.tipo == TipoLancamentoCaixa.ENTRADA:
            total_receitas = round(total_receitas + valor, 2)
            linha_plano(planos.get(avulso.plano_conta_id), valor)
            if avulso.fundo_id in por_fundo:
                por_fundo[avulso.fundo_id]["entradas"] = round(
                    por_fundo[avulso.fundo_id]["entradas"] + valor, 2
                )
        elif avulso.tipo == TipoLancamentoCaixa.SAIDA:
            total_despesas = round(total_despesas + valor, 2)
            linha_plano(planos.get(avulso.plano_conta_id), valor)
            if avulso.fundo_id in por_fundo:
                por_fundo[avulso.fundo_id]["saidas"] = round(
                    por_fundo[avulso.fundo_id]["saidas"] + valor, 2
                )
        elif avulso.tipo == TipoLancamentoCaixa.TRANSFERENCIA_FUNDO:
            if avulso.fundo_id in por_fundo:
                por_fundo[avulso.fundo_id]["transferencias_saida"] = round(
                    por_fundo[avulso.fundo_id]["transferencias_saida"] + valor, 2
                )
            if avulso.fundo_destino_id in por_fundo:
                por_fundo[avulso.fundo_destino_id]["transferencias_entrada"] = round(
                    por_fundo[avulso.fundo_destino_id]["transferencias_entrada"] + valor, 2
                )

    fundos_linhas = []
    for fundo in fundos:
        item = por_fundo[fundo.id]
        item["saldo"] = round(
            item["entradas"]
            + item["transferencias_entrada"]
            - item["saidas"]
            - item["transferencias_saida"],
            2,
        )
        fundos_linhas.append(item)

    blocos_linhas = []
    for chave in sorted(por_bloco, key=lambda item: (len(item), item)):
        quadro = por_bloco[chave]
        base = quadro["arrecadado"] + quadro["em_aberto"]
        quadro["inadimplencia"] = round((quadro["em_aberto"] / base) * 100, 1) if base else 0.0
        blocos_linhas.append(quadro)

    planos_linhas = sorted(por_plano.values(), key=lambda item: item["codigo"])
    anterior = FechamentoMensal.query.filter_by(
        condominio_id=condominio_id, competencia=_mes_anterior(competencia)
    ).first()
    saldo_inicial = _numero(anterior.saldo_final_mes) if anterior is not None else 0.0
    resultado = round(total_receitas - total_despesas - total_repasses, 2)
    contas = [
        {
            "nome": conta.nome_banco,
            "codigo": conta.codigo_banco,
            "saldo": _numero(conta.saldo_atual),
        }
        for conta in ContaBancaria.query.filter_by(condominio_id=condominio_id, ativa=True)
        .order_by(ContaBancaria.principal.desc(), ContaBancaria.id.asc())
        .all()
    ]
    return {
        "boletos_recebidos": boletos_recebidos,
        "despesas_pagas": despesas_pagas,
        "despesas_vencidas": despesas_vencidas,
        "repasses_qtd": sum(1 for repasse in repasses if _no_bloco(repasse.bloco, blocos)),
        "total_receitas": total_receitas,
        "total_despesas": total_despesas,
        "total_repasses": total_repasses,
        "resultado": resultado,
        "saldo_inicial": saldo_inicial,
        "saldo_final": round(saldo_inicial + resultado, 2),
        "contas": contas,
        "fundos": fundos_linhas,
        "planos": planos_linhas,
        "blocos": blocos_linhas,
        "receitas_planos": [item for item in planos_linhas if item["tipo"] == TipoPlanoConta.RECEITA],
        "despesas_planos": [item for item in planos_linhas if item["tipo"] == TipoPlanoConta.DESPESA],
    }
