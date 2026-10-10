"""Medidores de água, gás e energia. Admin do condomínio e síndico do bloco."""

import json
import uuid
from datetime import datetime

from flask import (
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

from app import db
from app.auth import get_current_user, unidade_required
from app.financeiro_medidores import calcular_linhas
from app.models import (
    CicloLeituraMedidor,
    FundoFinanceiro,
    ItemLeituraMedidor,
    MedidorConfig,
    ModoCalculoMedidor,
    NivelMedicao,
    ParticipanteMedidor,
    PlanoConta,
    Role,
    StatusCicloMedidor,
    TipoRecursoMedidor,
    Unidade,
)
from app.utils import get_blocos

_EXTENSOES_FOTO = {"jpg", "jpeg", "png", "webp"}
_TIPOS = (
    TipoRecursoMedidor.AGUA,
    TipoRecursoMedidor.GAS,
    TipoRecursoMedidor.ENERGIA,
    TipoRecursoMedidor.OUTRO,
)
_NIVEIS = (NivelMedicao.POR_UNIDADE, NivelMedicao.POR_BLOCO, NivelMedicao.ADM_SETOR)
_MODOS = (
    ModoCalculoMedidor.METRAGEM,
    ModoCalculoMedidor.POR_FAIXA,
    ModoCalculoMedidor.RATEIO_FATURA,
    ModoCalculoMedidor.MONITORAMENTO_DESPESA,
)
_MEDIDAS = ("m³", "kWh", "kg", "L", "unid")


def _marcado(nome):
    return "1" in request.form.getlist(nome)


def _numero(texto, casas=3):
    bruto = str(texto or "").strip().replace(" ", "")
    if not bruto:
        return None
    if "," in bruto and "." in bruto:
        bruto = bruto.replace(".", "").replace(",", ".")
    else:
        bruto = bruto.replace(",", ".")
    valor = float(bruto)
    if valor < 0 or valor > 10_000_000:
        raise ValueError("fora do intervalo")
    return round(valor, casas)


def _auditar(mensagem):
    from app.routes import _registrar_auditoria

    usuario = get_current_user()
    if usuario is not None:
        _registrar_auditoria(usuario, mensagem)


def _exigir_gestor():
    from app.auth import _redirect_login_tenant

    usuario = get_current_user()
    if usuario is None:
        return None, _redirect_login_tenant()
    if not usuario.condominio_id or usuario.role not in (Role.ADMIN, Role.SINDICO):
        flash("Acesso restrito à administração.", "danger")
        if getattr(usuario, "role", None) == Role.ASSISTENTE:
            return None, redirect(url_for("admin_index"))
        return None, _redirect_login_tenant()
    return usuario, None


def _blocos_do_gestor(usuario):
    if usuario.role == Role.ADMIN:
        return None
    return usuario.get_blocos_permitidos()


def _medidores_visiveis(usuario):
    consulta = MedidorConfig.query.filter_by(condominio_id=usuario.condominio_id)
    if usuario.role == Role.ADMIN:
        return consulta.order_by(MedidorConfig.titulo.asc())
    consulta = consulta.filter(
        MedidorConfig.nivel_medicao != NivelMedicao.ADM_SETOR,
        MedidorConfig.bloco_vinculado != "GERAL",
    )
    blocos = _blocos_do_gestor(usuario)
    if blocos is None:
        return consulta.order_by(MedidorConfig.titulo.asc())
    if not blocos:
        return consulta.filter(MedidorConfig.id == 0)
    return consulta.filter(MedidorConfig.bloco_vinculado.in_(blocos)).order_by(
        MedidorConfig.titulo.asc()
    )


def _pode_medidor(usuario, medidor):
    if medidor is None or medidor.condominio_id != usuario.condominio_id:
        return False
    if usuario.role == Role.ADMIN:
        return True
    if medidor.nivel_medicao == NivelMedicao.ADM_SETOR or medidor.bloco_vinculado in ("", "GERAL"):
        return False
    blocos = _blocos_do_gestor(usuario)
    if blocos is None:
        return True
    return medidor.bloco_vinculado in blocos


def _destino(usuario, medidor_id=None, ciclo_id=None):
    endpoint = (
        "sindico_medidores" if usuario.role == Role.SINDICO else "admin_financeiro_medidores"
    )
    return redirect(url_for(endpoint, medidor=medidor_id, ciclo=ciclo_id))


def _competencia_valida(texto):
    bruto = (texto or "").strip()
    if len(bruto) != 7 or bruto[2] != "/":
        return None
    try:
        mes = int(bruto[:2])
        ano = int(bruto[3:])
    except ValueError:
        return None
    if mes < 1 or mes > 12 or ano < 2000 or ano > 2100:
        return None
    return f"{mes:02d}/{ano}"


def _faixas_do_form(form):
    faixas = []
    for indice in range(4):
        de = form.get(f"faixa_{indice}_de")
        ate = form.get(f"faixa_{indice}_ate")
        preco = form.get(f"faixa_{indice}_valor")
        taxa = form.get(f"faixa_{indice}_taxa")
        if not str(de or "").strip() and not str(ate or "").strip() and not str(preco or "").strip():
            continue
        inicio = _numero(de, 3)
        fim = _numero(ate, 3)
        valor = _numero(preco, 4)
        taxa_faixa = _numero(taxa or "0", 2) or 0.0
        if inicio is None or fim is None or valor is None or fim <= inicio:
            raise ValueError("Revise as faixas: cada uma precisa de início, fim e preço, com fim maior que o início.")
        faixas.append({"de": inicio, "ate": fim, "valor_m3": valor, "taxa_fixa": taxa_faixa})
    return faixas


def _unidades_escolhidas(usuario, bloco, ids):
    consulta = Unidade.query.filter(
        Unidade.condominio_id == usuario.condominio_id,
        Unidade.eh_setor_interno.is_(False),
        Unidade.criada_pela_admin.is_(True),
    )
    if bloco and bloco != "GERAL":
        consulta = consulta.filter(Unidade.bloco == bloco)
    permitidos = _blocos_do_gestor(usuario)
    if usuario.role == Role.SINDICO and permitidos is not None:
        consulta = consulta.filter(Unidade.bloco.in_(permitidos or ["__nenhum__"]))
    escolhidos = []
    for bruto in ids:
        try:
            unidade_id = int(bruto)
        except (TypeError, ValueError):
            continue
        unidade = consulta.filter(Unidade.id == unidade_id).first()
        if unidade is not None:
            escolhidos.append(unidade)
    return escolhidos


def _desejados(usuario, nivel, bloco, form):
    if nivel == NivelMedicao.POR_UNIDADE:
        unidades = _unidades_escolhidas(usuario, bloco, form.getlist("unidade_id"))
        return [
            {
                "unidade_id": unidade.id,
                "identificador": f"{unidade.bloco} / {unidade.apartamento}",
                "serie": None,
                "inicial": 0.0,
            }
            for unidade in unidades
        ]
    if nivel == NivelMedicao.POR_BLOCO:
        if bloco and bloco != "GERAL":
            blocos = [bloco]
        elif usuario.role == Role.ADMIN:
            blocos = list(get_blocos())
        else:
            blocos = list(_blocos_do_gestor(usuario) or [])
        return [
            {
                "unidade_id": None,
                "identificador": f"Bloco {codigo} - Relógio Geral",
                "serie": None,
                "inicial": 0.0,
            }
            for codigo in blocos
        ]
    pontos = []
    for linha in (form.get("pontos") or "").splitlines():
        nome = linha.strip()
        if nome:
            pontos.append(
                {
                    "unidade_id": None,
                    "identificador": nome[:160],
                    "serie": None,
                    "inicial": 0.0,
                }
            )
    return pontos


def _sincronizar_participantes(medidor, desejados):
    atuais = medidor.participantes.all()
    por_unidade = {item.unidade_id: item for item in atuais if item.unidade_id}
    por_nome = {item.identificador_ponto: item for item in atuais if not item.unidade_id}
    mantidos = set()
    for desejado in desejados:
        if desejado["unidade_id"]:
            participante = por_unidade.get(desejado["unidade_id"])
        else:
            participante = por_nome.get(desejado["identificador"])
        if participante is None:
            participante = ParticipanteMedidor(
                medidor_id=medidor.id,
                unidade_id=desejado["unidade_id"],
                identificador_ponto=desejado["identificador"],
                numero_serie_relogio=desejado["serie"],
                leitura_inicial=desejado["inicial"],
                credito_acumulado=0.0,
                ativo=True,
            )
            db.session.add(participante)
        else:
            participante.ativo = True
            participante.identificador_ponto = desejado["identificador"]
        mantidos.add(id(participante))
    for participante in atuais:
        if id(participante) not in mantidos:
            participante.ativo = False


def _ultimo_por_participante(medidor_id, ignorar_ciclo_id=None):
    consulta = ItemLeituraMedidor.query.join(CicloLeituraMedidor).filter(
        CicloLeituraMedidor.medidor_id == medidor_id,
        ItemLeituraMedidor.leitura_atual.isnot(None),
    )
    if ignorar_ciclo_id:
        consulta = consulta.filter(CicloLeituraMedidor.id != ignorar_ciclo_id)
    historico = consulta.order_by(
        CicloLeituraMedidor.data_leitura.desc(), ItemLeituraMedidor.id.desc()
    ).all()
    ultimo = {}
    for item in historico:
        ultimo.setdefault(item.participante_id, item)
    return ultimo


def _abrir_itens(ciclo, medidor):
    ultimo = _ultimo_por_participante(medidor.id)
    for participante in medidor.participantes.filter_by(ativo=True).all():
        anterior = ultimo.get(participante.id)
        marco = float(anterior.leitura_atual) if anterior is not None else float(participante.leitura_inicial or 0)
        db.session.add(
            ItemLeituraMedidor(
                ciclo_id=ciclo.id,
                participante_id=participante.id,
                unidade_id=participante.unidade_id,
                leitura_anterior=marco,
                leitura_atual=None,
                reiniciada=False,
            )
        )


def _itens_do_ciclo(ciclo):
    return (
        ItemLeituraMedidor.query.filter_by(ciclo_id=ciclo.id)
        .join(ParticipanteMedidor, ParticipanteMedidor.id == ItemLeituraMedidor.participante_id)
        .outerjoin(Unidade, Unidade.id == ItemLeituraMedidor.unidade_id)
        .order_by(
            Unidade.bloco.asc(),
            Unidade.apartamento.asc(),
            ParticipanteMedidor.identificador_ponto.asc(),
        )
        .all()
    )


def _recalcular_persistido(medidor, ciclo):
    itens = _itens_do_ciclo(ciclo)
    ultimo = _ultimo_por_participante(medidor.id, ignorar_ciclo_id=ciclo.id)
    linhas = []
    for item in itens:
        if item.lancamento_gerado:
            continue
        anterior_hist = ultimo.get(item.participante_id)
        linhas.append(
            {
                "item": item,
                "leitura_anterior": item.leitura_anterior,
                "leitura_atual": item.leitura_atual,
                "reiniciada": item.reiniciada,
                "credito": float(item.participante.credito_acumulado or 0),
                "consumo_periodo_anterior": (
                    float(anterior_hist.consumo_final or 0) if anterior_hist is not None else 0
                ),
            }
        )
    calculadas = calcular_linhas(medidor, linhas, ciclo.valor_fatura_concessionaria)
    for calculada in calculadas:
        item = calculada["item"]
        item.consumo_apurado = calculada["consumo"]
        item.consumo_final = calculada["consumo"]
        item.credito_abatido = calculada["credito_abatido"]
        item.valor_calculado = calculada["valor"]
        item.alerta_anomalia = calculada["alerta"]
        if ultimo.get(item.participante_id) is None:
            item.participante.leitura_inicial = float(item.leitura_anterior or 0)
    consumo = 0.0
    valor = 0.0
    for item in itens:
        if item.leitura_atual is not None:
            consumo += float(item.consumo_final or 0)
            valor += float(item.valor_calculado or 0)
    ciclo.consumo_total = round(consumo, 3)
    ciclo.valor_total_apurado = round(valor, 2)


def _salvar_foto(arquivo):
    if arquivo is None or not arquivo.filename:
        return None
    nome = secure_filename(arquivo.filename)
    extensao = nome.rsplit(".", 1)[-1].lower() if "." in nome else ""
    if extensao not in _EXTENSOES_FOTO:
        raise ValueError("A foto do relógio precisa ser JPG, PNG ou WebP.")
    arquivo.seek(0, 2)
    tamanho = arquivo.tell()
    arquivo.seek(0)
    if tamanho > 5 * 1024 * 1024:
        raise ValueError("A foto do relógio pode ter no máximo 5 MB.")
    pasta = current_app.config["UPLOAD_MEDIDORES_FOLDER"]
    gerado = f"{uuid.uuid4().hex}.{extensao}"
    arquivo.save(f"{pasta}/{gerado}")
    return gerado


def _descricao_consumo(medidor, item):
    rotulos = {
        TipoRecursoMedidor.AGUA: "ÁGUA",
        TipoRecursoMedidor.GAS: "GÁS",
        TipoRecursoMedidor.ENERGIA: "ENERGIA",
    }
    recurso = rotulos.get(medidor.tipo_recurso, "CONSUMO")
    quantidade = f"{float(item.consumo_final or 0):.3f}".rstrip("0").rstrip(".").replace(".", ",")
    if medidor.nivel_medicao == NivelMedicao.POR_UNIDADE:
        return f"{recurso} INDIVIDUALIZADA - {quantidade} {medidor.unidade_medida}"[:120]
    return f"{recurso} - {quantidade} {medidor.unidade_medida}"[:120]


def _linha_composicao(medidor, item):
    fundo_nome = ""
    if medidor.plano_conta_id and medidor.plano_conta and medidor.plano_conta.fundo:
        fundo_nome = medidor.plano_conta.fundo.nome
    return {
        "plano_conta_id": medidor.plano_conta_id,
        "descricao": _descricao_consumo(medidor, item),
        "fundo_nome": fundo_nome,
        "valor": round(float(item.valor_calculado or 0), 2),
        "parcela_atual": 1,
        "total_parcelas": 1,
        "medidor_item_id": item.id,
    }


def _abater_credito(item):
    abatido = round(float(item.credito_abatido or 0), 2)
    saldo = round(float(item.participante.credito_acumulado or 0), 2)
    item.participante.credito_acumulado = round(max(0.0, saldo - abatido), 2)


def _criar_cobranca(medidor, ciclo, item, conta):
    from app.blueprints.financeiro import (
        _hoje,
        _pagador_da_unidade,
        _reservar_nossos_numeros,
    )
    from app.models import CobrancaUnidade, StatusCobranca
    from app.planta_unidades import nome_pagador_publico

    unidade = item.unidade
    nome, documento, email, telefone = _pagador_da_unidade(unidade)
    if not (nome or "").strip():
        nome = nome_pagador_publico(unidade)
    vencimento = ciclo.data_leitura
    hoje = _hoje()
    status = StatusCobranca.A_VENCER if vencimento >= hoje else StatusCobranca.VENCIDA
    cobranca = CobrancaUnidade(
        condominio_id=medidor.condominio_id,
        unidade_id=unidade.id,
        conta_bancaria_id=conta.id,
        competencia=ciclo.competencia,
        titulo=medidor.titulo[:200],
        nosso_numero=_reservar_nossos_numeros(medidor.condominio_id, 1)[0],
        vencimento=vencimento,
        pagador_nome=nome,
        pagador_documento=documento,
        pagador_email=email,
        pagador_telefone=telefone,
        composicao_json=[_linha_composicao(medidor, item)],
        valor_original=round(float(item.valor_calculado or 0), 2),
        status=status,
        remessa_gerada=False,
    )
    db.session.add(cobranca)
    db.session.flush()
    return cobranca


def _incluir_na_cobranca(cobranca, medidor, item):
    from app.blueprints.financeiro import _como_lista

    linhas = list(_como_lista(cobranca.composicao_json))
    if any(linha.get("medidor_item_id") == item.id for linha in linhas if isinstance(linha, dict)):
        return cobranca
    linhas.append(_linha_composicao(medidor, item))
    cobranca.composicao_json = linhas
    cobranca.valor_original = round(sum(float(linha.get("valor") or 0) for linha in linhas), 2)
    return cobranca


def _boleto_aberto(medidor, item, ciclo):
    from app.models import CobrancaUnidade, StatusCobranca

    return (
        CobrancaUnidade.query.filter(
            CobrancaUnidade.condominio_id == medidor.condominio_id,
            CobrancaUnidade.unidade_id == item.unidade_id,
            CobrancaUnidade.competencia == ciclo.competencia,
            CobrancaUnidade.status == StatusCobranca.A_VENCER,
            CobrancaUnidade.remessa_gerada.is_(False),
        )
        .order_by(CobrancaUnidade.id.asc())
        .first()
    )


def _impacto_cobranca(medidor, ciclo):
    if medidor is None or ciclo is None or medidor.embutido_taxa_ordinaria:
        return None
    if medidor.nivel_medicao != NivelMedicao.POR_UNIDADE:
        return None
    itens = _itens_cobraveis(ciclo)
    com_boleto = 0
    novas = 0
    total = 0.0
    for item in itens:
        total += float(item.valor_calculado or 0)
        if _boleto_aberto(medidor, item, ciclo):
            com_boleto += 1
        else:
            novas += 1
    return {
        "qtd": len(itens),
        "total": round(total, 2),
        "com_boleto": com_boleto,
        "novas": novas,
    }


def _itens_cobraveis(ciclo):
    return [
        item
        for item in _itens_do_ciclo(ciclo)
        if not item.lancamento_gerado
        and item.leitura_atual is not None
        and item.unidade_id
        and float(item.valor_calculado or 0) > 0
    ]


def _plano_obrigatorio(medidor):
    if not medidor.plano_conta_id:
        return "Escolha o plano de contas deste medidor antes de lançar a cobrança."
    plano = PlanoConta.query.filter_by(
        id=medidor.plano_conta_id, condominio_id=medidor.condominio_id
    ).first()
    if plano is None:
        return "O plano de contas deste medidor não pertence ao condomínio."
    return None


def painel_medidores():
    usuario, negado = _exigir_gestor()
    if negado is not None:
        return negado
    medidores = _medidores_visiveis(usuario).all()
    medidor_id = request.args.get("medidor", type=int)
    medidor = next((item for item in medidores if item.id == medidor_id), None)
    if medidor is None and medidores:
        medidor = medidores[0]
    ciclos = []
    ciclo = None
    itens = []
    if medidor is not None:
        ciclos = (
            CicloLeituraMedidor.query.filter_by(medidor_id=medidor.id)
            .order_by(CicloLeituraMedidor.data_leitura.desc(), CicloLeituraMedidor.id.desc())
            .all()
        )
        ciclo_id = request.args.get("ciclo", type=int)
        ciclo = next((item for item in ciclos if item.id == ciclo_id), None)
        if ciclo is None and ciclos:
            ciclo = next((item for item in ciclos if item.status == StatusCicloMedidor.ABERTO), ciclos[0])
        if ciclo is not None:
            itens = _itens_do_ciclo(ciclo)
    participantes_ativos = set()
    faixas = []
    if medidor is not None:
        participantes_ativos = {
            item.unidade_id
            for item in medidor.participantes.filter_by(ativo=True).all()
            if item.unidade_id
        }
        from app.financeiro_medidores import faixas_do_medidor

        faixas = faixas_do_medidor(medidor)
    blocos = get_blocos()
    permitidos = _blocos_do_gestor(usuario)
    if usuario.role == Role.SINDICO and permitidos is not None:
        blocos = [bloco for bloco in blocos if bloco in permitidos]
    unidades = Unidade.query.filter(
        Unidade.condominio_id == usuario.condominio_id,
        Unidade.eh_setor_interno.is_(False),
        Unidade.criada_pela_admin.is_(True),
    )
    if usuario.role == Role.SINDICO and permitidos is not None:
        unidades = unidades.filter(Unidade.bloco.in_(permitidos or ["__nenhum__"]))
    unidades = unidades.order_by(Unidade.bloco.asc(), Unidade.apartamento.asc()).all()
    lidas = sum(1 for item in itens if item.leitura_atual is not None)
    pontos_texto = ""
    if medidor is not None and medidor.nivel_medicao == NivelMedicao.ADM_SETOR:
        pontos_texto = "\n".join(
            item.identificador_ponto
            for item in medidor.participantes.filter_by(ativo=True).order_by(ParticipanteMedidor.id.asc())
        )
    return render_template(
        "admin/financeiro/medidores.html",
        medidores=medidores,
        medidor=medidor,
        ciclos=ciclos,
        ciclo=ciclo,
        itens=itens,
        lidas=lidas,
        pendentes=len(itens) - lidas,
        blocos=blocos,
        unidades=unidades,
        participantes_ativos=participantes_ativos,
        faixas=faixas,
        pontos_texto=pontos_texto,
        planos=PlanoConta.query.filter_by(condominio_id=usuario.condominio_id).order_by(PlanoConta.nome.asc()).all(),
        fundos=FundoFinanceiro.query.filter_by(condominio_id=usuario.condominio_id).order_by(FundoFinanceiro.nome.asc()).all(),
        eh_admin=usuario.role == Role.ADMIN,
        impacto=_impacto_cobranca(medidor, ciclo),
        endpoint_painel=(
            "sindico_medidores" if usuario.role == Role.SINDICO else "admin_financeiro_medidores"
        ),
    )


def salvar_medidor():
    usuario, negado = _exigir_gestor()
    if negado is not None:
        return negado
    titulo = (request.form.get("titulo") or "").strip()
    tipo = (request.form.get("tipo_recurso") or "").strip()
    medida = (request.form.get("unidade_medida") or "").strip()
    nivel = (request.form.get("nivel_medicao") or "").strip()
    bloco = (request.form.get("bloco_vinculado") or "GERAL").strip()
    modo = (request.form.get("modo_calculo") or "").strip()
    if not titulo or tipo not in _TIPOS or medida not in _MEDIDAS or nivel not in _NIVEIS or modo not in _MODOS:
        flash("Preencha título, recurso, medida, nível e modo de cálculo.", "danger")
        return _destino(usuario)
    if bloco != "GERAL" and bloco not in get_blocos():
        flash("Escolha um bloco válido.", "danger")
        return _destino(usuario)
    if usuario.role == Role.SINDICO and (nivel == NivelMedicao.ADM_SETOR or bloco == "GERAL"):
        flash("O síndico cadastra medidores vinculados a um bloco do seu mandato.", "warning")
        return _destino(usuario)
    permitidos = _blocos_do_gestor(usuario)
    if usuario.role == Role.SINDICO and permitidos is not None and bloco not in permitidos:
        flash("Esse bloco está fora do seu mandato.", "warning")
        return _destino(usuario)
    try:
        tarifa = _numero(request.form.get("tarifa_unitaria") or "0", 4) or 0.0
        taxa = _numero(request.form.get("taxa_fixa_minima") or "0", 2) or 0.0
        faixas = _faixas_do_form(request.form)
        desejados = _desejados(usuario, nivel, bloco, request.form)
    except ValueError as erro:
        flash(str(erro), "danger")
        return _destino(usuario)
    if not desejados:
        flash("Inclua ao menos uma unidade ou ponto de leitura.", "warning")
        return _destino(usuario)
    plano_id = request.form.get("plano_conta_id", type=int)
    fundo_id = request.form.get("fundo_id", type=int)
    if plano_id and PlanoConta.query.filter_by(id=plano_id, condominio_id=usuario.condominio_id).first() is None:
        plano_id = None
    if fundo_id and FundoFinanceiro.query.filter_by(id=fundo_id, condominio_id=usuario.condominio_id).first() is None:
        fundo_id = None
    medidor_id = request.form.get("medidor_id", type=int)
    medidor = None
    if medidor_id:
        medidor = db.session.get(MedidorConfig, medidor_id)
        if not _pode_medidor(usuario, medidor):
            abort(404)
    if medidor is None:
        medidor = MedidorConfig(condominio_id=usuario.condominio_id, criado_em=datetime.utcnow())
        db.session.add(medidor)
    medidor.titulo = titulo[:160]
    medidor.tipo_recurso = tipo
    medidor.unidade_medida = medida
    medidor.nivel_medicao = nivel
    medidor.bloco_vinculado = bloco or "GERAL"
    medidor.modo_calculo = modo
    medidor.tarifa_unitaria = tarifa
    medidor.taxa_fixa_minima = taxa
    medidor.faixas_json = json.dumps(faixas) if faixas else None
    medidor.plano_conta_id = plano_id
    medidor.fundo_id = fundo_id
    medidor.permitir_leitura_morador = _marcado("permitir_leitura_morador")
    medidor.embutido_taxa_ordinaria = _marcado("embutido_taxa_ordinaria")
    medidor.ativo = _marcado("ativo")
    db.session.flush()
    _sincronizar_participantes(medidor, desejados)
    _auditar(f"Medidor #{medidor.id} atualizado.")
    db.session.commit()
    flash("Medidor salvo.", "success")
    return _destino(usuario, medidor.id)


def nova_leitura():
    usuario, negado = _exigir_gestor()
    if negado is not None:
        return negado
    from app.blueprints.financeiro import _parse_data

    medidor = db.session.get(MedidorConfig, request.form.get("medidor_id", type=int))
    if not _pode_medidor(usuario, medidor):
        abort(404)
    competencia = _competencia_valida(request.form.get("competencia"))
    data_leitura = _parse_data(request.form.get("data_leitura"))
    if competencia is None or data_leitura is None:
        flash("Informe a competência (MM/AAAA) e a data da leitura.", "danger")
        return _destino(usuario, medidor.id)
    try:
        fatura = _numero(request.form.get("valor_fatura") or "0", 2) or 0.0
    except ValueError:
        flash("Informe o valor da fatura da concessionária.", "danger")
        return _destino(usuario, medidor.id)
    if not medidor.participantes.filter_by(ativo=True).first():
        flash("Este medidor ainda não tem participante ativo.", "warning")
        return _destino(usuario, medidor.id)
    ciclo = CicloLeituraMedidor(
        condominio_id=usuario.condominio_id,
        medidor_id=medidor.id,
        competencia=competencia,
        data_leitura=data_leitura,
        valor_fatura_concessionaria=fatura,
        status=StatusCicloMedidor.ABERTO,
        observacoes=(request.form.get("observacoes") or "").strip() or None,
        criado_em=datetime.utcnow(),
    )
    db.session.add(ciclo)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        flash("Já existe uma leitura desta competência para este medidor.", "warning")
        return _destino(usuario, medidor.id)
    _abrir_itens(ciclo, medidor)
    _auditar(f"Leitura do medidor #{medidor.id} aberta para {competencia}.")
    db.session.commit()
    flash("Período de leitura aberto. Lance a leitura atual de cada ponto.", "success")
    return _destino(usuario, medidor.id, ciclo.id)


def recalcular_leitura():
    usuario, negado = _exigir_gestor()
    if negado is not None:
        return negado
    ciclo = db.session.get(CicloLeituraMedidor, request.form.get("ciclo_id", type=int))
    if ciclo is None or ciclo.condominio_id != usuario.condominio_id:
        abort(404)
    medidor = ciclo.medidor
    if not _pode_medidor(usuario, medidor):
        abort(404)
    if ciclo.status != StatusCicloMedidor.ABERTO:
        flash("Esta leitura já foi fechada.", "warning")
        return _destino(usuario, medidor.id, ciclo.id)
    if any(item.lancamento_gerado for item in ciclo.itens):
        flash("Há lançamento nesta leitura. Abra outro período para corrigir.", "warning")
        return _destino(usuario, medidor.id, ciclo.id)
    try:
        fatura = _numero(request.form.get("valor_fatura") or str(ciclo.valor_fatura_concessionaria or 0), 2)
        for item in _itens_do_ciclo(ciclo):
            anterior = _numero(request.form.get(f"item_{item.id}_anterior"), 3)
            atual = _numero(request.form.get(f"item_{item.id}_atual"), 3)
            credito = _numero(request.form.get(f"item_{item.id}_credito") or "0", 2)
            if anterior is None:
                raise ValueError("Informe a leitura anterior de todos os pontos.")
            item.leitura_anterior = anterior
            item.leitura_atual = atual
            item.reiniciada = _marcado(f"item_{item.id}_reiniciada")
            item.participante.credito_acumulado = credito or 0.0
            foto = _salvar_foto(request.files.get(f"item_{item.id}_foto"))
            if foto:
                item.foto_relogio = foto
    except ValueError as erro:
        db.session.rollback()
        flash(str(erro), "danger")
        return _destino(usuario, medidor.id, ciclo.id)
    if fatura is not None:
        ciclo.valor_fatura_concessionaria = fatura
    _recalcular_persistido(medidor, ciclo)
    _auditar(f"Leitura do ciclo #{ciclo.id} recalculada.")
    db.session.commit()
    flash("Leitura recalculada.", "success")
    return _destino(usuario, medidor.id, ciclo.id)


def _fechar_se_completo(ciclo):
    itens = list(ciclo.itens)
    if not itens or any(item.leitura_atual is None for item in itens):
        return
    if any(not item.lancamento_gerado and float(item.valor_calculado or 0) > 0 for item in itens):
        return
    if any(item.lancamento_gerado for item in itens):
        ciclo.status = StatusCicloMedidor.COBRADO


def incluir_boleto():
    usuario, negado = _exigir_gestor()
    if negado is not None:
        return negado
    return _lancar_consumo(usuario, avulsa=False)


def gerar_avulsas():
    usuario, negado = _exigir_gestor()
    if negado is not None:
        return negado
    return _lancar_consumo(usuario, avulsa=True)


def _lancar_consumo(usuario, avulsa):
    from app.blueprints.financeiro import _conta_ativa

    ciclo = db.session.get(CicloLeituraMedidor, request.form.get("ciclo_id", type=int))
    if ciclo is None or ciclo.condominio_id != usuario.condominio_id:
        abort(404)
    medidor = ciclo.medidor
    if not _pode_medidor(usuario, medidor):
        abort(404)
    from app.financeiro_fechamento import competencia_esta_fechada, mensagem_competencia_fechada

    if competencia_esta_fechada(usuario.condominio_id, ciclo.competencia):
        flash(mensagem_competencia_fechada(ciclo.competencia), "warning")
        return _destino(usuario, medidor.id, ciclo.id)
    if medidor.embutido_taxa_ordinaria:
        flash(
            "Este custo já está coberto pela taxa ordinária da previsão orçamentária. "
            "A fatura é paga pelo caixa, em Contas a Pagar, sem cobrança extra nos boletos.",
            "warning",
        )
        return _destino(usuario, medidor.id, ciclo.id)
    if medidor.nivel_medicao != NivelMedicao.POR_UNIDADE:
        flash("Este lançamento vale para medidor individual por unidade.", "warning")
        return _destino(usuario, medidor.id, ciclo.id)
    erro = _plano_obrigatorio(medidor)
    if erro:
        flash(erro, "warning")
        return _destino(usuario, medidor.id, ciclo.id)
    conta = _conta_ativa(usuario.condominio_id)
    if conta is None:
        flash("Cadastre uma conta bancária ativa antes de gerar a cobrança.", "danger")
        return _destino(usuario, medidor.id, ciclo.id)
    cobraveis = _itens_cobraveis(ciclo)
    if not cobraveis:
        flash("Não há leitura com valor a cobrar.", "warning")
        return _destino(usuario, medidor.id, ciclo.id)
    incluidos = 0
    criados = 0
    try:
        for item in cobraveis:
            cobranca = None if avulsa else _boleto_aberto(medidor, item, ciclo)
            if cobranca is None:
                cobranca = _criar_cobranca(medidor, ciclo, item, conta)
                criados += 1
            else:
                _incluir_na_cobranca(cobranca, medidor, item)
                incluidos += 1
            item.lancamento_gerado = True
            item.cobranca_id = cobranca.id
            _abater_credito(item)
        _fechar_se_completo(ciclo)
        _auditar(f"Consumo do ciclo #{ciclo.id} lançado em cobranças.")
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Não foi possível reservar o nosso número. Tente novamente.", "danger")
        return _destino(usuario, medidor.id, ciclo.id)
    if avulsa:
        flash(f"{criados} cobrança(s) avulsa(s) de consumo gerada(s).", "success")
    else:
        flash(
            f"{incluidos} boleto(s) da competência atualizado(s) e {criados} cobrança(s) nova(s).",
            "success",
        )
    return _destino(usuario, medidor.id, ciclo.id)


def gerar_despesa():
    usuario, negado = _exigir_gestor()
    if negado is not None:
        return negado
    from app.blueprints.financeiro import _conta_ativa, _hoje
    from app.models import DespesaPagamento, StatusDespesa

    ciclo = db.session.get(CicloLeituraMedidor, request.form.get("ciclo_id", type=int))
    if ciclo is None or ciclo.condominio_id != usuario.condominio_id:
        abort(404)
    medidor = ciclo.medidor
    if not _pode_medidor(usuario, medidor):
        abort(404)
    from app.financeiro_fechamento import competencia_esta_fechada, mensagem_competencia_fechada

    if competencia_esta_fechada(usuario.condominio_id, ciclo.competencia):
        flash(mensagem_competencia_fechada(ciclo.competencia), "warning")
        return _destino(usuario, medidor.id, ciclo.id)
    monitoramento = (
        medidor.modo_calculo == ModoCalculoMedidor.MONITORAMENTO_DESPESA
        and medidor.nivel_medicao != NivelMedicao.POR_UNIDADE
    )
    if not medidor.embutido_taxa_ordinaria and not monitoramento:
        flash(
            "A conta a pagar é para custo já coberto pela taxa ordinária, ou para medidor coletivo ou da administração em modo de despesa.",
            "warning",
        )
        return _destino(usuario, medidor.id, ciclo.id)
    if any(item.lancamento_gerado for item in ciclo.itens):
        flash("Esta leitura já gerou lançamento.", "warning")
        return _destino(usuario, medidor.id, ciclo.id)
    erro = _plano_obrigatorio(medidor)
    if erro or not medidor.fundo_id:
        flash(erro or "Escolha o fundo deste medidor antes de gerar a despesa.", "warning")
        return _destino(usuario, medidor.id, ciclo.id)
    fundo = FundoFinanceiro.query.filter_by(id=medidor.fundo_id, condominio_id=usuario.condominio_id).first()
    conta = _conta_ativa(usuario.condominio_id)
    if fundo is None or conta is None:
        flash("Conta bancária ou fundo indisponível neste condomínio.", "danger")
        return _destino(usuario, medidor.id, ciclo.id)
    valor = float(ciclo.valor_fatura_concessionaria or 0) or float(ciclo.valor_total_apurado or 0)
    valor = round(valor, 2)
    if valor <= 0:
        flash("Informe a fatura da concessionária ou apure um valor maior que zero.", "warning")
        return _destino(usuario, medidor.id, ciclo.id)
    vencimento = ciclo.data_leitura
    despesa = DespesaPagamento(
        condominio_id=usuario.condominio_id,
        fornecedor_nome=medidor.titulo[:200],
        conta_bancaria_id=conta.id,
        plano_conta_id=medidor.plano_conta_id,
        fundo_id=fundo.id,
        titulo=f"{medidor.titulo} {ciclo.competencia}"[:200],
        competencia=ciclo.competencia,
        vencimento=vencimento,
        operacao="Boleto - Título",
        bloco_alocado=medidor.bloco_vinculado if medidor.bloco_vinculado != "GERAL" else "GERAL",
        valor_original=valor,
        status=StatusDespesa.A_VENCER if vencimento >= _hoje() else StatusDespesa.VENCIDO,
        observacoes=f"Medidor #{medidor.id}, ciclo #{ciclo.id}.",
        criado_em=datetime.utcnow(),
    )
    db.session.add(despesa)
    for item in ciclo.itens:
        if item.leitura_atual is not None:
            item.lancamento_gerado = True
    ciclo.status = StatusCicloMedidor.COBRADO
    _auditar(f"Despesa do medidor #{medidor.id} gerada no ciclo #{ciclo.id}.")
    db.session.commit()
    flash(
        "Fatura lançada em Pagamentos / Despesas. O valor entra no Orçado vs. Realizado desta competência.",
        "success",
    )
    return _destino(usuario, medidor.id, ciclo.id)


def foto_medidor(item_id):
    usuario, negado = _exigir_gestor()
    if negado is not None:
        return negado
    item = db.session.get(ItemLeituraMedidor, item_id)
    if item is None or not item.foto_relogio or item.ciclo.condominio_id != usuario.condominio_id:
        abort(404)
    if not _pode_medidor(usuario, item.ciclo.medidor):
        abort(404)
    return send_from_directory(current_app.config["UPLOAD_MEDIDORES_FOLDER"], item.foto_relogio)


def imprimir_medidores():
    usuario, negado = _exigir_gestor()
    if negado is not None:
        return negado
    ciclo = db.session.get(CicloLeituraMedidor, request.args.get("ciclo", type=int))
    if ciclo is None or ciclo.condominio_id != usuario.condominio_id:
        abort(404)
    if not _pode_medidor(usuario, ciclo.medidor):
        abort(404)
    return render_template(
        "admin/financeiro/medidores_impressao.html",
        medidor=ciclo.medidor,
        ciclo=ciclo,
        itens=_itens_do_ciclo(ciclo),
    )


@unidade_required
def morador_medidores(unidade):
    abertos = _leituras_do_morador(unidade)
    return render_template("morador/medidores.html", unidade=unidade, abertos=abertos)


@unidade_required
def morador_medidores_enviar(unidade):
    item = db.session.get(ItemLeituraMedidor, request.form.get("item_id", type=int))
    if (
        item is None
        or item.unidade_id != unidade.id
        or item.ciclo.condominio_id != unidade.condominio_id
        or not item.ciclo.medidor.ativo
        or not item.ciclo.medidor.permitir_leitura_morador
        or item.ciclo.status != StatusCicloMedidor.ABERTO
        or item.lancamento_gerado
    ):
        flash("Esta leitura não está aberta para a sua unidade.", "warning")
        return redirect(url_for("morador_medidores"))
    try:
        atual = _numero(request.form.get("leitura_atual"), 3)
        if atual is None:
            raise ValueError("Informe a leitura atual do relógio.")
        item.leitura_atual = atual
        item.reiniciada = _marcado("reiniciada")
        foto = _salvar_foto(request.files.get("foto_relogio"))
    except ValueError as erro:
        db.session.rollback()
        flash(str(erro), "danger")
        return redirect(url_for("morador_medidores"))
    if foto:
        item.foto_relogio = foto
    item.enviado_pelo_morador = True
    item.data_envio_morador = datetime.utcnow()
    _recalcular_persistido(item.ciclo.medidor, item.ciclo)
    db.session.commit()
    flash("Leitura enviada. A administração confere o consumo antes de cobrar.", "success")
    return redirect(url_for("morador_medidores"))


@unidade_required
def morador_medidores_foto(unidade, item_id):
    item = db.session.get(ItemLeituraMedidor, item_id)
    if item is None or item.unidade_id != unidade.id or not item.foto_relogio:
        abort(404)
    return send_from_directory(current_app.config["UPLOAD_MEDIDORES_FOLDER"], item.foto_relogio)


def _leituras_do_morador(unidade):
    return (
        ItemLeituraMedidor.query.join(CicloLeituraMedidor)
        .join(MedidorConfig, MedidorConfig.id == CicloLeituraMedidor.medidor_id)
        .filter(
            ItemLeituraMedidor.unidade_id == unidade.id,
            CicloLeituraMedidor.condominio_id == unidade.condominio_id,
            CicloLeituraMedidor.status == StatusCicloMedidor.ABERTO,
            MedidorConfig.ativo.is_(True),
            MedidorConfig.permitir_leitura_morador.is_(True),
            ItemLeituraMedidor.lancamento_gerado.is_(False),
        )
        .order_by(CicloLeituraMedidor.id.desc())
        .all()
    )


def medidores_abertos_unidade(unidade):
    if unidade is None:
        return 0
    try:
        return _leituras_do_morador(unidade).count()
    except Exception:
        db.session.rollback()
        return 0


def register(app):
    app.add_url_rule(
        "/admin/financeiro/medidores",
        "admin_financeiro_medidores",
        painel_medidores,
        methods=["GET"],
    )
    app.add_url_rule(
        "/sindico/medidores",
        "sindico_medidores",
        painel_medidores,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/financeiro/medidores/salvar",
        "admin_financeiro_medidor_salvar",
        salvar_medidor,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/medidores/ciclo",
        "admin_financeiro_medidor_ciclo",
        nova_leitura,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/medidores/recalcular",
        "admin_financeiro_medidor_recalcular",
        recalcular_leitura,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/medidores/boleto",
        "admin_financeiro_medidor_boleto",
        incluir_boleto,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/medidores/avulsas",
        "admin_financeiro_medidor_avulsas",
        gerar_avulsas,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/medidores/despesa",
        "admin_financeiro_medidor_despesa",
        gerar_despesa,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/medidores/foto/<int:item_id>",
        "admin_financeiro_medidor_foto",
        foto_medidor,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/financeiro/medidores/imprimir",
        "admin_financeiro_medidor_imprimir",
        imprimir_medidores,
        methods=["GET"],
    )
    app.add_url_rule("/medidores", "morador_medidores", morador_medidores, methods=["GET"])
    app.add_url_rule(
        "/medidores/enviar",
        "morador_medidores_enviar",
        morador_medidores_enviar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/medidores/foto/<int:item_id>",
        "morador_medidores_foto",
        morador_medidores_foto,
        methods=["GET"],
    )
