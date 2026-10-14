"""Encargos de atraso e boleto FEBRABAN do Itaú (341), carteira 109.

A ficha de compensação usa o campo livre do Itaú: carteira, nosso número de
8 dígitos, DAC do nosso número, agência, conta, DAC da conta e três zeros.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

_FUSO = ZoneInfo("America/Sao_Paulo")
_BASE_FATOR_ANTIGA = date(1997, 10, 7)
_BASE_FATOR_NOVA = date(2025, 2, 22)

_ITF = {
    "0": "nnwwn",
    "1": "wnnnw",
    "2": "nwnnw",
    "3": "wwnnn",
    "4": "nnwnw",
    "5": "wnwnn",
    "6": "nwwnn",
    "7": "nnnww",
    "8": "wnnwn",
    "9": "nwnwn",
}


def hoje_sp():
    return datetime.now(_FUSO).date()


def modulo_10(numero):
    """DAC FEBRABAN. Pesos 2 e 1 da direita para a esquerda, somando os dígitos."""
    fator = 2
    soma = 0
    for digito in reversed(str(numero)):
        produto = int(digito) * fator
        soma += (produto // 10) + (produto % 10)
        fator = 1 if fator == 2 else 2
    resto = soma % 10
    if resto == 0:
        return 0
    return 10 - resto


def modulo_11_resto(numero):
    """Resto do módulo 11 com pesos de 2 a 9, da direita para a esquerda."""
    fator = 2
    soma = 0
    for digito in reversed(str(numero)):
        soma += int(digito) * fator
        fator = 2 if fator == 9 else fator + 1
    return soma % 11


def dac_codigo_barras(numero_43):
    """DV geral do código de barras. Resto 0, 1 ou 10 vira 1."""
    resto = modulo_11_resto(numero_43)
    if resto in (0, 1, 10):
        return 1
    return 11 - resto


def fator_vencimento(data_vencimento):
    """Fator FEBRABAN. A partir de 22/02/2025 a contagem recomeça em 1000."""
    if data_vencimento >= _BASE_FATOR_NOVA:
        return (data_vencimento - _BASE_FATOR_NOVA).days + 1000
    return (data_vencimento - _BASE_FATOR_ANTIGA).days


def _somente_digitos(valor, tamanho):
    digitos = "".join(caractere for caractere in str(valor or "") if caractere.isdigit())
    if len(digitos) > tamanho:
        digitos = digitos[-tamanho:]
    return digitos.zfill(tamanho)


def _centavos(valor):
    centavos = int(round(float(valor or 0) * 100))
    if centavos < 0:
        centavos = 0
    if centavos > 9999999999:
        raise ValueError("Valor acima do limite do código de barras.")
    return f"{centavos:010d}"


def _somar_meses(data_inicio, data_fim):
    """Meses estritamente posteriores ao vencimento, até o mês da data de referência."""
    ano, mes = data_inicio.year, data_inicio.month
    mes += 1
    if mes == 13:
        mes = 1
        ano += 1
    limite = (data_fim.year, data_fim.month)
    while (ano, mes) <= limite:
        yield f"{ano:04d}-{mes:02d}"
        mes += 1
        if mes == 13:
            mes = 1
            ano += 1


def calcular_encargos_atraso(cobranca, data_ref=None, condominio=None, indices_por_mes=None):
    """Multa, juros pró-rata e correção. No mês do vencimento a correção fica zerada.

    Cobrança paga ou cancelada devolve os encargos já gravados, sem recalcular.
    """
    from app.models import Condominio, IndiceEconomico, StatusCobranca

    data_ref = data_ref or hoje_sp()
    original = round(float(cobranca.valor_original or 0), 2)
    outros = round(float(cobranca.valor_outros_acrescimos or 0), 2)
    desconto = round(float(cobranca.valor_desconto or 0), 2)
    if cobranca.status in (StatusCobranca.PAGA, StatusCobranca.CANCELADA):
        correcao = round(float(cobranca.valor_correcao or 0), 2)
        multa = round(float(cobranca.valor_multa or 0), 2)
        juros = round(float(cobranca.valor_juros or 0), 2)
        total = round(original + correcao + multa + juros + outros - desconto, 2)
        if cobranca.status == StatusCobranca.PAGA and cobranca.valor_pago is not None:
            total = round(float(cobranca.valor_pago), 2)
        return {
            "correcao": correcao,
            "multa": multa,
            "juros": juros,
            "dias_atraso": 0,
            "total_atualizado": total,
            "vencida": False,
        }

    vencida = bool(cobranca.vencimento and cobranca.vencimento < data_ref)
    if not vencida:
        return {
            "correcao": 0.0,
            "multa": 0.0,
            "juros": 0.0,
            "dias_atraso": 0,
            "total_atualizado": round(original + outros - desconto, 2),
            "vencida": False,
        }

    if condominio is None:
        condominio = Condominio.query.get(cobranca.condominio_id)
    multa_percentual = float(getattr(condominio, "fin_multa_percentual", 2.0) or 0.0)
    juros_mensal = float(getattr(condominio, "fin_juros_mensal", 1.0) or 0.0)
    sigla = (getattr(condominio, "fin_indice_correcao", "") or "").strip()

    mesmo_mes = (
        cobranca.vencimento.year == data_ref.year
        and cobranca.vencimento.month == data_ref.month
    )
    correcao = 0.0
    if not mesmo_mes and original:
        from app.financeiro_correcao import correcao_por_sigla

        serie = correcao_por_sigla(
            sigla,
            original,
            cobranca.vencimento,
            data_ref,
            condominio_id=getattr(condominio, "id", None),
        )
        if serie is not None and not serie.get("faltantes"):
            correcao = serie["correcao"]
        else:
            if indices_por_mes is None and condominio is not None and sigla:
                linhas = IndiceEconomico.query.filter_by(
                    condominio_id=condominio.id, sigla=sigla
                ).all()
                indices_por_mes = {
                    linha.ano_mes: float(linha.fator_mensal or 0) for linha in linhas
                }
            fator = 1.0
            for ano_mes in _somar_meses(cobranca.vencimento, data_ref):
                percentual = float((indices_por_mes or {}).get(ano_mes) or 0.0)
                fator *= 1.0 + (percentual / 100.0)
            correcao = round(original * (fator - 1.0), 2)

    dias = (data_ref - cobranca.vencimento).days
    base = original + correcao
    multa = round(base * (multa_percentual / 100.0), 2)
    juros = round(base * ((juros_mensal / 100.0) / 30.0) * dias, 2)
    total = round(original + correcao + multa + juros + outros - desconto, 2)
    return {
        "correcao": correcao,
        "multa": multa,
        "juros": juros,
        "dias_atraso": dias,
        "total_atualizado": total,
        "vencida": True,
    }


def montar_boleto_itau(conta, nosso_numero, vencimento, valor):
    """Linha digitável de 47 dígitos e código de barras de 44. Só Itaú 341."""
    if conta is None or str(conta.codigo_banco or "").strip() != "341":
        return None
    carteira = _somente_digitos(conta.carteira or "109", 3)
    agencia = _somente_digitos(conta.agencia, 4)
    numero_conta = _somente_digitos(conta.conta, 5)
    nosso = _somente_digitos(nosso_numero, 8)
    dac_nosso = modulo_10(f"{agencia}{numero_conta}{carteira}{nosso}")
    dac_conta = modulo_10(f"{agencia}{numero_conta}")
    campo_livre = f"{carteira}{nosso}{dac_nosso}{agencia}{numero_conta}{dac_conta}000"
    if len(campo_livre) != 25:
        raise ValueError("Campo livre do Itaú precisa ter 25 dígitos.")

    fator = f"{fator_vencimento(vencimento):04d}"
    valor_10 = _centavos(valor)
    sem_dv = f"3419{fator}{valor_10}{campo_livre}"
    if len(sem_dv) != 43:
        raise ValueError("Código de barras sem DV precisa ter 43 dígitos.")
    dv_geral = dac_codigo_barras(sem_dv)
    codigo_barras = f"3419{dv_geral}{fator}{valor_10}{campo_livre}"

    campo1 = f"3419{campo_livre[:5]}"
    campo1 = f"{campo1}{modulo_10(campo1)}"
    campo2 = campo_livre[5:15]
    campo2 = f"{campo2}{modulo_10(campo2)}"
    campo3 = campo_livre[15:25]
    campo3 = f"{campo3}{modulo_10(campo3)}"
    campo5 = f"{fator}{valor_10}"
    linha_numeros = f"{campo1}{campo2}{campo3}{dv_geral}{campo5}"
    linha_formatada = (
        f"{campo1[:5]}.{campo1[5:]} "
        f"{campo2[:5]}.{campo2[5:]} "
        f"{campo3[:5]}.{campo3[5:]} "
        f"{dv_geral} {campo5}"
    )
    digitos_linha = "".join(caractere for caractere in linha_formatada if caractere.isdigit())
    if len(digitos_linha) != 47 or len(codigo_barras) != 44:
        raise ValueError("Boleto Itaú fora do tamanho FEBRABAN.")
    if digitos_linha != linha_numeros:
        raise ValueError("Linha digitável divergente do código de barras.")
    livre = f"{campo1[4:9]}{campo2[:10]}{campo3[:10]}"
    if f"3419{dv_geral}{campo5}{livre}" != codigo_barras:
        raise ValueError("Linha digitável não reconstitui o código de barras.")

    dv_exibido = (conta.conta_dv or str(dac_conta)).strip() or str(dac_conta)
    return {
        "codigo_barras": codigo_barras,
        "linha_digitavel": linha_formatada,
        "linha_numeros": linha_numeros,
        "nosso_numero_formatado": f"{carteira}/{nosso}-{dac_nosso}",
        "agencia_codigo": f"{agencia} / {numero_conta}-{dv_exibido}",
        "carteira": carteira,
        "codigo_banco": "341-7",
        "svg": svg_itf(codigo_barras),
    }


def svg_itf(codigo):
    """Interleaved 2 of 5 em SVG. O código precisa ter quantidade par de dígitos."""
    digitos = "".join(caractere for caractere in str(codigo) if caractere.isdigit())
    if len(digitos) % 2:
        raise ValueError("ITF exige quantidade par de dígitos.")
    elementos = [("bar", 1), ("space", 1), ("bar", 1), ("space", 1)]
    for indice in range(0, len(digitos), 2):
        barras = _ITF[digitos[indice]]
        espacos = _ITF[digitos[indice + 1]]
        for barra, espaco in zip(barras, espacos):
            elementos.append(("bar", 3 if barra == "w" else 1))
            elementos.append(("space", 3 if espaco == "w" else 1))
    elementos.extend((("bar", 3), ("space", 1), ("bar", 1)))
    x = 0
    altura = 60
    partes = []
    for tipo, largura in elementos:
        if tipo == "bar":
            partes.append(
                f'<rect x="{x}" y="0" width="{largura}" height="{altura}" fill="#000"/>'
            )
        x += largura
    corpo = "".join(partes)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {x} {altura}" '
        f'width="100%" height="70" preserveAspectRatio="none" role="img" '
        f'aria-label="Código de barras">{corpo}</svg>'
    )


def dividir_centavos(valor, parcelas):
    """Reparte em N parcelas. A última absorve o centavo restante."""
    total = int(round(float(valor or 0) * 100))
    if parcelas < 1:
        raise ValueError("Quantidade de parcelas inválida.")
    base = total // parcelas
    partes = [base] * parcelas
    partes[-1] = total - base * (parcelas - 1)
    return [parte / 100.0 for parte in partes]


def somar_meses_data(data_base, meses):
    mes_indice = data_base.month - 1 + meses
    ano = data_base.year + mes_indice // 12
    mes = mes_indice % 12 + 1
    ultimo = [
        31,
        29 if ano % 4 == 0 and (ano % 100 != 0 or ano % 400 == 0) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ][mes - 1]
    return date(ano, mes, min(data_base.day, ultimo))
