"""Reservas de áreas comuns e cobrança da taxa de uso na aprovação.

Não altera o módulo operacional de `EspacoComum` / `Reserva` (`/reservas`).
A inadimplência da taxa condominial não impede o pedido (STJ).
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from app import db
from app.auth import admin_or_sindico_required, get_current_user, unidade_required
from app.models import (
    AreaComum,
    CobrancaUnidade,
    ConvidadoReserva,
    EscopoRepasse,
    FundoFinanceiro,
    PlanoConta,
    ReservaArea,
    Role,
    StatusCobranca,
    StatusReservaArea,
    TipoPlanoConta,
    Unidade,
)
from app.utils import get_blocos, html_rico_form, normalizar_bloco_codigo

_FUSO = ZoneInfo("America/Sao_Paulo")
_GERAL = "GERAL"
_OCUPADAS = (
    StatusReservaArea.PENDENTE,
    StatusReservaArea.AGUARDANDO_PAGAMENTO,
    StatusReservaArea.APROVADA,
)
_CODIGOS_PLANO = ("1.8.9", "1.8.8", "1.8.7", "1.8.6")
_STATUS_UNIDADE = ("Aprovada", "Registrada")
_ABA_PENDENTES = "pendentes"
_ABA_CALENDARIO = "calendario"
_ABA_ESPACOS = "espacos"
_ABAS_GESTAO = (_ABA_PENDENTES, _ABA_CALENDARIO, _ABA_ESPACOS)
_ALIAS_ABA = {"areas": _ABA_ESPACOS, "solicitacoes": _ABA_PENDENTES}


def _agora():
    return datetime.now(_FUSO).replace(tzinfo=None)


def _hoje():
    return _agora().date()


def _parse_data(texto):
    bruto = (texto or "").strip()
    for formato in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(bruto, formato).date()
        except ValueError:
            continue
    return None


def _parse_hora(texto):
    bruto = (texto or "").strip()
    for formato in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(bruto, formato).time()
        except ValueError:
            continue
    return None


def _parse_valor(texto):
    from app.blueprints.financeiro import _parse_valor as parse_financeiro

    return parse_financeiro(texto)


def _marcado(nome):
    return "1" in request.form.getlist(nome)


def _endpoint_lista():
    usuario = get_current_user()
    if usuario and usuario.role == Role.SINDICO:
        return "sindico_areas"
    return "admin_areas"


def _redirecionar_lista(**extras):
    return redirect(url_for(_endpoint_lista(), **extras))


def _resolver_aba(pedida):
    aba = _ALIAS_ABA.get(pedida or "", pedida)
    if aba in _ABAS_GESTAO:
        return aba
    return _ABA_PENDENTES


_FILTROS_HISTORICO = ("pendentes", "aprovadas", "recusadas", "todas")


def _resolver_filtro(pedido):
    if pedido in _FILTROS_HISTORICO:
        return pedido
    return "pendentes"


def _reservas_do_filtro(reservas, filtro):
    if filtro == "todas":
        escolhidas = list(reservas)
    elif filtro == "aprovadas":
        escolhidas = [
            item
            for item in reservas
            if item.status
            in (
                StatusReservaArea.APROVADA,
                StatusReservaArea.AGUARDANDO_PAGAMENTO,
            )
        ]
    elif filtro == "recusadas":
        escolhidas = [
            item
            for item in reservas
            if item.status
            in (StatusReservaArea.REJEITADA, StatusReservaArea.CANCELADA)
        ]
    else:
        escolhidas = [
            item for item in reservas if item.status == StatusReservaArea.PENDENTE
        ]
    reverso = filtro != "pendentes"
    return sorted(escolhidas, key=lambda item: (item.data_evento, item.id), reverse=reverso)


_COR_EVENTO = {
    StatusReservaArea.PENDENTE: "#f0ad4e",
    StatusReservaArea.AGUARDANDO_PAGAMENTO: "#0dcaf0",
    StatusReservaArea.APROVADA: "#198754",
    StatusReservaArea.REJEITADA: "#6c757d",
    StatusReservaArea.CANCELADA: "#6c757d",
}


def _pedido_fetch():
    return request.headers.get("X-Requested-With") == "fetch"


def _contar_pendentes(usuario):
    ids = [area.id for area in _areas_do_ator(usuario, usuario.condominio_id)]
    if not ids:
        return 0
    return ReservaArea.query.filter(
        ReservaArea.area_id.in_(ids),
        ReservaArea.status == StatusReservaArea.PENDENTE,
    ).count()


def _texto_financeiro_area(area):
    valor = round(float(area.taxa_uso_valor or 0), 2)
    if valor <= 0:
        return "Sem taxa de uso"
    if area.pagamento_antecipado_obrigatorio:
        return "Cobrança antecipada: o boleto confirma a reserva"
    return "Cobrança será embutida na próxima taxa"


def _valor_area(area):
    valor = round(float(area.taxa_uso_valor or 0), 2)
    if valor <= 0:
        return "Gratuita"
    return "R$ " + f"{valor:.2f}".replace(".", ",")


def _resposta_decisao(usuario, mensagem, categoria, *, ok, status=None, remover=False, reserva_id=None):
    if _pedido_fetch():
        corpo = {"ok": ok, "mensagem": mensagem}
        if ok:
            corpo["status"] = status
            corpo["cor"] = _COR_EVENTO.get(status)
            corpo["remover"] = remover
            corpo["pendentes"] = _contar_pendentes(usuario)
        return jsonify(corpo), 200 if ok else 400
    flash(mensagem, categoria)
    extras = {"aba": _ABA_PENDENTES}
    if reserva_id is not None:
        extras["foco"] = reserva_id
    return _redirecionar_lista(**extras)


def _blocos_escolha(usuario):
    """Blocos que o ator pode vincular a uma área. Admin inclui a área geral."""
    if usuario.role == Role.ADMIN:
        return [_GERAL] + list(get_blocos())
    from app.routes import _blocos_codigo_sindico

    blocos = list(_blocos_codigo_sindico(usuario))
    if usuario.get_blocos_permitidos() is None:
        return [_GERAL] + blocos
    return blocos


def _normalizar_bloco_area(texto, permitidos):
    bruto = (texto or "").strip()
    if bruto.upper() in ("", _GERAL, "GERAL"):
        codigo = _GERAL
    else:
        codigo = normalizar_bloco_codigo(bruto)
    if codigo not in permitidos:
        return None
    return codigo


def _areas_do_ator(usuario, condominio_id):
    consulta = AreaComum.query.filter_by(condominio_id=condominio_id)
    if usuario.role != Role.SINDICO:
        return consulta.order_by(AreaComum.nome.asc())
    if usuario.get_blocos_permitidos() is None:
        return consulta.order_by(AreaComum.nome.asc())
    from app.routes import _blocos_codigo_sindico

    blocos = _blocos_codigo_sindico(usuario)
    if not blocos:
        return consulta.filter(AreaComum.id == 0)
    return consulta.filter(AreaComum.bloco_vinculado.in_(blocos)).order_by(AreaComum.nome.asc())


def _area_do_ator(area_id, usuario, condominio_id):
    return _areas_do_ator(usuario, condominio_id).filter(AreaComum.id == area_id).first()


def _reserva_do_ator(reserva_id, usuario, condominio_id):
    reserva = ReservaArea.query.filter_by(id=reserva_id).first()
    if reserva is None or reserva.area is None:
        return None
    if reserva.area.condominio_id != condominio_id:
        return None
    if _area_do_ator(reserva.area_id, usuario, condominio_id) is None:
        return None
    return reserva


def _area_do_morador(unidade, area_id):
    if unidade is None or unidade.eh_setor_interno:
        return None
    if unidade.status not in _STATUS_UNIDADE:
        return None
    area = AreaComum.query.filter_by(
        id=area_id, condominio_id=unidade.condominio_id, ativa=True
    ).first()
    if area is None:
        return None
    if area.bloco_vinculado not in (_GERAL, unidade.bloco):
        return None
    return area


def _conflito(area_id, data_evento, ignorar_id=None, status=None):
    consulta = ReservaArea.query.filter(
        ReservaArea.area_id == area_id,
        ReservaArea.data_evento == data_evento,
        ReservaArea.status.in_(status or _OCUPADAS),
    )
    if ignorar_id:
        consulta = consulta.filter(ReservaArea.id != ignorar_id)
    return consulta.first() is not None


def _datas_ocupadas(area_id):
    linhas = (
        db.session.query(ReservaArea.data_evento)
        .filter(
            ReservaArea.area_id == area_id,
            ReservaArea.status.in_(_OCUPADAS),
        )
        .all()
    )
    return sorted({item[0].isoformat() for item in linhas if item[0]})


def _notificar_morador(reserva, titulo, mensagem):
    from app.models import PerfilDestinoNotificacao
    from app.routes import _criar_notificacao

    _criar_notificacao(
        reserva.area.condominio_id,
        PerfilDestinoNotificacao.MORADOR,
        titulo,
        mensagem,
        unidade_id=reserva.unidade_id,
        link_destino=f"/morador/reservas?foco={reserva.id}",
        tipo="RESERVA_AREA",
    )


def _notificar_gestao(reserva, titulo, mensagem):
    from app.models import PerfilDestinoNotificacao, Usuario
    from app.routes import _criar_notificacao, _sindico_gerencia_bloco

    _criar_notificacao(
        reserva.area.condominio_id,
        PerfilDestinoNotificacao.ADMIN,
        titulo,
        mensagem,
        link_destino=f"/admin/reservas?aba=pendentes&foco={reserva.id}",
        tipo="RESERVA_AREA",
    )
    bloco = reserva.area.bloco_vinculado
    if not bloco or bloco == _GERAL:
        return
    sindicos = Usuario.query.filter_by(
        condominio_id=reserva.area.condominio_id,
        role=Role.SINDICO,
    ).all()
    if not any(_sindico_gerencia_bloco(sindico, bloco) for sindico in sindicos):
        return
    _criar_notificacao(
        reserva.area.condominio_id,
        PerfilDestinoNotificacao.SINDICO,
        titulo,
        mensagem,
        unidade_id=reserva.unidade_id,
        link_destino=f"/sindico/reservas?aba=pendentes&foco={reserva.id}",
        tipo="RESERVA_AREA",
    )


def _texto_aprovada(reserva):
    data = reserva.data_evento.strftime("%d/%m")
    return f"🎉 Sua reserva para o {reserva.area.nome} dia {data} foi aprovada!"


def _plano_taxas(condominio_id):
    plano = PlanoConta.query.filter_by(
        condominio_id=condominio_id,
        nome="Taxas de Áreas Comuns",
        tipo=TipoPlanoConta.RECEITA,
    ).first()
    if plano is not None:
        return plano, None
    fundos = (
        FundoFinanceiro.query.filter_by(condominio_id=condominio_id)
        .order_by(FundoFinanceiro.id.asc())
        .all()
    )
    fundo = next(
        (item for item in fundos if "CAIXA" in (item.nome or "").upper()),
        fundos[0] if fundos else None,
    )
    if fundo is None:
        return None, "Cadastre um fundo financeiro antes de aprovar a taxa de uso."
    codigo = next(
        (
            candidato
            for candidato in _CODIGOS_PLANO
            if PlanoConta.query.filter_by(
                condominio_id=condominio_id, codigo=candidato
            ).first()
            is None
        ),
        None,
    )
    if codigo is None:
        return None, "Não há código livre no plano de contas para a taxa de uso."
    plano = PlanoConta(
        condominio_id=condominio_id,
        codigo=codigo,
        nome="Taxas de Áreas Comuns",
        tipo=TipoPlanoConta.RECEITA,
        fundo_id=fundo.id,
        escopo_repasse=EscopoRepasse.ADM_GERAL,
    )
    db.session.add(plano)
    db.session.flush()
    return plano, None


def _vencimento_taxa(antecipado, hoje):
    if antecipado:
        return hoje + timedelta(days=3)
    ano = hoje.year + (1 if hoje.month == 12 else 0)
    mes = 1 if hoje.month == 12 else hoje.month + 1
    return datetime(ano, mes, 10).date()


def _criar_cobranca_taxa(reserva, valor, vencimento, hoje, antecipado=False):
    from app.blueprints.financeiro import (
        _conta_ativa,
        _pagador_da_unidade,
        _reservar_nossos_numeros,
    )
    from app.financeiro_fechamento import (
        competencia_esta_fechada,
        mensagem_competencia_fechada,
    )

    conta = _conta_ativa(reserva.area.condominio_id)
    if conta is None:
        return None, "Cadastre uma conta bancária ativa antes de aprovar a taxa de uso."
    plano, erro = _plano_taxas(reserva.area.condominio_id)
    if erro:
        return None, erro
    competencia = vencimento.strftime("%Y-%m")
    if competencia_esta_fechada(reserva.area.condominio_id, competencia):
        return None, mensagem_competencia_fechada(competencia)
    unidade = reserva.unidade
    nome, documento, email, telefone = _pagador_da_unidade(unidade)
    if not (nome or "").strip():
        nome = unidade.identificador
    numeros = _reservar_nossos_numeros(reserva.area.condominio_id, 1)
    cobranca = CobrancaUnidade(
        condominio_id=reserva.area.condominio_id,
        unidade_id=unidade.id,
        conta_bancaria_id=conta.id,
        competencia=competencia,
        titulo=f"Taxa de uso — {reserva.area.nome}"[:200],
        nosso_numero=numeros[0],
        vencimento=vencimento,
        pagador_nome=(nome or "")[:200],
        pagador_documento=(documento or "")[:20] or None,
        pagador_email=(email or "")[:120] or None,
        pagador_telefone=(telefone or "")[:20] or None,
        composicao_json=[
            {
                "plano_conta_id": plano.id,
                "descricao": "Taxas de Áreas Comuns",
                "fundo_nome": plano.fundo.nome if plano.fundo else "",
                "valor": valor,
                "parcela_atual": 1,
                "total_parcelas": 1,
            }
        ],
        valor_original=valor,
        status=(
            StatusCobranca.A_VENCER if vencimento >= hoje else StatusCobranca.VENCIDA
        ),
        observacoes=(
            f"Gerada pela reserva de área #{reserva.id}."
            if antecipado
            else f"Embutida na próxima taxa. Gerada pela reserva de área #{reserva.id}."
        ),
    )
    db.session.add(cobranca)
    db.session.flush()
    return cobranca, None


def liberar_reservas_pagas(condominio_id, cobranca_ids):
    """Confirma a reserva quando o CNAB liquida o boleto da taxa. Não faz commit."""
    if not cobranca_ids:
        return
    reservas = ReservaArea.query.filter(
        ReservaArea.cobranca_id.in_(list(cobranca_ids)),
        ReservaArea.status == StatusReservaArea.AGUARDANDO_PAGAMENTO,
    ).all()
    for reserva in reservas:
        if reserva.area is None or reserva.area.condominio_id != condominio_id:
            continue
        reserva.status = StatusReservaArea.APROVADA
        texto = _texto_aprovada(reserva)
        _notificar_morador(reserva, texto, texto)


@admin_or_sindico_required
def areas_lista():
    usuario = get_current_user()
    condominio_id = usuario.condominio_id
    areas = _areas_do_ator(usuario, condominio_id).all()
    ids = [area.id for area in areas]
    reservas = []
    if ids:
        reservas = (
            ReservaArea.query.filter(ReservaArea.area_id.in_(ids))
            .order_by(ReservaArea.data_evento.asc(), ReservaArea.id.desc())
            .all()
        )
    pendentes = [item for item in reservas if item.status == StatusReservaArea.PENDENTE]
    aba = _resolver_aba(request.args.get("aba"))
    filtro = _resolver_filtro(request.args.get("filtro"))
    eh_sindico = usuario.role == Role.SINDICO
    aprovar_endpoint = "sindico_areas_aprovar" if eh_sindico else "admin_areas_aprovar"
    recusar_endpoint = "sindico_areas_rejeitar" if eh_sindico else "admin_areas_rejeitar"
    eventos = [
        {
            "id": item.id,
            "title": f"{item.area.nome} · {item.unidade.identificador}",
            "start": item.data_evento.isoformat(),
            "color": _COR_EVENTO.get(item.status, "#6c757d"),
            "extendedProps": {
                "pendente": item.status == StatusReservaArea.PENDENTE,
                "espaco": item.area.nome,
                "unidade": item.unidade.identificador,
                "data": item.data_evento.strftime("%d/%m/%Y"),
                "horario": (
                    f"{item.horario_inicio.strftime('%H:%M')} às "
                    f"{item.horario_fim.strftime('%H:%M')}"
                ),
                "valor": _valor_area(item.area),
                "financeiro": _texto_financeiro_area(item.area),
                "aprovar": url_for(aprovar_endpoint, reserva_id=item.id),
                "recusar": url_for(recusar_endpoint, reserva_id=item.id),
            },
        }
        for item in reservas
    ]
    return render_template(
        "admin/areas.html",
        areas=areas,
        reservas=reservas,
        eventos=eventos,
        pendentes=pendentes,
        lista=_reservas_do_filtro(reservas, filtro),
        filtro=filtro,
        aba=aba,
        blocos=_blocos_escolha(usuario),
        eh_sindico=eh_sindico,
        foco=request.args.get("foco", type=int),
        status_pendente=StatusReservaArea.PENDENTE,
        status_aguardando=StatusReservaArea.AGUARDANDO_PAGAMENTO,
        status_aprovada=StatusReservaArea.APROVADA,
        status_rejeitada=StatusReservaArea.REJEITADA,
        geral=_GERAL,
    )


@admin_or_sindico_required
def areas_salvar():
    from app.routes import _registrar_auditoria

    usuario = get_current_user()
    permitidos = _blocos_escolha(usuario)
    area_id = request.form.get("area_id", type=int)
    area = None
    if area_id:
        area = _area_do_ator(area_id, usuario, usuario.condominio_id)
        if area is None:
            abort(404)
    nome = (request.form.get("nome") or "").strip()
    if not nome or len(nome) > 150:
        flash("Informe o nome da área, com até 150 caracteres.", "danger")
        return _redirecionar_lista(aba="espacos")
    bloco = _normalizar_bloco_area(request.form.get("bloco_vinculado"), permitidos)
    if bloco is None:
        flash("Escolha um bloco do seu mandato.", "danger")
        return _redirecionar_lista(aba="espacos")
    try:
        capacidade = int(request.form.get("capacidade") or 0)
    except (TypeError, ValueError):
        capacidade = -1
    if capacidade < 0 or capacidade > 9999:
        flash("Informe a capacidade máxima.", "danger")
        return _redirecionar_lista(aba="espacos")
    try:
        taxa = _parse_valor(request.form.get("taxa_uso_valor") or "0")
    except (TypeError, ValueError):
        flash("Informe a taxa de uso.", "danger")
        return _redirecionar_lista(aba="espacos")
    if taxa < 0:
        flash("A taxa de uso não pode ser negativa.", "danger")
        return _redirecionar_lista(aba="espacos")
    regras = html_rico_form("regras_uso")
    if len(regras) > 8000:
        flash("As regras de uso ficaram longas demais.", "danger")
        return _redirecionar_lista(aba="espacos")
    antecipado = _marcado("pagamento_antecipado_obrigatorio")
    if area is None:
        area = AreaComum(
            condominio_id=usuario.condominio_id,
            nome=nome,
            bloco_vinculado=bloco,
            capacidade=capacidade,
            taxa_uso_valor=round(taxa, 2),
            pagamento_antecipado_obrigatorio=antecipado,
            regras_uso=regras or None,
            ativa=True,
        )
        db.session.add(area)
        db.session.flush()
        _registrar_auditoria(usuario, f"Criou a área comum #{area.id}.")
        flash("Área comum cadastrada.", "success")
    else:
        area.nome = nome
        area.bloco_vinculado = bloco
        area.capacidade = capacidade
        area.taxa_uso_valor = round(taxa, 2)
        area.pagamento_antecipado_obrigatorio = antecipado
        area.regras_uso = regras or None
        area.ativa = _marcado("ativa")
        _registrar_auditoria(usuario, f"Atualizou a área comum #{area.id}.")
        flash("Área comum atualizada.", "success")
    db.session.commit()
    return _redirecionar_lista(aba="espacos")


@admin_or_sindico_required
def areas_excluir(area_id):
    from app.routes import _registrar_auditoria

    usuario = get_current_user()
    area = _area_do_ator(area_id, usuario, usuario.condominio_id)
    if area is None:
        abort(404)
    if area.reservas.count():
        flash("Esta área já tem reservas. Desative-a na edição.", "warning")
        return _redirecionar_lista(aba="espacos")
    db.session.delete(area)
    _registrar_auditoria(usuario, f"Excluiu a área comum #{area_id}.")
    db.session.commit()
    flash("Área comum excluída.", "success")
    return _redirecionar_lista(aba="espacos")


@admin_or_sindico_required
def areas_aprovar(reserva_id):
    from app.routes import _registrar_auditoria

    usuario = get_current_user()
    hoje = _hoje()
    reserva = _reserva_do_ator(reserva_id, usuario, usuario.condominio_id)
    if reserva is None:
        abort(404)
    if reserva.status != StatusReservaArea.PENDENTE:
        return _resposta_decisao(
            usuario,
            "Esta solicitação já foi decidida.",
            "warning",
            ok=False,
            reserva_id=reserva.id,
        )
    if _conflito(
        reserva.area_id,
        reserva.data_evento,
        ignorar_id=reserva.id,
        status=(
            StatusReservaArea.AGUARDANDO_PAGAMENTO,
            StatusReservaArea.APROVADA,
        ),
    ):
        return _resposta_decisao(
            usuario,
            "Esta data já foi confirmada para outra unidade.",
            "warning",
            ok=False,
            reserva_id=reserva.id,
        )
    valor = round(float(reserva.area.taxa_uso_valor or 0), 2)
    antecipado = bool(reserva.area.pagamento_antecipado_obrigatorio) and valor > 0
    cobranca = None
    if valor > 0:
        vencimento = _vencimento_taxa(antecipado, hoje)
        try:
            cobranca, erro = _criar_cobranca_taxa(
                reserva, valor, vencimento, hoje, antecipado
            )
        except IntegrityError:
            db.session.rollback()
            return _resposta_decisao(
                usuario,
                "Não foi possível reservar o nosso número. Tente novamente.",
                "danger",
                ok=False,
                reserva_id=reserva.id,
            )
        if erro:
            db.session.rollback()
            return _resposta_decisao(
                usuario, erro, "danger", ok=False, reserva_id=reserva.id
            )
    status_novo = (
        StatusReservaArea.AGUARDANDO_PAGAMENTO
        if antecipado
        else StatusReservaArea.APROVADA
    )
    valores = {ReservaArea.status: status_novo}
    if cobranca is not None:
        valores[ReservaArea.cobranca_id] = cobranca.id
    linhas = ReservaArea.query.filter(
        ReservaArea.id == reserva.id,
        ReservaArea.status == StatusReservaArea.PENDENTE,
        ReservaArea.cobranca_id.is_(None),
    ).update(valores, synchronize_session=False)
    if not linhas:
        db.session.rollback()
        return _resposta_decisao(
            usuario,
            "Esta solicitação já foi decidida.",
            "warning",
            ok=False,
            reserva_id=reserva.id,
        )
    db.session.expire(reserva)
    if status_novo == StatusReservaArea.APROVADA:
        texto = _texto_aprovada(reserva)
        if cobranca is not None:
            quando = cobranca.vencimento.strftime("%d/%m/%Y")
            texto = f"{texto} A taxa foi lançada para {quando}."
        _notificar_morador(reserva, texto[:120], texto)
    else:
        quando = cobranca.vencimento.strftime("%d/%m/%Y")
        texto = (
            f"Sua reserva para o {reserva.area.nome} dia "
            f"{reserva.data_evento.strftime('%d/%m')} aguarda o pagamento da taxa. "
            f"O boleto vence em {quando}."
        )
        _notificar_morador(reserva, texto[:120], texto)
    _registrar_auditoria(usuario, f"Aprovou a reserva de área #{reserva.id}.")
    db.session.commit()
    return _resposta_decisao(
        usuario,
        "Solicitação aprovada.",
        "success",
        ok=True,
        status=status_novo,
        reserva_id=reserva.id,
    )


@admin_or_sindico_required
def areas_rejeitar(reserva_id):
    from app.routes import _registrar_auditoria

    usuario = get_current_user()
    reserva = _reserva_do_ator(reserva_id, usuario, usuario.condominio_id)
    if reserva is None:
        abort(404)
    if reserva.status != StatusReservaArea.PENDENTE:
        return _resposta_decisao(
            usuario,
            "Esta solicitação já foi decidida.",
            "warning",
            ok=False,
            reserva_id=reserva.id,
        )
    motivo = (request.form.get("motivo_rejeicao") or "").strip()
    if not motivo or len(motivo) > 255:
        return _resposta_decisao(
            usuario,
            "Informe o motivo da rejeição.",
            "danger",
            ok=False,
            reserva_id=reserva.id,
        )
    linhas = ReservaArea.query.filter(
        ReservaArea.id == reserva.id,
        ReservaArea.status == StatusReservaArea.PENDENTE,
    ).update(
        {
            ReservaArea.status: StatusReservaArea.REJEITADA,
            ReservaArea.motivo_rejeicao: motivo,
        },
        synchronize_session=False,
    )
    if not linhas:
        db.session.rollback()
        return _resposta_decisao(
            usuario,
            "Esta solicitação já foi decidida.",
            "warning",
            ok=False,
            reserva_id=reserva.id,
        )
    db.session.expire(reserva)
    texto = (
        f"Sua reserva para o {reserva.area.nome} dia "
        f"{reserva.data_evento.strftime('%d/%m')} foi recusada. Motivo: {motivo}"
    )
    _notificar_morador(reserva, texto[:120], texto)
    _registrar_auditoria(usuario, f"Rejeitou a reserva de área #{reserva.id}.")
    db.session.commit()
    return _resposta_decisao(
        usuario,
        "Solicitação recusada.",
        "success",
        ok=True,
        status=StatusReservaArea.REJEITADA,
        remover=True,
        reserva_id=reserva.id,
    )


_SEMANA = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)


def _data_evento_formatada(dia):
    return f"{_SEMANA[dia.weekday()]}, {dia.strftime('%d/%m/%Y')}"


def _proximo_evento(reservas, hoje):
    candidatas = [
        reserva
        for reserva in reservas
        if reserva.status == StatusReservaArea.APROVADA and reserva.data_evento >= hoje
    ]
    if not candidatas:
        return None
    return min(
        candidatas,
        key=lambda reserva: (reserva.data_evento, reserva.horario_inicio, reserva.id),
    )


def _situacao_taxa(reserva):
    """aberta, agendada, paga ou None. A embutida não vira atalho de boleto avulso."""
    cobranca = reserva.cobranca
    if cobranca is None or cobranca.status == StatusCobranca.CANCELADA:
        return None
    if cobranca.status == StatusCobranca.PAGA:
        return "paga"
    observacoes = (cobranca.observacoes or "").casefold()
    embutida = "embutida na próxima taxa" in observacoes
    if not embutida and not reserva.area.pagamento_antecipado_obrigatorio:
        embutida = True
    if embutida:
        return "agendada"
    if cobranca.status in StatusCobranca.ABERTAS:
        return "aberta"
    return None


@unidade_required
def morador_areas(unidade):
    if unidade.eh_setor_interno or unidade.status not in _STATUS_UNIDADE:
        flash("A unidade precisa estar aprovada para reservar uma área.", "warning")
        return redirect(url_for("morador_inicio"))
    areas = (
        AreaComum.query.filter(
            AreaComum.condominio_id == unidade.condominio_id,
            AreaComum.ativa.is_(True),
            AreaComum.bloco_vinculado.in_([_GERAL, unidade.bloco]),
        )
        .order_by(AreaComum.nome.asc())
        .all()
    )
    escolhida = None
    area_id = request.args.get("area", type=int)
    if area_id:
        escolhida = next((area for area in areas if area.id == area_id), None)
        if escolhida is None:
            abort(404)
    minhas = (
        ReservaArea.query.filter_by(unidade_id=unidade.id)
        .join(AreaComum)
        .filter(AreaComum.condominio_id == unidade.condominio_id)
        .order_by(ReservaArea.data_evento.desc(), ReservaArea.id.desc())
        .all()
    )
    hoje = _hoje()
    for item in minhas:
        item.situacao_taxa = _situacao_taxa(item)
    proximo = _proximo_evento(minhas, hoje)
    return render_template(
        "morador/areas.html",
        areas=areas,
        area=escolhida,
        minhas=minhas,
        proximo=proximo,
        data_proximo=_data_evento_formatada(proximo.data_evento) if proximo else "",
        hoje=hoje.isoformat(),
        foco=request.args.get("foco", type=int),
        hora_inicio="08:00",
        hora_fim="22:00",
    )


@unidade_required
def morador_areas_ocupacao(unidade):
    area = _area_do_morador(unidade, request.args.get("area", type=int))
    if area is None:
        abort(404)
    return jsonify(datas=_datas_ocupadas(area.id))


@unidade_required
def morador_areas_solicitar(unidade):
    area = _area_do_morador(unidade, request.form.get("area_id", type=int))
    if area is None:
        abort(404)
    # Inadimplência da cota condominial não entra nesta decisão.
    data_evento = _parse_data(request.form.get("data_evento"))
    if data_evento is None or data_evento < _hoje():
        flash("Escolha uma data de hoje em diante.", "danger")
        return redirect(url_for("morador_areas", area=area.id))
    inicio = _parse_hora(request.form.get("horario_inicio")) or time(8, 0)
    fim = _parse_hora(request.form.get("horario_fim")) or time(22, 0)
    if inicio >= fim:
        flash("O horário final precisa ser depois do início.", "danger")
        return redirect(url_for("morador_areas", area=area.id))
    if request.form.get("de_acordo") != "1":
        flash("Confirme que está de acordo com as regras de uso.", "danger")
        return redirect(url_for("morador_areas", area=area.id))
    if _conflito(area.id, data_evento):
        flash("Esta data já está reservada.", "warning")
        return redirect(url_for("morador_areas", area=area.id))
    condominio = area.condominio
    taxa = round(float(area.taxa_uso_valor or 0), 2)
    automatica = bool(getattr(condominio, "aprovacao_automatica_reservas", False)) and taxa <= 0
    reserva = ReservaArea(
        area_id=area.id,
        unidade_id=unidade.id,
        data_evento=data_evento,
        horario_inicio=inicio,
        horario_fim=fim,
        status=(
            StatusReservaArea.APROVADA if automatica else StatusReservaArea.PENDENTE
        ),
    )
    db.session.add(reserva)
    db.session.flush()
    if _conflito(area.id, data_evento, ignorar_id=reserva.id):
        db.session.rollback()
        flash("Esta data já está reservada.", "warning")
        return redirect(url_for("morador_areas", area=area.id))
    if automatica:
        texto = _texto_aprovada(reserva)
        _notificar_morador(reserva, texto, texto)
        _notificar_gestao(
            reserva,
            f"Reserva automática: {unidade.identificador}",
            (
                f"📅 Reserva automática da Unidade {unidade.identificador} "
                f"para {area.nome} em {data_evento.strftime('%d/%m/%Y')}."
            ),
        )
        flash("Reserva aprovada.", "success")
    else:
        _notificar_gestao(
            reserva,
            f"Nova reserva: {unidade.identificador}",
            (
                f"📅 Nova reserva da Unidade {unidade.identificador} "
                f"para {area.nome} em {data_evento.strftime('%d/%m/%Y')}."
            ),
        )
        flash("Solicitação enviada para a gestão.", "success")
    db.session.commit()
    return redirect(url_for("morador_areas", foco=reserva.id))


def eventos_aprovados_no_dia(condominio_id, dia):
    if not condominio_id or dia is None:
        return []
    return (
        ReservaArea.query.join(AreaComum)
        .filter(
            AreaComum.condominio_id == condominio_id,
            ReservaArea.status == StatusReservaArea.APROVADA,
            ReservaArea.data_evento == dia,
        )
        .order_by(ReservaArea.horario_inicio.asc(), AreaComum.nome.asc())
        .all()
    )


def _cpf_digitos(texto):
    return "".join(caractere for caractere in (texto or "") if caractere.isdigit())


def _cpf_valido(texto):
    digitos = _cpf_digitos(texto)
    if len(digitos) != 11 or digitos == digitos[0] * 11:
        return None

    def digito(fatia):
        soma = sum(
            int(numero) * peso
            for numero, peso in zip(fatia, range(len(fatia) + 1, 1, -1))
        )
        resto = soma % 11
        return 0 if resto < 2 else 11 - resto

    if digito(digitos[:9]) != int(digitos[9]) or digito(digitos[:10]) != int(digitos[10]):
        return None
    return digitos


def _reserva_aprovada_da_unidade(unidade, reserva_id):
    reserva = ReservaArea.query.filter_by(id=reserva_id, unidade_id=unidade.id).first()
    if reserva is None or reserva.area is None:
        return None
    if reserva.area.condominio_id != unidade.condominio_id:
        return None
    if reserva.status != StatusReservaArea.APROVADA:
        return None
    return reserva


def _pode_remover_convidado(reserva, agora=None):
    agora = agora or _agora()
    inicio = datetime.combine(reserva.data_evento, reserva.horario_inicio)
    return agora < inicio


def _convidados_ordenados(reserva):
    return sorted(reserva.convidados.all(), key=lambda item: (item.nome or "").casefold())


@unidade_required
def morador_reservas_convidados(unidade, reserva_id):
    reserva = _reserva_aprovada_da_unidade(unidade, reserva_id)
    if reserva is None:
        abort(404)
    convidados = _convidados_ordenados(reserva)
    capacidade = int(reserva.area.capacidade or 0)
    return render_template(
        "morador/convidados.html",
        reserva=reserva,
        convidados=convidados,
        capacidade=capacidade,
        lotado=len(convidados) >= capacidade,
        exigir_cpf=bool(getattr(reserva.area.condominio, "exigir_cpf_convidados", False)),
        pode_remover=_pode_remover_convidado(reserva),
    )


@unidade_required
def morador_reservas_convidados_adicionar(unidade, reserva_id):
    reserva = _reserva_aprovada_da_unidade(unidade, reserva_id)
    if reserva is None:
        abort(404)
    nome = " ".join((request.form.get("nome") or "").split())
    if not nome or len(nome) > 200:
        flash("Informe o nome completo do convidado.", "danger")
        return redirect(url_for("morador_reservas_convidados", reserva_id=reserva.id))
    capacidade = int(reserva.area.capacidade or 0)
    if reserva.convidados.count() >= capacidade:
        flash("A lotação máxima desta área já foi atingida.", "warning")
        return redirect(url_for("morador_reservas_convidados", reserva_id=reserva.id))
    exigir = bool(getattr(reserva.area.condominio, "exigir_cpf_convidados", False))
    documento = None
    if exigir:
        documento = _cpf_valido(request.form.get("documento_cpf"))
        if documento is None:
            flash("Informe um CPF válido.", "danger")
            return redirect(url_for("morador_reservas_convidados", reserva_id=reserva.id))
        repetido = ConvidadoReserva.query.filter_by(
            reserva_id=reserva.id, documento_cpf=documento
        ).first()
        if repetido is not None:
            flash("Este CPF já está na lista.", "warning")
            return redirect(url_for("morador_reservas_convidados", reserva_id=reserva.id))
    db.session.add(
        ConvidadoReserva(
            reserva_id=reserva.id,
            nome=nome,
            documento_cpf=documento,
            status_checkin=False,
        )
    )
    db.session.commit()
    flash("Convidado adicionado à lista.", "success")
    return redirect(url_for("morador_reservas_convidados", reserva_id=reserva.id))


@unidade_required
def morador_reservas_convidados_remover(unidade, reserva_id, convidado_id):
    reserva = _reserva_aprovada_da_unidade(unidade, reserva_id)
    if reserva is None:
        abort(404)
    convidado = ConvidadoReserva.query.filter_by(
        id=convidado_id, reserva_id=reserva.id
    ).first()
    if convidado is None:
        abort(404)
    if convidado.status_checkin or not _pode_remover_convidado(reserva):
        flash("Este convidado não pode mais ser removido.", "warning")
        return redirect(url_for("morador_reservas_convidados", reserva_id=reserva.id))
    db.session.delete(convidado)
    db.session.commit()
    flash("Convidado removido da lista.", "success")
    return redirect(url_for("morador_reservas_convidados", reserva_id=reserva.id))


def register(app):
    app.add_url_rule("/admin/reservas", "admin_areas", areas_lista, methods=["GET"])
    app.add_url_rule("/sindico/reservas", "sindico_areas", areas_lista, methods=["GET"])
    app.add_url_rule(
        "/admin/reservas/areas",
        "admin_areas_salvar",
        areas_salvar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/sindico/reservas/areas",
        "sindico_areas_salvar",
        areas_salvar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/reservas/areas/<int:area_id>/excluir",
        "admin_areas_excluir",
        areas_excluir,
        methods=["POST"],
    )
    app.add_url_rule(
        "/sindico/reservas/areas/<int:area_id>/excluir",
        "sindico_areas_excluir",
        areas_excluir,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/reservas/<int:reserva_id>/aprovar",
        "admin_areas_aprovar",
        areas_aprovar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/sindico/reservas/<int:reserva_id>/aprovar",
        "sindico_areas_aprovar",
        areas_aprovar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/reservas/<int:reserva_id>/rejeitar",
        "admin_areas_rejeitar",
        areas_rejeitar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/sindico/reservas/<int:reserva_id>/rejeitar",
        "sindico_areas_rejeitar",
        areas_rejeitar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/morador/reservas",
        "morador_areas",
        morador_areas,
        methods=["GET"],
    )
    app.add_url_rule(
        "/morador/reservas/ocupacao",
        "morador_areas_ocupacao",
        morador_areas_ocupacao,
        methods=["GET"],
    )
    app.add_url_rule(
        "/morador/reservas/solicitar",
        "morador_areas_solicitar",
        morador_areas_solicitar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/morador/reservas/<int:reserva_id>/convidados",
        "morador_reservas_convidados",
        morador_reservas_convidados,
        methods=["GET"],
    )
    app.add_url_rule(
        "/morador/reservas/<int:reserva_id>/convidados",
        "morador_reservas_convidados_adicionar",
        morador_reservas_convidados_adicionar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/morador/reservas/<int:reserva_id>/convidados/<int:convidado_id>/remover",
        "morador_reservas_convidados_remover",
        morador_reservas_convidados_remover,
        methods=["POST"],
    )
