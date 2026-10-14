"""Cálculo de consumo e tarifa dos medidores. Não grava cobrança."""

import json

from app.financeiro_fracao import _repartir
from app.models import ModoCalculoMedidor


def faixas_do_medidor(medidor):
    bruto = medidor.faixas_json or ""
    if not bruto.strip():
        return []
    try:
        dados = json.loads(bruto)
    except json.JSONDecodeError:
        return []
    if not isinstance(dados, list):
        return []
    faixas = []
    for item in dados:
        if not isinstance(item, dict):
            continue
        try:
            inicio = float(item.get("de") or 0)
            fim = float(item.get("ate") or 0)
            preco = float(item.get("valor_m3") or 0)
            taxa = float(item.get("taxa_fixa") or 0)
        except (TypeError, ValueError):
            continue
        if fim <= inicio or preco < 0 or taxa < 0:
            continue
        faixas.append({"de": inicio, "ate": fim, "valor_m3": preco, "taxa_fixa": taxa})
    faixas.sort(key=lambda faixa: faixa["de"])
    return faixas


def consumo_da_leitura(anterior, atual, reiniciada):
    """Diferença do relógio. Com [R], a leitura atual é o consumo do relógio novo."""
    if atual is None:
        return 0.0, False
    anterior = float(anterior or 0)
    atual = float(atual)
    if reiniciada:
        consumo = round(max(0.0, atual), 3)
        return consumo, atual < 0
    if atual < anterior:
        return 0.0, True
    return round(atual - anterior, 3), False


def anomalia(consumo, consumo_periodo_anterior, leitura_caiu):
    if leitura_caiu or consumo < 0:
        return True
    anterior = float(consumo_periodo_anterior or 0)
    if anterior > 0 and consumo > anterior * 1.5:
        return True
    return False


def valor_metragem(consumo, tarifa, taxa_fixa):
    if consumo <= 0:
        return round(float(taxa_fixa or 0), 2)
    return round(float(consumo) * float(tarifa or 0) + float(taxa_fixa or 0), 2)


def valor_por_faixas(consumo, faixas, taxa_minima):
    """Faixas progressivas. Um vão menor que 0,05 entre faixas é ignorado."""
    consumo = float(consumo or 0)
    if consumo <= 0:
        return round(float(taxa_minima or 0), 2)
    valor = 0.0
    coberto = 0.0
    for faixa in faixas:
        inicio = float(faixa["de"])
        fim = float(faixa["ate"])
        if coberto + 0.05 >= inicio:
            inicio = coberto
        if consumo <= inicio:
            continue
        trecho = min(consumo, fim) - inicio
        if trecho <= 0:
            continue
        valor += trecho * float(faixa["valor_m3"]) + float(faixa["taxa_fixa"])
        coberto = min(consumo, fim)
    if valor <= 0:
        return round(float(taxa_minima or 0), 2)
    return round(max(valor, float(taxa_minima or 0)), 2)


def _ratear(total, pesos):
    centavos = int(round(float(total or 0) * 100))
    if centavos <= 0:
        return [0.0 for _ in pesos]
    partes = _repartir(centavos, pesos)
    return [parte / 100.0 for parte in partes]


def calcular_linhas(medidor, linhas, valor_fatura):
    """Completa consumo, alerta, crédito abatido e valor de cada linha.

    Cada linha precisa de leitura_anterior, leitura_atual, reiniciada, credito
    e consumo_periodo_anterior. Não altera o saldo de crédito do participante.
    """
    faixas = faixas_do_medidor(medidor)
    preparados = []
    for linha in linhas:
        atual = linha.get("leitura_atual")
        consumo, leitura_caiu = consumo_da_leitura(
            linha.get("leitura_anterior"), atual, bool(linha.get("reiniciada"))
        )
        if atual is None:
            consumo = 0.0
            alerta = False
        else:
            alerta = anomalia(consumo, linha.get("consumo_periodo_anterior"), leitura_caiu)
        preparados.append(
            {
                **linha,
                "consumo": consumo,
                "alerta": alerta,
                "valor_bruto": 0.0,
                "credito_abatido": 0.0,
                "valor": 0.0,
            }
        )
    modo = medidor.modo_calculo or ModoCalculoMedidor.METRAGEM
    if modo == ModoCalculoMedidor.RATEIO_FATURA:
        brutos = _ratear(valor_fatura, [item["consumo"] for item in preparados])
    else:
        brutos = []
        for item in preparados:
            if item.get("leitura_atual") is None:
                brutos.append(0.0)
                continue
            if modo == ModoCalculoMedidor.POR_FAIXA:
                brutos.append(valor_por_faixas(item["consumo"], faixas, medidor.taxa_fixa_minima))
            else:
                brutos.append(
                    valor_metragem(item["consumo"], medidor.tarifa_unitaria, medidor.taxa_fixa_minima)
                )
    for item, bruto in zip(preparados, brutos):
        credito = max(0.0, float(item.get("credito") or 0))
        abatido = round(min(credito, bruto), 2)
        item["valor_bruto"] = round(bruto, 2)
        item["credito_abatido"] = abatido
        item["valor"] = round(bruto - abatido, 2)
    return preparados
