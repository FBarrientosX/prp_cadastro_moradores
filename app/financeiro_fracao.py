"""Cálculo de cotas por fração. O rateio sem grupo continua igualitário."""

from datetime import datetime

TITULO_IGUALITARIA = "Fração Geral Igualitária (Cota Igual = 1,0)"


def _centavos(valor):
    return int(round(float(valor or 0) * 100))


def _repartir(total_centavos, pesos):
    """Distribui centavos na proporção dos pesos. O maior peso absorve o resto."""
    quantidade = len(pesos)
    if quantidade == 0 or total_centavos <= 0:
        return [0] * quantidade
    escalas = [max(0, int(round(float(peso) * 1_000_000))) for peso in pesos]
    soma = sum(escalas)
    if soma <= 0:
        return [0] * quantidade
    partes = [total_centavos * escala // soma for escala in escalas]
    resto = total_centavos - sum(partes)
    ordem = sorted(range(quantidade), key=lambda indice: (escalas[indice], -indice), reverse=True)
    for passo in range(resto):
        partes[ordem[passo % quantidade]] += 1
    return partes


def peso_efetivo(valor_base, ativa, isencao_tipo, isencao_valor):
    if not ativa:
        return 0.0
    base = max(0.0, float(valor_base or 0))
    if (isencao_tipo or "NENHUMA") == "PERCENTUAL":
        percentual = min(max(float(isencao_valor or 0), 0.0), 100.0)
        return max(0.0, base * (1.0 - percentual / 100.0))
    return base


def coeficiente_percentual(valor_base, ativa, modo_calculo, soma_ativas):
    if not ativa:
        return 0.0
    base = max(0.0, float(valor_base or 0))
    if modo_calculo == "PROPORCAO":
        return base
    if soma_ativas <= 0:
        return 0.0
    return base / soma_ativas * 100.0


def montar_cotas(modo_calculo, redistribuir, modo_rateio, valor_referencia, linhas):
    """Devolve a cota de cada linha ativa, em reais com duas casas.

    VALOR_UNITARIO + VALOR: referência vezes o peso.
    VALOR_UNITARIO + PROPORCAO: referência vezes a quantidade ativa vezes o percentual.
    DIVIDIR_TOTAL + VALOR: reparte o total pelos pesos.
    DIVIDIR_TOTAL + PROPORCAO: cada percentual incide direto sobre o total.
    Isenção percentual ou fixa sai da cota. Com redistribuição, esse valor
    volta para quem ainda tem saldo, e a soma arrecadada permanece a soma bruta.
    """
    participantes = [linha for linha in linhas if linha.get("ativa")]
    if not participantes:
        return None
    modo = modo_calculo if modo_calculo in ("VALOR", "PROPORCAO") else "VALOR"
    rateio = modo_rateio if modo_rateio in ("VALOR_UNITARIO", "DIVIDIR_TOTAL") else "VALOR_UNITARIO"
    pesos = [max(0.0, float(linha.get("valor_base") or 0)) for linha in participantes]
    if sum(pesos) <= 0 or float(valor_referencia or 0) <= 0:
        return None

    if rateio == "DIVIDIR_TOTAL" and modo == "VALOR":
        brutos = _repartir(_centavos(valor_referencia), pesos)
    elif rateio == "DIVIDIR_TOTAL":
        brutos = [_centavos(float(valor_referencia) * peso / 100.0) for peso in pesos]
    elif modo == "PROPORCAO":
        quantidade = len(participantes)
        brutos = [
            _centavos(float(valor_referencia) * quantidade * peso / 100.0) for peso in pesos
        ]
    else:
        brutos = [_centavos(float(valor_referencia) * peso) for peso in pesos]

    descontos = []
    liquidos = []
    for linha, bruto in zip(participantes, brutos):
        tipo = linha.get("isencao_tipo") or "NENHUMA"
        if tipo == "PERCENTUAL":
            percentual = min(max(float(linha.get("isencao_valor") or 0), 0.0), 100.0)
            desconto = int(round(bruto * percentual / 100.0))
        elif tipo == "VALOR_FIXO":
            desconto = min(bruto, _centavos(linha.get("isencao_valor")))
        else:
            desconto = 0
        descontos.append(desconto)
        liquidos.append(max(0, bruto - desconto))

    finais = list(liquidos)
    if redistribuir:
        pool = sum(descontos)
        destinatarios = [indice for indice, liquido in enumerate(liquidos) if liquido > 0]
        if pool and destinatarios:
            extras = _repartir(pool, [liquidos[indice] for indice in destinatarios])
            for indice, extra in zip(destinatarios, extras):
                finais[indice] = liquidos[indice] + extra

    cotas = []
    for linha, bruto, desconto, final in zip(participantes, brutos, descontos, finais):
        cotas.append(
            {
                "unidade_id": linha.get("unidade_id"),
                "bruto": bruto / 100.0,
                "desconto": desconto / 100.0,
                "final": final / 100.0,
                "motivo": (linha.get("motivo") or "").strip(),
            }
        )
    return cotas


def escalar_composicao(itens, destino):
    """Reparte a composição para somar exatamente o valor líquido da unidade."""
    bruto = round(sum(float(item.get("valor") or 0) for item in itens), 2)
    destino_centavos = _centavos(destino)
    if bruto <= 0 or destino_centavos <= 0:
        return []
    partes = []
    acumulado = 0
    for indice, item in enumerate(itens):
        copia = dict(item)
        if indice == len(itens) - 1:
            cents = destino_centavos - acumulado
        else:
            cents = int(round(float(item.get("valor") or 0) / bruto * destino_centavos))
            acumulado += cents
        if cents < 0:
            cents = 0
        copia["valor"] = cents / 100.0
        partes.append(copia)
    return partes


def cotas_para_rateio(grupo, unidades, valor_referencia, modo_rateio):
    """Cotas só das unidades elegíveis que estão ativas nesta fração."""
    from app.models import ItemFracaoUnidade

    ids = [unidade.id for unidade in unidades]
    if not ids:
        return None
    itens = ItemFracaoUnidade.query.filter(
        ItemFracaoUnidade.grupo_fracao_id == grupo.id,
        ItemFracaoUnidade.unidade_id.in_(ids),
        ItemFracaoUnidade.ativa_no_rateio.is_(True),
    ).all()
    linhas = [
        {
            "unidade_id": item.unidade_id,
            "valor_base": float(item.valor_base or 0),
            "ativa": True,
            "isencao_tipo": item.isencao_tipo or "NENHUMA",
            "isencao_valor": float(item.isencao_valor or 0),
            "motivo": item.motivo_isencao or "",
        }
        for item in itens
    ]
    return montar_cotas(
        grupo.modo_calculo,
        bool(grupo.redistribuir_isencoes),
        modo_rateio,
        valor_referencia,
        linhas,
    )


def unidades_residenciais(condominio_id):
    from app.models import Unidade

    return (
        Unidade.query.filter(
            Unidade.condominio_id == condominio_id,
            Unidade.eh_setor_interno.is_(False),
        )
        .order_by(Unidade.bloco.asc(), Unidade.apartamento.asc(), Unidade.id.asc())
        .all()
    )


def incluir_unidades(grupo, unidades, valor_base=1.0):
    """Cria o item das unidades que ainda não estão no grupo. Não faz commit."""
    from app import db
    from app.models import ItemFracaoUnidade, TipoIsencaoFracao

    if not unidades:
        return 0
    existentes = {
        unidade_id
        for (unidade_id,) in db.session.query(ItemFracaoUnidade.unidade_id)
        .filter(ItemFracaoUnidade.grupo_fracao_id == grupo.id)
        .all()
    }
    novas = 0
    for unidade in unidades:
        if unidade.id in existentes or unidade.eh_setor_interno:
            continue
        if unidade.condominio_id != grupo.condominio_id:
            continue
        db.session.add(
            ItemFracaoUnidade(
                grupo_fracao_id=grupo.id,
                unidade_id=unidade.id,
                valor_base=valor_base,
                ativa_no_rateio=True,
                isencao_tipo=TipoIsencaoFracao.NENHUMA,
                isencao_valor=0.0,
            )
        )
        novas += 1
    return novas


def garantir_fracao_igualitaria():
    """Um grupo de cota igual por condomínio que ainda não tem fração nenhuma."""
    from app import db
    from app.models import Condominio, GrupoFracao, ModoFracao

    agora = datetime.utcnow()
    for condominio in Condominio.query.order_by(Condominio.id.asc()).all():
        existe = GrupoFracao.query.filter_by(condominio_id=condominio.id).first()
        if existe is not None:
            continue
        grupo = GrupoFracao(
            condominio_id=condominio.id,
            titulo=TITULO_IGUALITARIA,
            modo_calculo=ModoFracao.VALOR,
            redistribuir_isencoes=True,
            padrao=True,
            criado_em=agora,
            atualizado_em=agora,
        )
        db.session.add(grupo)
        db.session.flush()
        incluir_unidades(grupo, unidades_residenciais(condominio.id), 1.0)
    db.session.commit()
