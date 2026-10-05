"""Remessa e retorno CNAB 400 do Itaú, com leitura auxiliar de CNAB 240 e CSV.

Cada linha da remessa tem 400 caracteres ASCII e termina com CR/LF.
O número da linha no arquivo começa em 000001. O sequencial do lote
fica no registro do arquivo, não no header.
"""

import csv
import io
import json
import re
from datetime import date

_ACENTOS = str.maketrans(
    "ÁÀÂÃÄáàâãäÉÈÊËéèêëÍÌÎÏíìîïÓÒÔÕÖóòôõöÚÙÛÜúùûüÇçÑñ",
    "AAAAAaaaaaEEEEeeeeIIIIiiiiOOOOOoooooUUUUuuuuCcNn",
)
_LIQUIDACOES = {"06", "07", "08", "10"}
FORMA_RETORNO = "Boleto - Retorno CNAB"


class RetornoInvalido(ValueError):
    """Arquivo enviado não é um retorno reconhecível."""


def _ascii(texto, tamanho):
    limpo = (
        str(texto or "")
        .translate(_ACENTOS)
        .encode("ascii", "ignore")
        .decode("ascii")
        .upper()
    )
    limpo = re.sub(r"[^A-Z0-9 ./,-]", " ", limpo)
    limpo = re.sub(r"\s+", " ", limpo).strip()
    return limpo[:tamanho].ljust(tamanho)


def _digitos(texto, tamanho):
    numeros = "".join(caractere for caractere in str(texto or "") if caractere.isdigit())
    if len(numeros) > tamanho:
        numeros = numeros[-tamanho:]
    return numeros.zfill(tamanho)


def _centavos(valor, tamanho):
    centavos = int(round(float(valor or 0) * 100))
    if centavos < 0:
        centavos = 0
    return f"{centavos:0{tamanho}d}"[-tamanho:]


def _data6(data_ref):
    return data_ref.strftime("%d%m%y")


def _colocar(buf, inicio, texto):
    texto = str(texto)
    if inicio < 1 or inicio - 1 + len(texto) > len(buf):
        raise ValueError("Campo CNAB fora da linha de 400 posições.")
    for indice, caractere in enumerate(texto):
        buf[inicio - 1 + indice] = caractere


def _linha_vazia():
    return [" "] * 400


def _fechar(buf, sequencia):
    _colocar(buf, 395, f"{sequencia:06d}")
    linha = "".join(buf)
    if len(linha) != 400 or any(ord(caractere) > 127 for caractere in linha):
        raise ValueError("Linha CNAB inválida.")
    return linha


def _documento(valor):
    digitos = "".join(caractere for caractere in str(valor or "") if caractere.isdigit())
    if len(digitos) > 11:
        return "02", _digitos(digitos, 14)
    return "01", _digitos(digitos, 14)


def _juros_dia(valor, juros_mensal):
    diario = float(valor or 0) * (float(juros_mensal or 0) / 100.0) / 30.0
    return round(diario, 2)


def montar_remessa_itau(conta, condominio, cobrancas, data_geracao, juros_mensal=1.0):
    """Devolve o texto .REM e o valor de face somado. Não grava o lote."""
    if conta is None or str(conta.codigo_banco or "").strip() != "341":
        raise ValueError("A remessa CNAB 400 desta fase é do Itaú 341.")
    if not cobrancas:
        raise ValueError("Selecione ao menos uma cobrança em aberto.")

    agencia = _digitos(conta.agencia, 4)
    numero_conta = _digitos(conta.conta, 5)
    dac = _digitos(conta.conta_dv or "0", 1)
    carteira = _digitos(conta.carteira or "109", 3)
    codigo_carteira = "I" if carteira == "109" else "1"
    tipo_empresa, documento_empresa = _documento(getattr(condominio, "cnpj", ""))
    nome = _ascii(
        getattr(condominio, "razao_social", None) or getattr(condominio, "nome", ""),
        30,
    )
    endereco = _ascii(
        " ".join(
            parte
            for parte in (
                getattr(condominio, "logradouro", None),
                getattr(condominio, "numero", None),
            )
            if parte
        ),
        40,
    )
    bairro = _ascii(getattr(condominio, "bairro", None), 12)
    cidade = _ascii(getattr(condominio, "cidade", None), 15)
    uf = _ascii(getattr(condominio, "uf", None), 2)
    cep = _digitos(getattr(condominio, "cep", None), 8)

    linhas = []
    cabecalho = _linha_vazia()
    _colocar(cabecalho, 1, "01REMESSA01" + "COBRANCA".ljust(15))
    _colocar(cabecalho, 27, agencia)
    _colocar(cabecalho, 31, "00")
    _colocar(cabecalho, 33, numero_conta)
    _colocar(cabecalho, 38, dac)
    _colocar(cabecalho, 47, nome)
    _colocar(cabecalho, 77, "341")
    _colocar(cabecalho, 80, "BANCO ITAU SA  ")
    _colocar(cabecalho, 95, _data6(data_geracao))
    linhas.append(_fechar(cabecalho, 1))

    total = 0.0
    for indice, cobranca in enumerate(cobrancas, start=2):
        tipo_pagador, documento_pagador = _documento(cobranca.pagador_documento)
        valor = round(float(cobranca.valor_original or 0), 2)
        total = round(total + valor, 2)
        detalhe = _linha_vazia()
        _colocar(detalhe, 1, "1")
        _colocar(detalhe, 2, tipo_empresa)
        _colocar(detalhe, 4, documento_empresa)
        _colocar(detalhe, 18, agencia)
        _colocar(detalhe, 22, "00")
        _colocar(detalhe, 24, numero_conta)
        _colocar(detalhe, 29, dac)
        _colocar(detalhe, 34, "0000")
        _colocar(detalhe, 38, _digitos(cobranca.id, 25))
        _colocar(detalhe, 63, _digitos(cobranca.nosso_numero, 8))
        _colocar(detalhe, 71, "0" * 13)
        _colocar(detalhe, 84, carteira)
        _colocar(detalhe, 108, codigo_carteira)
        _colocar(detalhe, 109, "01")
        _colocar(detalhe, 111, _digitos(cobranca.id, 10))
        _colocar(detalhe, 121, _data6(cobranca.vencimento))
        _colocar(detalhe, 127, _centavos(valor, 13))
        _colocar(detalhe, 140, "341")
        _colocar(detalhe, 143, "00000")
        _colocar(detalhe, 148, "01")
        _colocar(detalhe, 150, "N")
        emissao = cobranca.criado_em.date() if cobranca.criado_em else data_geracao
        _colocar(detalhe, 151, _data6(emissao))
        _colocar(detalhe, 157, "00")
        _colocar(detalhe, 159, "00")
        _colocar(detalhe, 161, _centavos(_juros_dia(valor, juros_mensal), 13))
        _colocar(detalhe, 174, "0" * 6)
        _colocar(detalhe, 180, "0" * 13)
        _colocar(detalhe, 193, "0" * 13)
        _colocar(detalhe, 206, "0" * 13)
        _colocar(detalhe, 219, tipo_pagador)
        _colocar(detalhe, 221, documento_pagador)
        _colocar(detalhe, 235, _ascii(cobranca.pagador_nome, 30))
        _colocar(detalhe, 275, endereco)
        _colocar(detalhe, 315, bairro)
        _colocar(detalhe, 327, cep)
        _colocar(detalhe, 335, cidade)
        _colocar(detalhe, 350, uf)
        _colocar(detalhe, 386, "0" * 6)
        _colocar(detalhe, 392, "00")
        linhas.append(_fechar(detalhe, indice))

    trailer = _linha_vazia()
    _colocar(trailer, 1, "9")
    linhas.append(_fechar(trailer, len(linhas) + 1))
    return "\r\n".join(linhas) + "\r\n", round(total, 2)


def nome_remessa(data_geracao):
    return f"CB{data_geracao.strftime('%d%m%y')}.REM"


def nome_retorno_teste(data_geracao):
    return f"CN{data_geracao.strftime('%d%m%y')}.RET"


def montar_retorno_teste(cobrancas, data_ocorrencia):
    """CNAB 400 de simulação: ocorrência 06 no valor de face de cada título."""
    if not cobrancas:
        raise ValueError("Não há cobrança em aberto para simular o retorno.")
    linhas = []
    cabecalho = _linha_vazia()
    _colocar(cabecalho, 1, "02RETORNO01" + "COBRANCA".ljust(15))
    _colocar(cabecalho, 77, "341")
    _colocar(cabecalho, 95, _data6(data_ocorrencia))
    linhas.append(_fechar(cabecalho, 1))
    for indice, cobranca in enumerate(cobrancas, start=2):
        detalhe = _linha_vazia()
        _colocar(detalhe, 1, "1")
        _colocar(detalhe, 63, _digitos(cobranca.nosso_numero, 8))
        _colocar(detalhe, 86, _digitos(cobranca.nosso_numero, 8))
        _colocar(detalhe, 109, "06")
        _colocar(detalhe, 111, _data6(data_ocorrencia))
        _colocar(detalhe, 117, _digitos(cobranca.id, 10))
        _colocar(detalhe, 254, _centavos(cobranca.valor_original, 13))
        _colocar(detalhe, 296, _data6(data_ocorrencia))
        linhas.append(_fechar(detalhe, indice))
    trailer = _linha_vazia()
    _colocar(trailer, 1, "9")
    linhas.append(_fechar(trailer, len(linhas) + 1))
    return "\r\n".join(linhas) + "\r\n"


def _data_curta(texto):
    bruto = "".join(caractere for caractere in str(texto or "") if caractere.isdigit())
    if len(bruto) == 8:
        dia, mes, ano = int(bruto[:2]), int(bruto[2:4]), int(bruto[4:8])
    elif len(bruto) == 6 and bruto != "000000":
        dia, mes, ano = int(bruto[:2]), int(bruto[2:4]), int(bruto[4:6])
        ano += 2000 if ano < 80 else 1900
    else:
        return None
    try:
        return date(ano, mes, dia)
    except ValueError:
        return None


def _data_livre(texto):
    bruto = (texto or "").strip()
    if not bruto:
        return None
    if "/" in bruto:
        partes = bruto.split("/")
        if len(partes) == 3 and all(parte.isdigit() for parte in partes):
            ano = int(partes[2])
            if ano < 100:
                ano += 2000
            try:
                return date(ano, int(partes[1]), int(partes[0]))
            except ValueError:
                return None
    if len(bruto) == 10 and bruto[4] == "-":
        try:
            return date.fromisoformat(bruto)
        except ValueError:
            return None
    return _data_curta(bruto)


def _valor_livre(texto):
    bruto = str(texto or "").strip().replace("R$", "").replace(" ", "")
    if not bruto:
        return 0.0
    if bruto.isdigit() and len(bruto) >= 3:
        return round(int(bruto) / 100.0, 2)
    if "," in bruto and "." in bruto:
        bruto = bruto.replace(".", "").replace(",", ".")
    elif "," in bruto:
        bruto = bruto.replace(",", ".")
    try:
        return round(float(bruto), 2)
    except ValueError:
        return 0.0


def _linhas_arquivo(texto):
    return [linha.rstrip("\r") for linha in str(texto or "").split("\n") if linha.strip()]


def _item_cnab400(linha):
    if len(linha) < 400 or linha[0] != "1":
        return None
    nosso_principal = linha[62:70].strip()
    nosso_alternativo = linha[85:93].strip()
    nosso = "".join(caractere for caractere in (nosso_principal or nosso_alternativo) if caractere.isdigit())
    return {
        "ocorrencia": linha[108:110].strip(),
        "nosso": nosso[-8:] if nosso else "",
        "seu": linha[116:126].strip(),
        "data_pagamento": _data_curta(linha[110:116]),
        "data_credito": _data_curta(linha[295:301]),
        "valor": (
            round(int(linha[253:266].strip()) / 100.0, 2)
            if linha[253:266].strip().isdigit()
            else 0.0
        ),
    }


def _itens_cnab240(linhas):
    itens = []
    pendente = None
    for linha in linhas:
        if len(linha) < 240 or linha[7] != "3":
            continue
        segmento = linha[13]
        if segmento == "T":
            if pendente:
                itens.append(pendente)
            nosso = "".join(caractere for caractere in linha[37:57] if caractere.isdigit())
            pendente = {
                "ocorrencia": linha[15:17].strip(),
                "nosso": nosso[-8:] if nosso else "",
                "seu": linha[58:73].strip(),
                "data_pagamento": None,
                "data_credito": None,
                "valor": 0.0,
            }
        elif segmento == "U" and pendente is not None:
            valor = linha[77:92].strip()
            pendente["valor"] = round(int(valor) / 100.0, 2) if valor.isdigit() else 0.0
            pendente["data_pagamento"] = _data_curta(linha[137:145])
            pendente["data_credito"] = _data_curta(linha[145:153])
            itens.append(pendente)
            pendente = None
    if pendente:
        itens.append(pendente)
    return itens


def _itens_csv(linhas):
    amostra = linhas[0]
    delimitador = ";" if amostra.count(";") >= amostra.count(",") else ","
    leitor = csv.reader(io.StringIO("\n".join(linhas)), delimiter=delimitador)
    registros = [linha for linha in leitor if any(celula.strip() for celula in linha)]
    if not registros:
        return []
    cabecalho = [celula.strip().lower() for celula in registros[0]]
    if any(nome in "".join(cabecalho) for nome in ("ocorr", "nosso", "seu")):
        mapa = {nome: indice for indice, nome in enumerate(cabecalho)}

        def coluna(registro, *nomes):
            for nome in nomes:
                for chave, indice in mapa.items():
                    if nome in chave and indice < len(registro):
                        return registro[indice].strip()
            return ""

        itens = []
        for registro in registros[1:]:
            itens.append(
                {
                    "ocorrencia": _digitos(coluna(registro, "ocorr"), 2),
                    "nosso": _digitos(coluna(registro, "nosso"), 8),
                    "seu": coluna(registro, "seu", "id"),
                    "data_pagamento": _data_livre(coluna(registro, "pagamento", "ocorrencia_data", "data")),
                    "data_credito": _data_livre(coluna(registro, "credito", "extrato")),
                    "valor": _valor_livre(coluna(registro, "valor", "pago")),
                }
            )
        return itens
    itens = []
    for registro in registros:
        while len(registro) < 6:
            registro.append("")
        itens.append(
            {
                "ocorrencia": _digitos(registro[2], 2),
                "nosso": _digitos(registro[0], 8),
                "seu": registro[1].strip(),
                "data_pagamento": _data_livre(registro[3]),
                "data_credito": _data_livre(registro[4]),
                "valor": _valor_livre(registro[5]),
            }
        )
    return itens


def interpretar_retorno(texto):
    """Lê CNAB 400, CNAB 240 ou CSV. Remessa é recusada."""
    linhas = _linhas_arquivo(texto)
    if not linhas:
        raise RetornoInvalido("O arquivo está vazio.")
    primeira = linhas[0]
    if len(primeira) >= 400 and "REMESSA" in primeira[:30]:
        raise RetornoInvalido("Este arquivo é uma remessa, não um retorno do banco.")
    if len(primeira) >= 400 and primeira[0] in "019":
        itens = [item for item in (_item_cnab400(linha) for linha in linhas) if item]
        return itens, "CNAB400_ITAU"
    if len(primeira) >= 240 and len(primeira) < 400:
        return _itens_cnab240(linhas), "CNAB240"
    return _itens_csv(linhas), "CSV"


def _buscar_cobranca(condominio_id, seu, nosso):
    from app.models import CobrancaUnidade

    seu_limpo = "".join(caractere for caractere in str(seu or "") if caractere.isdigit())
    if seu_limpo:
        cobranca = CobrancaUnidade.query.filter_by(
            condominio_id=condominio_id, id=int(seu_limpo)
        ).first()
        if cobranca is not None:
            return cobranca
    if not nosso:
        return None
    candidatos = CobrancaUnidade.query.filter(
        CobrancaUnidade.condominio_id == condominio_id,
        CobrancaUnidade.nosso_numero.endswith(nosso[-8:]),
    ).all()
    iguais = [
        candidata
        for candidata in candidatos
        if str(candidata.nosso_numero or "").zfill(8)[-8:] == nosso[-8:].zfill(8)
    ]
    if len(iguais) == 1:
        return iguais[0]
    return None


def aplicar_retorno(condominio_id, texto):
    """Atualiza cobranças e saldo. Quem chama faz o commit."""
    from app.models import StatusAcordo, StatusBanco, StatusCobranca

    from app.blueprints.financeiro import _sincronizar_acordo

    itens, layout = interpretar_retorno(texto)
    relatorio = {
        "layout": layout,
        "liquidados": [],
        "confirmados": [],
        "rejeitados": [],
        "ja_pagos": [],
        "ignorados": [],
        "nao_encontrados": [],
        "valor_creditado": 0.0,
    }
    for item in itens:
        cobranca = _buscar_cobranca(condominio_id, item.get("seu"), item.get("nosso"))
        if cobranca is None:
            relatorio["nao_encontrados"].append(item.get("nosso") or item.get("seu") or "?")
            continue
        ocorrencia = _digitos(item.get("ocorrencia"), 2)
        cobranca.codigo_ocorrencia_banco = ocorrencia or None
        if ocorrencia == "02":
            cobranca.status_banco = StatusBanco.REGISTRADO
            relatorio["confirmados"].append(cobranca.id)
            continue
        if ocorrencia == "03":
            cobranca.status_banco = StatusBanco.REJEITADO
            relatorio["rejeitados"].append(cobranca.id)
            continue
        if ocorrencia not in _LIQUIDACOES:
            relatorio["ignorados"].append(cobranca.id)
            continue
        if cobranca.status == StatusCobranca.CANCELADA:
            relatorio["ignorados"].append(cobranca.id)
            continue
        if cobranca.status == StatusCobranca.PAGA:
            relatorio["ja_pagos"].append(cobranca.id)
            continue
        from app.financeiro_fechamento import competencia_esta_fechada

        if competencia_esta_fechada(condominio_id, cobranca.competencia):
            relatorio.setdefault("fechados", []).append(cobranca.id)
            continue
        valor = round(float(item.get("valor") or 0), 2)
        if valor <= 0:
            relatorio["ignorados"].append(cobranca.id)
            continue
        original = round(float(cobranca.valor_original or 0), 2)
        excesso = round(valor - original, 2)
        if excesso > 0:
            ja_lancado = round(
                float(cobranca.valor_correcao or 0)
                + float(cobranca.valor_multa or 0)
                + float(cobranca.valor_juros or 0)
                + float(cobranca.valor_outros_acrescimos or 0)
                - float(cobranca.valor_desconto or 0),
                2,
            )
            falta = round(excesso - ja_lancado, 2)
            if falta > 0:
                cobranca.valor_outros_acrescimos = round(
                    float(cobranca.valor_outros_acrescimos or 0) + falta, 2
                )
        cobranca.valor_pago = valor
        from app.financeiro_boleto import hoje_sp

        cobranca.data_pagamento = item.get("data_pagamento") or hoje_sp()
        cobranca.data_extrato = item.get("data_credito") or cobranca.data_pagamento
        cobranca.forma_pagamento = FORMA_RETORNO
        cobranca.status = StatusCobranca.PAGA
        cobranca.status_banco = StatusBanco.LIQUIDADO
        if cobranca.conta_bancaria is not None:
            cobranca.conta_bancaria.saldo_atual = round(
                float(cobranca.conta_bancaria.saldo_atual or 0) + valor, 2
            )
        if cobranca.acordo_id:
            from app.models import AcordoFinanceiro

            acordo = AcordoFinanceiro.query.filter_by(
                id=cobranca.acordo_id, condominio_id=condominio_id
            ).first()
            if acordo is not None and acordo.status == StatusAcordo.ATIVO:
                _sincronizar_acordo(acordo)
        relatorio["liquidados"].append({"id": cobranca.id, "valor": valor})
        relatorio["valor_creditado"] = round(relatorio["valor_creditado"] + valor, 2)
    relatorio["qtd_titulos"] = len(itens)
    return relatorio


def resumo_json(relatorio):
    return json.dumps(relatorio, ensure_ascii=True)
