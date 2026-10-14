"""Correção monetária pela série histórica do catálogo.

PERCENTUAL_MENSAL acumula (1 + taxa/100). NUMERO_INDICE divide o valor do
mês final pelo valor do mês inicial. SOMA_SIMPLES soma os percentuais.
Mês negativo vira zero quando o índice ignora deflação.
"""

import json
from datetime import date
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app import db
from app.financeiro_series_seed import IGPM_MENSAL, IPCA_MENSAL, POUPANCA_MENSAL
from app.models import CatalogoIndice, TipoCalculoIndice, ValorIndiceMensal

_MESES = ("", "Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez")
_ANO_INICIAL = 2009

UFIR_ANUAL = {
    2009: 1.93719995,
    2010: 2.018300056,
    2011: 2.135200024,
    2012: 2.27519989,
    2013: 2.406599998,
    2014: 2.5473001,
    2015: 2.711899996,
    2016: 3.002300024,
    2017: 3.199899912,
    2018: 3.292900085,
    2019: 3.421099901,
    2020: 3.555000067,
    2021: 3.705300093,
    2022: 4.091499805,
    2023: 4.3329,
    2024: 4.53730011,
    2025: 4.7508,
    2026: 4.9604,
}

CATALOGO_PADRAO = (
    ("UFIR-RJ", "UFIR-RJ (SEFAZ-RJ)", "SEFAZ-RJ", TipoCalculoIndice.NUMERO_INDICE, None, "https://portal.fazenda.rj.gov.br/"),
    ("IPCA", "IPCA (IBGE) - BR", "IBGE", TipoCalculoIndice.PERCENTUAL_MENSAL, 433, "https://www.ibge.gov.br/"),
    ("IGP-M", "IGP-M (FGV) - BR", "FGV", TipoCalculoIndice.PERCENTUAL_MENSAL, 189, "https://portal.fgv.br/"),
    ("IGP-DI", "IGP-DI (FGV) - BR", "FGV", TipoCalculoIndice.PERCENTUAL_MENSAL, 190, "https://portal.fgv.br/"),
    ("INPC", "INPC (IBGE) - BR", "IBGE", TipoCalculoIndice.PERCENTUAL_MENSAL, 188, "https://www.ibge.gov.br/"),
    ("INCC-M", "INCC-M (FGV) - BR", "FGV", TipoCalculoIndice.PERCENTUAL_MENSAL, 192, "https://portal.fgv.br/"),
    ("INCC-DI", "INCC-DI (FGV) - BR", "FGV", TipoCalculoIndice.PERCENTUAL_MENSAL, None, "https://portal.fgv.br/"),
    ("POUPANCA", "Poupança - BR", "BACEN", TipoCalculoIndice.PERCENTUAL_MENSAL, 195, "https://www.bcb.gov.br/"),
    ("SELIC", "Selic (Receita Federal)", "BACEN", TipoCalculoIndice.PERCENTUAL_MENSAL, 4390, "https://www.bcb.gov.br/"),
    ("TR", "TR (BACEN) - BR", "BACEN", TipoCalculoIndice.PERCENTUAL_MENSAL, 226, "https://www.bcb.gov.br/"),
    ("IPC-FIPE", "IPC (FIPE) - BR", "FIPE", TipoCalculoIndice.PERCENTUAL_MENSAL, 193, "https://www.fipe.org.br/"),
    ("IPC-DI", "IPC-DI (FGV) - BR", "FGV", TipoCalculoIndice.PERCENTUAL_MENSAL, None, "https://portal.fgv.br/"),
    ("IPA-M", "IPA-M (FGV) - BR", "FGV", TipoCalculoIndice.PERCENTUAL_MENSAL, None, "https://portal.fgv.br/"),
    ("IPA-DI", "IPA-DI (FGV) - BR", "FGV", TipoCalculoIndice.PERCENTUAL_MENSAL, None, "https://portal.fgv.br/"),
    ("SALMIN", "Salário Mínimo - BR", "Governo Federal", TipoCalculoIndice.NUMERO_INDICE, None, "https://www.gov.br/trabalho-e-emprego/"),
    ("TJ-SP", "TJ-SP (TJSP) - BR", "TJ-SP", TipoCalculoIndice.NUMERO_INDICE, None, "https://www.tjsp.jus.br/"),
    ("TJ-MG", "TJ-MG (TJMG) - BR", "TJ-MG", TipoCalculoIndice.NUMERO_INDICE, None, "https://www.tjmg.jus.br/"),
    ("ENCO", "ENCO (JEBR0521N) - BR", "ENCOGE", TipoCalculoIndice.NUMERO_INDICE, None, "https://www.encoge.org.br/"),
    ("ENCOG", "ENCOG (JEBR0719N) - BR", "ENCOGE", TipoCalculoIndice.NUMERO_INDICE, None, "https://www.encoge.org.br/"),
    ("ENCOGE718", "ENCOGE (JEBR0718N) - BR", "ENCOGE", TipoCalculoIndice.NUMERO_INDICE, None, "https://www.encoge.org.br/"),
    ("ENCOGE620", "ENCOGE (JEBR0620N) - BR", "ENCOGE", TipoCalculoIndice.NUMERO_INDICE, None, "https://www.encoge.org.br/"),
    ("ENCONGE", "ENCONGE (JEBR0820N) - BR", "ENCOGE", TipoCalculoIndice.NUMERO_INDICE, None, "https://www.encoge.org.br/"),
)

SERIES_OFFLINE = {
    "IPCA": IPCA_MENSAL,
    "IGP-M": IGPM_MENSAL,
    "POUPANCA": POUPANCA_MENSAL,
}


class SerieIndisponivel(Exception):
    """A consulta ao Banco Central não devolveu a série."""


def rotulo_mes(ano_mes):
    ano, mes = ano_mes.split("-")
    return f"{_MESES[int(mes)]}/{ano}"


def rotulo_formula(tipo):
    if tipo == TipoCalculoIndice.NUMERO_INDICE:
        return "Fator/Nominal"
    if tipo == TipoCalculoIndice.SOMA_SIMPLES:
        return "Soma simples"
    return "% Mensal"


def _meses_entre(inicio, fim, incluir_inicial):
    ano, mes = inicio.year, inicio.month
    if not incluir_inicial:
        mes += 1
        if mes == 13:
            mes = 1
            ano += 1
    limite = (fim.year, fim.month)
    while (ano, mes) <= limite:
        yield f"{ano:04d}-{mes:02d}"
        mes += 1
        if mes == 13:
            mes = 1
            ano += 1


def _taxa(valor, ignorar_deflacao):
    taxa = float(valor or 0.0)
    if ignorar_deflacao and taxa < 0:
        return 0.0
    return taxa


def calcular_correcao_serie(tipo, valores, valor_original, data_inicial, data_final, ignorar_deflacao=True):
    """Devolve fator, correção e a memória mês a mês. Não grava nada."""
    original = round(float(valor_original or 0), 2)
    if data_final < data_inicial:
        raise ValueError("A data final não pode ser anterior à data inicial.")
    memoria = []
    faltantes = []
    if tipo == TipoCalculoIndice.NUMERO_INDICE:
        base_chave = f"{data_inicial.year:04d}-{data_inicial.month:02d}"
        fim_chave = f"{data_final.year:04d}-{data_final.month:02d}"
        base = valores.get(base_chave)
        final = valores.get(fim_chave)
        if base in (None, 0) or final is None:
            if base in (None, 0):
                faltantes.append(base_chave)
            if final is None:
                faltantes.append(fim_chave)
            fator = 1.0
        else:
            fator = float(final) / float(base)
        saldo = original
        for chave in _meses_entre(data_inicial, data_final, incluir_inicial=True):
            ponto = valores.get(chave)
            if ponto is None:
                if chave not in faltantes and chave in (base_chave, fim_chave):
                    faltantes.append(chave)
                memoria.append(
                    {"ano_mes": chave, "rotulo": rotulo_mes(chave), "indice": None, "aplicado": None, "saldo": saldo, "faltou": True}
                )
                continue
            if base not in (None, 0):
                saldo = round(original * (float(ponto) / float(base)), 2)
            memoria.append(
                {
                    "ano_mes": chave,
                    "rotulo": rotulo_mes(chave),
                    "indice": float(ponto),
                    "aplicado": None,
                    "saldo": saldo,
                    "faltou": False,
                }
            )
        correcao = round(original * (fator - 1.0), 2) if not faltantes else 0.0
        return {
            "fator": fator if not faltantes else 1.0,
            "percentual": round(((fator - 1.0) * 100.0), 6) if not faltantes else 0.0,
            "correcao": correcao,
            "memoria": memoria,
            "faltantes": faltantes,
        }

    fator = 1.0
    soma = 0.0
    saldo = original
    memoria.append(
        {
            "ano_mes": f"{data_inicial.year:04d}-{data_inicial.month:02d}",
            "rotulo": rotulo_mes(f"{data_inicial.year:04d}-{data_inicial.month:02d}"),
            "indice": valores.get(f"{data_inicial.year:04d}-{data_inicial.month:02d}"),
            "aplicado": None,
            "saldo": original,
            "faltou": False,
        }
    )
    for chave in _meses_entre(data_inicial, data_final, incluir_inicial=False):
        bruto = valores.get(chave)
        if bruto is None:
            aplicado = 0.0
            faltou = True
        else:
            aplicado = _taxa(bruto, ignorar_deflacao)
            faltou = False
        if tipo == TipoCalculoIndice.SOMA_SIMPLES:
            soma += aplicado
            saldo = round(original * (1.0 + soma / 100.0), 2)
        else:
            fator *= 1.0 + (aplicado / 100.0)
            saldo = round(original * fator, 2)
        memoria.append(
            {
                "ano_mes": chave,
                "rotulo": rotulo_mes(chave),
                "indice": None if bruto is None else float(bruto),
                "aplicado": aplicado,
                "saldo": saldo,
                "faltou": faltou,
            }
        )
    if tipo == TipoCalculoIndice.SOMA_SIMPLES:
        fator = 1.0 + soma / 100.0
    correcao = round(original * (fator - 1.0), 2)
    return {
        "fator": fator,
        "percentual": round((fator - 1.0) * 100.0, 6),
        "correcao": correcao,
        "memoria": memoria,
        "faltantes": [],
    }


def catalogos_visiveis(condominio_id):
    return (
        CatalogoIndice.query.filter(
            db.or_(
                CatalogoIndice.condominio_id.is_(None),
                CatalogoIndice.condominio_id == condominio_id,
            )
        )
        .order_by(CatalogoIndice.nome_completo.asc())
        .all()
    )


def catalogo_da_sigla(sigla, condominio_id=None):
    sigla = (sigla or "").strip()
    if not sigla:
        return None
    if condominio_id is not None:
        proprio = CatalogoIndice.query.filter_by(
            sigla=sigla, condominio_id=condominio_id
        ).first()
        if proprio is not None:
            return proprio
    return CatalogoIndice.query.filter_by(sigla=sigla, condominio_id=None).first()


def mapa_valores(catalogo_id):
    linhas = ValorIndiceMensal.query.filter_by(catalogo_id=catalogo_id).all()
    return {linha.ano_mes: float(linha.valor or 0) for linha in linhas}


def correcao_por_sigla(sigla, valor, data_inicial, data_final, condominio_id=None):
    catalogo = catalogo_da_sigla(sigla, condominio_id)
    if catalogo is None:
        return None
    resultado = calcular_correcao_serie(
        catalogo.tipo_calculo,
        mapa_valores(catalogo.id),
        valor,
        data_inicial,
        data_final,
        ignorar_deflacao=bool(catalogo.ignorar_deflacao),
    )
    resultado["catalogo"] = catalogo
    return resultado


def situacao_serie(valores, hoje, tipo):
    """Verde quando os doze meses até o mês corrente estão preenchidos."""
    del tipo
    if not valores:
        return False
    ano, mes = hoje.year, hoje.month
    for _ in range(12):
        chave = f"{ano:04d}-{mes:02d}"
        if chave not in valores:
            return False
        mes -= 1
        if mes == 0:
            mes = 12
            ano -= 1
    return True


def _gravar_valor(catalogo_id, ano_mes, valor, existentes):
    ano = int(ano_mes[:4])
    mes = int(ano_mes[5:])
    linha = existentes.get(ano_mes)
    if linha is None:
        linha = ValorIndiceMensal(
            catalogo_id=catalogo_id, ano=ano, mes=mes, ano_mes=ano_mes, valor=float(valor)
        )
        db.session.add(linha)
        existentes[ano_mes] = linha
        return True
    return False


def garantir_series_indices():
    """Cria o catálogo padrão e preenche só os meses que ainda não existem."""
    from sqlalchemy import inspect

    tabelas = set(inspect(db.engine).get_table_names())
    if "catalogo_indice" not in tabelas or "valor_indice_mensal" not in tabelas:
        return
    for sigla, nome, orgao, tipo, codigo, url in CATALOGO_PADRAO:
        catalogo = CatalogoIndice.query.filter_by(sigla=sigla, condominio_id=None).first()
        if catalogo is None:
            catalogo = CatalogoIndice(
                condominio_id=None,
                sigla=sigla,
                nome_completo=nome,
                orgao=orgao,
                tipo_calculo=tipo,
                ignorar_deflacao=True,
                codigo_sgs_bacen=codigo,
                url_fonte_oficial=url,
                sistema_padrao=True,
            )
            db.session.add(catalogo)
            db.session.flush()
    db.session.commit()
    ufir = CatalogoIndice.query.filter_by(sigla="UFIR-RJ", condominio_id=None).first()
    if ufir is not None:
        existentes = {
            linha.ano_mes: linha
            for linha in ValorIndiceMensal.query.filter_by(catalogo_id=ufir.id).all()
        }
        for ano, valor in UFIR_ANUAL.items():
            for mes in range(1, 13):
                _gravar_valor(ufir.id, f"{ano:04d}-{mes:02d}", valor, existentes)
    for sigla, serie in SERIES_OFFLINE.items():
        catalogo = CatalogoIndice.query.filter_by(sigla=sigla, condominio_id=None).first()
        if catalogo is None:
            continue
        existentes = {
            linha.ano_mes: linha
            for linha in ValorIndiceMensal.query.filter_by(catalogo_id=catalogo.id).all()
        }
        for ano_mes, valor in serie.items():
            _gravar_valor(catalogo.id, ano_mes, valor, existentes)
    db.session.commit()


def pontos_para_meses(pontos):
    """Um valor por mês. No mês com vários dias, fica o primeiro dia publicado."""
    meses = {}
    for item in pontos:
        bruto = str(item.get("data") or "")
        partes = bruto.split("/")
        if len(partes) != 3:
            continue
        dia, mes, ano = partes
        if not (dia.isdigit() and mes.isdigit() and ano.isdigit()):
            continue
        chave = f"{int(ano):04d}-{int(mes):02d}"
        try:
            valor = float(str(item.get("valor") or "").replace(",", "."))
        except ValueError:
            continue
        if chave not in meses or int(dia) < meses[chave][0]:
            meses[chave] = (int(dia), valor)
    return {chave: par[1] for chave, par in meses.items()}


def buscar_serie_bacen(codigo, ano_inicial=2010, ano_final=None):
    """Consulta o SGS ano a ano. A faixa inteira de uma vez costuma falhar no BACEN."""
    if not isinstance(codigo, int) or codigo <= 0 or codigo > 999999:
        raise SerieIndisponivel("Código SGS inválido.")
    if ano_final is None:
        ano_final = date.today().year
    pontos = []
    falhas = 0
    for ano in range(ano_inicial, ano_final + 1):
        url = (
            "https://api.bcb.gov.br/dados/serie/bcdata.sgs."
            f"{codigo}/dados?formato=json&dataInicial=01/01/{ano}&dataFinal=01/12/{ano}"
        )
        pedido = Request(url, headers={"User-Agent": "Vizinsync", "Accept": "application/json"})
        try:
            with urlopen(pedido, timeout=20) as resposta:
                corpo = resposta.read(2_000_000)
        except (HTTPError, URLError, TimeoutError, OSError):
            falhas += 1
            continue
        try:
            dados = json.loads(corpo.decode("utf-8"))
        except json.JSONDecodeError:
            falhas += 1
            continue
        if isinstance(dados, list):
            pontos.extend(dados)
        else:
            falhas += 1
    if not pontos and falhas:
        raise SerieIndisponivel("O Banco Central não respondeu a série pedida.")
    return pontos_para_meses(pontos)


def atualizar_serie(catalogo, meses, substituir=True):
    existentes = {
        linha.ano_mes: linha
        for linha in ValorIndiceMensal.query.filter_by(catalogo_id=catalogo.id).all()
    }
    gravados = 0
    for ano_mes, valor in meses.items():
        if len(ano_mes) != 7 or ano_mes[4] != "-":
            continue
        linha = existentes.get(ano_mes)
        if linha is None:
            db.session.add(
                ValorIndiceMensal(
                    catalogo_id=catalogo.id,
                    ano=int(ano_mes[:4]),
                    mes=int(ano_mes[5:]),
                    ano_mes=ano_mes,
                    valor=float(valor),
                )
            )
            gravados += 1
        elif substituir and float(linha.valor) != float(valor):
            linha.valor = float(valor)
            gravados += 1
    return gravados
