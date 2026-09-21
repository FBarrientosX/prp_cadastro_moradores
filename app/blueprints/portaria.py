"""Portaria: controle de acesso de visitantes/prestadores, encomendas e chegada de mudanças.

Extraído de app/routes.py seguindo o mesmo padrão dos módulos anteriores
(parceiro, superadmin, sindico, admin): sem a classe Blueprint do Flask,
apenas `register(app)` chamando `app.add_url_rule` para preservar os
endpoints originais.

`_condominio_id_portaria`, `_criar_notificacao`, `_registrar_auditoria`,
`_agendamento_do_tenant`, `_slug_logout` e `_salvar_imagem_upload` continuam
em app/routes.py por serem compartilhadas com outros módulos (notificações,
morador, admin, síndico) — são só importadas aqui, dentro de cada view.
"""

import os
from datetime import datetime

from flask import current_app, flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None

from app import db
from app.auth import condominio_id_obrigatorio, get_current_user, logout_usuario, portaria_required
from app.models import (
    AgendamentoMudanca,
    AutorizacaoAcesso,
    Encomenda,
    PerfilDestinoNotificacao,
    Pessoa,
    RegistroAcesso,
    Role,
    StatusAgendamentoMudanca,
    StatusAutorizacaoAcesso,
    StatusEncomenda,
    TipoVisitante,
    Unidade,
    Visitante,
)


def _normalizar_documento_visitante(documento):
    """Normaliza RG/CPF para busca única por tenant (remove pontuação)."""
    bruto = (documento or "").strip().upper()
    limpo = "".join(ch for ch in bruto if ch.isalnum())
    return limpo[:20]


def _entrada_aberta_visitante(condominio_id, visitante_id):
    """Registro com saída nula do visitante neste condomínio, se houver.

    Trava de aplicação no lugar do índice parcial SQLite (MySQL não
    suporta CREATE UNIQUE INDEX ... WHERE). O lock no visitante serializa
    check-ins concorrentes da mesma pessoa.
    """
    db.session.query(Visitante).filter_by(id=visitante_id).with_for_update().first()
    return RegistroAcesso.query.filter_by(
        condominio_id=condominio_id,
        visitante_id=visitante_id,
        data_saida=None,
    ).first()


def _autorizacao_do_tenant(autorizacao_id, condominio_id):
    """Carrega autorização prévia do mesmo condomínio (anti-IDOR)."""
    return AutorizacaoAcesso.query.filter_by(
        id=autorizacao_id, condominio_id=condominio_id
    ).first()


def _registro_acesso_do_tenant(registro_id, condominio_id):
    """Carrega log de acesso do mesmo condomínio (anti-IDOR)."""
    return RegistroAcesso.query.filter_by(
        id=registro_id, condominio_id=condominio_id
    ).first_or_404()


def _encomenda_do_tenant(encomenda_id, condominio_id):
    """Carrega encomenda do mesmo condomínio (anti-IDOR)."""
    return Encomenda.query.filter_by(
        id=encomenda_id, condominio_id=condominio_id
    ).first_or_404()


def _salvar_foto_encomenda(arquivo, prefixo="encomenda"):
    """Salva foto do pacote em static/uploads/encomendas/."""
    from app.routes import _salvar_imagem_upload

    pasta = current_app.config.get("UPLOAD_ENCOMENDAS_FOLDER") or os.path.join(
        current_app.root_path, "static", "uploads", "encomendas"
    )
    return _salvar_imagem_upload(arquivo, pasta, prefixo=prefixo)


def _contagens_acesso_aberto(condominio_id):
    if not condominio_id:
        return 0, 0
    base = (
        RegistroAcesso.query.join(Visitante)
        .filter(
            RegistroAcesso.condominio_id == condominio_id,
            RegistroAcesso.data_saida.is_(None),
        )
    )
    visitantes_no_local = base.filter(
        Visitante.tipo == TipoVisitante.VISITANTE
    ).count()
    prestadores_no_local = (
        RegistroAcesso.query.join(Visitante)
        .filter(
            RegistroAcesso.condominio_id == condominio_id,
            RegistroAcesso.data_saida.is_(None),
            Visitante.tipo == TipoVisitante.PRESTADOR,
        )
        .count()
    )
    return visitantes_no_local, prestadores_no_local


def _obter_ou_criar_visitante_autorizacao(autorizacao, condominio_id):
    """Resolve Visitante pelo documento da autorização, ou cria um novo."""
    documento = _normalizar_documento_visitante(autorizacao.documento)
    tipo = (
        autorizacao.tipo
        if autorizacao.tipo in TipoVisitante.CHOICES
        else TipoVisitante.VISITANTE
    )
    nome = (autorizacao.nome_visitante or "").strip()

    visitante = None
    if documento:
        visitante = Visitante.query.filter_by(
            condominio_id=condominio_id,
            documento=documento,
        ).first()

    if visitante is None:
        if not documento:
            documento = f"AUTH{autorizacao.id}"[:20]
        visitante = Visitante(
            condominio_id=condominio_id,
            documento=documento,
            nome=nome,
            tipo=tipo,
        )
        db.session.add(visitante)
        db.session.flush()
    else:
        visitante.nome = nome
        visitante.tipo = tipo

    return visitante


TZ_SAO_PAULO = "America/Sao_Paulo"


def _agora_sao_paulo():
    """Retorna datetime local de America/Sao_Paulo (naive, para persistência)."""
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(TZ_SAO_PAULO)).replace(tzinfo=None)
        except Exception:
            pass
    return datetime.utcnow()


def _hoje_sao_paulo():
    """Data civil de hoje no fuso America/Sao_Paulo (não a do servidor em UTC)."""
    return _agora_sao_paulo().date()


def _parse_data_hora_entrega(data_str, hora_str):
    """Combina data (YYYY-MM-DD) e hora (HH:MM) em datetime naive (SP)."""
    data_str = (data_str or "").strip()
    hora_str = (hora_str or "").strip()
    if data_str and hora_str:
        try:
            return datetime.strptime(f"{data_str} {hora_str}", "%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return _agora_sao_paulo()


def _ids_encomendas_form():
    """Extrai IDs de encomenda do form (lista ou string CSV)."""
    ids = []
    for raw in request.form.getlist("encomenda_ids"):
        for part in str(raw or "").split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))
    return list(dict.fromkeys(ids))


def portaria_logout():
    from app.routes import _slug_logout

    slug = _slug_logout()
    logout_usuario()
    flash("Sessão encerrada.", "info")
    return redirect(url_for("tenant_login", slug=slug, tab="equipe"))


@portaria_required
def portaria_dashboard():
    from app.routes import _condominio_id_portaria

    usuario = get_current_user()
    condominio_id = _condominio_id_portaria(usuario)
    visitantes_no_local, prestadores_no_local = _contagens_acesso_aberto(condominio_id)
    encomendas_pendentes = 0
    if condominio_id:
        encomendas_pendentes = Encomenda.query.filter_by(
            condominio_id=condominio_id,
            status=StatusEncomenda.PENDENTE,
        ).count()
    return render_template(
        "portaria/dashboard.html",
        current_user=usuario,
        visitantes_no_local=visitantes_no_local,
        prestadores_no_local=prestadores_no_local,
        encomendas_pendentes=encomendas_pendentes,
    )


@portaria_required
def portaria_acesso():
    from app.routes import _condominio_id_portaria

    usuario = get_current_user()
    condominio_id = _condominio_id_portaria(usuario)
    if not condominio_id:
        flash(
            "Conta de portaria sem condomínio vinculado. Contate a administração.",
            "danger",
        )
        return redirect(url_for("portaria_dashboard"))

    registros_abertos = (
        RegistroAcesso.query.join(Visitante)
        .join(Unidade)
        .filter(
            RegistroAcesso.condominio_id == condominio_id,
            RegistroAcesso.data_saida.is_(None),
        )
        .order_by(RegistroAcesso.data_entrada.asc())
        .all()
    )
    registros_historico = (
        RegistroAcesso.query.join(Visitante)
        .join(Unidade)
        .filter(
            RegistroAcesso.condominio_id == condominio_id,
            RegistroAcesso.data_saida.isnot(None),
        )
        .order_by(RegistroAcesso.data_saida.desc())
        .all()
    )
    unidades = (
        Unidade.query.filter_by(condominio_id=condominio_id)
        .order_by(Unidade.bloco, Unidade.apartamento)
        .all()
    )
    hoje_brasil = _hoje_sao_paulo()
    autorizacoes_hoje = (
        AutorizacaoAcesso.query.join(Unidade)
        .filter(
            AutorizacaoAcesso.condominio_id == condominio_id,
            AutorizacaoAcesso.data_prevista == hoje_brasil,
        )
        .order_by(AutorizacaoAcesso.created_at.asc())
        .all()
    )
    autorizacoes_futuras = (
        AutorizacaoAcesso.query.join(Unidade)
        .filter(
            AutorizacaoAcesso.condominio_id == condominio_id,
            AutorizacaoAcesso.data_prevista > hoje_brasil,
        )
        .order_by(
            AutorizacaoAcesso.data_prevista.asc(),
            AutorizacaoAcesso.created_at.asc(),
        )
        .all()
    )
    autorizacoes_historico = (
        AutorizacaoAcesso.query.join(Unidade)
        .filter(
            AutorizacaoAcesso.condominio_id == condominio_id,
            AutorizacaoAcesso.data_prevista < hoje_brasil,
        )
        .order_by(
            AutorizacaoAcesso.data_prevista.desc(),
            AutorizacaoAcesso.created_at.desc(),
        )
        .limit(50)
        .all()
    )
    return render_template(
        "portaria/acesso.html",
        current_user=usuario,
        registros_abertos=registros_abertos,
        registros_historico=registros_historico,
        unidades=unidades,
        tipos_visitante=TipoVisitante.CHOICES,
        autorizacoes_hoje=autorizacoes_hoje,
        autorizacoes_futuras=autorizacoes_futuras,
        autorizacoes_historico=autorizacoes_historico,
        data_hoje=hoje_brasil,
    )


@portaria_required
def portaria_acesso_entrada():
    from app.routes import _condominio_id_portaria, _criar_notificacao, _registrar_auditoria

    usuario = get_current_user()
    condominio_id = _condominio_id_portaria(usuario)
    if not condominio_id:
        flash(
            "Conta de portaria sem condomínio vinculado. Contate a administração.",
            "danger",
        )
        return redirect(url_for("portaria_acesso"))

    documento = _normalizar_documento_visitante(request.form.get("documento", ""))
    nome = (request.form.get("nome", "") or "").strip()
    tipo = (request.form.get("tipo", "") or "").strip()
    empresa = (request.form.get("empresa", "") or "").strip() or None
    unidade_id_raw = (request.form.get("unidade_id", "") or "").strip()
    placa_raw = (request.form.get("placa_veiculo") or "").strip()
    placa_veiculo = placa_raw.upper() if placa_raw else None

    if not documento or not nome or tipo not in TipoVisitante.CHOICES:
        flash("Preencha documento, nome e tipo para registrar a entrada.", "danger")
        return redirect(url_for("portaria_acesso"))

    try:
        unidade_id = int(unidade_id_raw)
    except (TypeError, ValueError):
        flash("Selecione a unidade de destino.", "danger")
        return redirect(url_for("portaria_acesso"))

    unidade = Unidade.query.filter_by(
        id=unidade_id, condominio_id=condominio_id
    ).first()
    if unidade is None:
        flash("Unidade inválida para este condomínio.", "danger")
        return redirect(url_for("portaria_acesso"))

    if tipo != TipoVisitante.PRESTADOR:
        empresa = None

    visitante = Visitante.query.filter_by(
        condominio_id=condominio_id,
        documento=documento,
    ).first()
    if visitante is None:
        visitante = Visitante(
            condominio_id=condominio_id,
            documento=documento,
            nome=nome,
            tipo=tipo,
            empresa=empresa,
        )
        db.session.add(visitante)
        db.session.flush()
    else:
        visitante.nome = nome
        visitante.tipo = tipo
        visitante.empresa = empresa

    entrada_aberta = _entrada_aberta_visitante(condominio_id, visitante.id)
    if entrada_aberta:
        nome_aberto = visitante.nome
        unidade_aberta = entrada_aberta.unidade.identificador
        db.session.rollback()
        flash(
            f"{nome_aberto} já possui entrada em aberto em {unidade_aberta}. "
            "Registre a saída antes de uma nova entrada.",
            "warning",
        )
        return redirect(url_for("portaria_acesso"))

    agora = _agora_sao_paulo()
    registro = RegistroAcesso(
        condominio_id=condominio_id,
        visitante_id=visitante.id,
        unidade_id=unidade.id,
        data_entrada=agora,
        data_saida=None,
        porteiro_id=usuario.id,
        placa_veiculo=placa_veiculo,
    )
    db.session.add(registro)
    _registrar_auditoria(
        usuario,
        f"Portaria '{usuario.username}' registrou entrada de {visitante.nome} "
        f"({visitante.tipo}) na unidade {unidade.identificador}.",
    )
    rotulo = "prestador" if visitante.tipo == TipoVisitante.PRESTADOR else "visitante"
    _criar_notificacao(
        condominio_id=condominio_id,
        perfil_destino=PerfilDestinoNotificacao.MORADOR,
        titulo="Chegada na portaria",
        mensagem=f"O {rotulo} {visitante.nome} acabou de entrar.",
        unidade_id=unidade.id,
    )
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash(
            f"{visitante.nome} já possui entrada em aberto em outra unidade. "
            "Registre a saída antes de uma nova entrada.",
            "warning",
        )
        return redirect(url_for("portaria_acesso"))
    flash(
        f"Entrada registrada: {visitante.nome} → {unidade.identificador} "
        f"às {agora.strftime('%H:%M')}.",
        "success",
    )
    return redirect(url_for("portaria_acesso"))


@portaria_required
def portaria_acesso_autorizada(auth_id):
    """Check-in expresso a partir de autorização prévia do morador."""
    from app.routes import _condominio_id_portaria, _criar_notificacao, _registrar_auditoria

    usuario = get_current_user()
    condominio_id = _condominio_id_portaria(usuario)
    if not condominio_id:
        flash(
            "Conta de portaria sem condomínio vinculado. Contate a administração.",
            "danger",
        )
        return redirect(url_for("portaria_acesso"))

    autorizacao = _autorizacao_do_tenant(auth_id, condominio_id)
    if not autorizacao:
        flash("Autorização não encontrada.", "danger")
        return redirect(url_for("portaria_acesso"))

    if autorizacao.status != StatusAutorizacaoAcesso.PENDENTE:
        flash("Esta autorização já foi concluída ou cancelada.", "warning")
        return redirect(url_for("portaria_acesso"))

    if autorizacao.data_prevista != _hoje_sao_paulo():
        flash("Esta autorização não é para o dia de hoje.", "warning")
        return redirect(url_for("portaria_acesso"))

    unidade = Unidade.query.filter_by(
        id=autorizacao.unidade_id, condominio_id=condominio_id
    ).first()
    if unidade is None:
        flash("Unidade de destino inválida para este condomínio.", "danger")
        return redirect(url_for("portaria_acesso"))

    nome = (autorizacao.nome_visitante or "").strip()
    if not nome:
        flash("Autorização sem nome de visitante. Não foi possível registrar.", "danger")
        return redirect(url_for("portaria_acesso"))

    visitante = _obter_ou_criar_visitante_autorizacao(autorizacao, condominio_id)

    entrada_aberta = _entrada_aberta_visitante(condominio_id, visitante.id)
    if entrada_aberta:
        db.session.rollback()
        flash(
            f"{visitante.nome} já possui entrada em aberto em "
            f"{entrada_aberta.unidade.identificador}. "
            "Registre a saída antes de confirmar a chegada.",
            "warning",
        )
        return redirect(url_for("portaria_acesso"))

    placa_portaria = (request.form.get("placa_veiculo") or "").strip().upper()
    if placa_portaria:
        placa_veiculo = placa_portaria
    else:
        placa_auth = (autorizacao.placa_veiculo or "").strip()
        placa_veiculo = placa_auth.upper() if placa_auth else None

    agora = _agora_sao_paulo()
    registro = RegistroAcesso(
        condominio_id=condominio_id,
        visitante_id=visitante.id,
        unidade_id=unidade.id,
        data_entrada=agora,
        data_saida=None,
        porteiro_id=usuario.id,
        placa_veiculo=placa_veiculo,
    )
    db.session.add(registro)
    autorizacao.status = StatusAutorizacaoAcesso.CONCLUIDA
    _registrar_auditoria(
        usuario,
        f"Portaria '{usuario.username}' confirmou chegada autorizada de "
        f"{visitante.nome} ({visitante.tipo}) na unidade {unidade.identificador}.",
    )
    rotulo = "prestador" if visitante.tipo == TipoVisitante.PRESTADOR else "visitante"
    _criar_notificacao(
        condominio_id=condominio_id,
        perfil_destino=PerfilDestinoNotificacao.MORADOR,
        titulo="Chegada na portaria",
        mensagem=f"O {rotulo} {visitante.nome} acabou de entrar.",
        unidade_id=unidade.id,
    )
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash(
            f"{visitante.nome} já possui entrada em aberto em outra unidade. "
            "Registre a saída antes de confirmar a chegada.",
            "warning",
        )
        return redirect(url_for("portaria_acesso"))
    flash(
        f"Chegada confirmada: {visitante.nome} → {unidade.identificador} "
        f"às {agora.strftime('%H:%M')}.",
        "success",
    )
    return redirect(url_for("portaria_acesso"))


@portaria_required
def portaria_acesso_saida(registro_id):
    from app.routes import _condominio_id_portaria, _registrar_auditoria

    usuario = get_current_user()
    condominio_id = _condominio_id_portaria(usuario)
    if not condominio_id:
        flash(
            "Conta de portaria sem condomínio vinculado. Contate a administração.",
            "danger",
        )
        return redirect(url_for("portaria_acesso"))

    registro = _registro_acesso_do_tenant(registro_id, condominio_id)
    if registro.data_saida:
        flash("Esta entrada já foi encerrada.", "info")
        return redirect(url_for("portaria_acesso"))

    registro.data_saida = _agora_sao_paulo()
    registro.porteiro_saida_id = usuario.id
    _registrar_auditoria(
        usuario,
        f"Portaria '{usuario.username}' registrou saída de "
        f"{registro.visitante.nome} da unidade {registro.unidade.identificador} "
        f"às {registro.data_saida.strftime('%H:%M')}.",
    )
    db.session.commit()
    flash(
        f"Saída registrada: {registro.visitante.nome} "
        f"às {registro.data_saida.strftime('%H:%M')}.",
        "success",
    )
    return redirect(url_for("portaria_acesso"))


@portaria_required
def portaria_encomendas():
    from app.routes import _condominio_id_portaria

    usuario = get_current_user()
    condominio_id = _condominio_id_portaria(usuario)
    if not condominio_id:
        flash(
            "Conta de portaria sem condomínio vinculado. Contate a administração.",
            "danger",
        )
        return redirect(url_for("portaria_dashboard"))

    pendentes = (
        Encomenda.query.options(
            joinedload(Encomenda.unidade),
            joinedload(Encomenda.porteiro_recebimento),
        )
        .filter(
            Encomenda.condominio_id == condominio_id,
            Encomenda.status == StatusEncomenda.PENDENTE,
        )
        .order_by(Encomenda.data_recebimento.asc())
        .all()
    )
    # Unidade.pessoas é lazy="dynamic" (não aceita joinedload); pré-carrega em lote.
    pessoas_por_unidade = {}
    unidade_ids = {item.unidade_id for item in pendentes if item.unidade_id}
    if unidade_ids:
        for pessoa in (
            Pessoa.query.filter(Pessoa.unidade_id.in_(unidade_ids))
            .order_by(Pessoa.nome_completo.asc())
            .all()
        ):
            pessoas_por_unidade.setdefault(pessoa.unidade_id, []).append(pessoa)

    historico = (
        Encomenda.query.join(Unidade)
        .filter(
            Encomenda.condominio_id == condominio_id,
            Encomenda.status == StatusEncomenda.ENTREGUE,
        )
        .order_by(Encomenda.data_entrega.desc())
        .all()
    )
    unidades = (
        Unidade.query.filter_by(condominio_id=condominio_id)
        .order_by(Unidade.bloco, Unidade.apartamento)
        .all()
    )
    return render_template(
        "portaria/encomendas.html",
        current_user=usuario,
        pendentes=pendentes,
        historico=historico,
        unidades=unidades,
        pessoas_por_unidade=pessoas_por_unidade,
        agora_entrega=_agora_sao_paulo(),
    )


@portaria_required
def portaria_encomendas_receber():
    from app.routes import _condominio_id_portaria, _criar_notificacao, _registrar_auditoria

    usuario = get_current_user()
    condominio_id = _condominio_id_portaria(usuario)
    if not condominio_id:
        flash(
            "Conta de portaria sem condomínio vinculado. Contate a administração.",
            "danger",
        )
        return redirect(url_for("portaria_encomendas"))

    destinatario = (request.form.get("destinatario", "") or "").strip() or None
    transportadora = (request.form.get("transportadora", "") or "").strip() or None
    codigo_rastreio = (request.form.get("codigo_rastreio", "") or "").strip() or None
    unidade_id_raw = (request.form.get("unidade_id", "") or "").strip()

    try:
        unidade_id = int(unidade_id_raw)
    except (TypeError, ValueError):
        flash("Selecione a unidade destinatária da encomenda.", "danger")
        return redirect(url_for("portaria_encomendas"))

    unidade = Unidade.query.filter_by(
        id=unidade_id, condominio_id=condominio_id
    ).first()
    if unidade is None:
        flash("Unidade inválida para este condomínio.", "danger")
        return redirect(url_for("portaria_encomendas"))

    foto_pacote, erro_foto = _salvar_foto_encomenda(
        request.files.get("foto_pacote"),
        prefixo=f"enc{unidade.id}",
    )
    if erro_foto:
        flash(erro_foto, "danger")
        return redirect(url_for("portaria_encomendas"))

    agora = _agora_sao_paulo()
    encomenda = Encomenda(
        condominio_id=condominio_id,
        unidade_id=unidade.id,
        destinatario=destinatario,
        transportadora=transportadora,
        codigo_rastreio=codigo_rastreio[:100] if codigo_rastreio else None,
        foto_pacote=foto_pacote,
        status=StatusEncomenda.PENDENTE,
        data_recebimento=agora,
        data_entrega=None,
        tentativas_contato=1,
        porteiro_recebimento_id=usuario.id,
        porteiro_entrega_id=None,
    )
    db.session.add(encomenda)
    _registrar_auditoria(
        usuario,
        f"Portaria '{usuario.username}' recebeu encomenda para "
        f"{unidade.identificador}"
        + (f" ({destinatario})" if destinatario else "")
        + ".",
    )
    _criar_notificacao(
        condominio_id=condominio_id,
        perfil_destino=PerfilDestinoNotificacao.MORADOR,
        titulo="Nova encomenda",
        mensagem="Você tem uma nova encomenda na portaria.",
        unidade_id=unidade.id,
    )
    db.session.commit()
    flash(
        f"Encomenda recebida para {unidade.identificador} "
        f"às {agora.strftime('%H:%M')}.",
        "success",
    )
    return redirect(url_for("portaria_encomendas"))


@portaria_required
def portaria_encomendas_entregar(encomenda_id):
    from app.routes import _condominio_id_portaria, _registrar_auditoria

    usuario = get_current_user()
    condominio_id = _condominio_id_portaria(usuario)
    if not condominio_id:
        flash(
            "Conta de portaria sem condomínio vinculado. Contate a administração.",
            "danger",
        )
        return redirect(url_for("portaria_encomendas"))

    encomenda = _encomenda_do_tenant(encomenda_id, condominio_id)
    if encomenda.status == StatusEncomenda.ENTREGUE:
        flash("Esta encomenda já foi entregue ao morador.", "info")
        return redirect(url_for("portaria_encomendas"))
    if encomenda.unidade is None:
        # Defesa extra: a exclusão de unidade já é bloqueada havendo
        # encomenda pendente, mas evita um 500 caso essa relação fique
        # órfã por qualquer outro caminho.
        flash(
            "A unidade desta encomenda não existe mais. Contate a administração.",
            "danger",
        )
        return redirect(url_for("portaria_encomendas"))

    entregue_para = (request.form.get("entregue_para") or "").strip()
    if not entregue_para:
        flash("Informe quem retirou a encomenda.", "danger")
        return redirect(url_for("portaria_encomendas"))

    data_entrega = _parse_data_hora_entrega(
        request.form.get("data_entrega"),
        request.form.get("hora_entrega"),
    )

    foto_entrega, erro_foto = _salvar_foto_encomenda(
        request.files.get("foto_entrega"),
        prefixo=f"ent{encomenda.id}",
    )
    if erro_foto:
        flash(erro_foto, "danger")
        return redirect(url_for("portaria_encomendas"))

    encomenda.status = StatusEncomenda.ENTREGUE
    encomenda.data_entrega = data_entrega
    encomenda.entregue_para = entregue_para[:200]
    encomenda.foto_entrega = foto_entrega
    encomenda.porteiro_entrega_id = usuario.id
    _registrar_auditoria(
        usuario,
        f"Portaria '{usuario.username}' entregou encomenda #{encomenda.id} "
        f"da unidade {encomenda.unidade.identificador} "
        f"para '{entregue_para}' "
        f"às {encomenda.data_entrega.strftime('%d/%m/%Y %H:%M')}.",
    )
    db.session.commit()
    flash(
        f"Entrega registrada para {encomenda.unidade.identificador} "
        f"(retirado por {entregue_para}) "
        f"às {encomenda.data_entrega.strftime('%H:%M')}.",
        "success",
    )
    return redirect(url_for("portaria_encomendas"))


@portaria_required
def portaria_encomendas_entregar_lote():
    """Baixa em lote: entrega várias encomendas pendentes do mesmo tenant."""
    from app.routes import _condominio_id_portaria, _registrar_auditoria

    usuario = get_current_user()
    condominio_id = _condominio_id_portaria(usuario)
    if not condominio_id:
        flash(
            "Conta de portaria sem condomínio vinculado. Contate a administração.",
            "danger",
        )
        return redirect(url_for("portaria_encomendas"))

    ids = _ids_encomendas_form()
    if not ids:
        flash("Selecione ao menos uma encomenda para entregar.", "warning")
        return redirect(url_for("portaria_encomendas"))

    entregue_para = (request.form.get("entregue_para") or "").strip()
    if not entregue_para:
        flash("Informe quem retirou as encomendas.", "danger")
        return redirect(url_for("portaria_encomendas"))

    data_entrega = _parse_data_hora_entrega(
        request.form.get("data_entrega"),
        request.form.get("hora_entrega"),
    )

    foto_entrega, erro_foto = _salvar_foto_encomenda(
        request.files.get("foto_entrega"),
        prefixo="entlote",
    )
    if erro_foto:
        flash(erro_foto, "danger")
        return redirect(url_for("portaria_encomendas"))

    encomendas = (
        Encomenda.query.filter(
            Encomenda.id.in_(ids),
            Encomenda.condominio_id == condominio_id,
            Encomenda.status == StatusEncomenda.PENDENTE,
        )
        .all()
    )
    if not encomendas:
        flash("Nenhuma encomenda pendente válida foi encontrada para entrega.", "warning")
        return redirect(url_for("portaria_encomendas"))

    entregue_para = entregue_para[:200]
    for encomenda in encomendas:
        encomenda.status = StatusEncomenda.ENTREGUE
        encomenda.data_entrega = data_entrega
        encomenda.entregue_para = entregue_para
        encomenda.foto_entrega = foto_entrega
        encomenda.porteiro_entrega_id = usuario.id

    _registrar_auditoria(
        usuario,
        f"Portaria '{usuario.username}' entregou {len(encomendas)} encomenda(s) "
        f"em lote para '{entregue_para}' "
        f"às {data_entrega.strftime('%d/%m/%Y %H:%M')} "
        f"(ids: {', '.join(str(e.id) for e in encomendas)}).",
    )
    db.session.commit()
    flash(
        f"{len(encomendas)} encomenda(s) entregue(s) com sucesso "
        f"(retirado por {entregue_para}).",
        "success",
    )
    return redirect(url_for("portaria_encomendas"))


@portaria_required
def portaria_encomendas_notificar(id):
    from app.routes import _condominio_id_portaria, _criar_notificacao, _registrar_auditoria

    usuario = get_current_user()
    condominio_id = _condominio_id_portaria(usuario)
    if not condominio_id:
        flash(
            "Conta de portaria sem condomínio vinculado. Contate a administração.",
            "danger",
        )
        return redirect(url_for("portaria_encomendas"))

    encomenda = _encomenda_do_tenant(id, condominio_id)
    if encomenda.status != StatusEncomenda.PENDENTE:
        flash(
            "Só é possível reenviar notificação de encomendas aguardando retirada.",
            "warning",
        )
        return redirect(url_for("portaria_encomendas"))
    if encomenda.unidade is None:
        flash(
            "A unidade desta encomenda não existe mais. Contate a administração.",
            "danger",
        )
        return redirect(url_for("portaria_encomendas"))

    remetente = encomenda.transportadora or "não informado"
    if encomenda.tentativas_contato is None:
        encomenda.tentativas_contato = 1
    encomenda.tentativas_contato += 1
    _criar_notificacao(
        condominio_id=condominio_id,
        perfil_destino=PerfilDestinoNotificacao.MORADOR,
        titulo="Lembrete de encomenda",
        mensagem=(
            "Lembrete: Você tem uma encomenda aguardando retirada na portaria "
            f"(Remetente: {remetente})."
        ),
        unidade_id=encomenda.unidade_id,
    )
    _registrar_auditoria(
        usuario,
        f"Portaria '{usuario.username}' reenviou notificação da encomenda "
        f"#{encomenda.id} para {encomenda.unidade.identificador} "
        f"(tentativa {encomenda.tentativas_contato}).",
    )
    db.session.commit()
    flash(
        f"Morador notificado novamente "
        f"(tentativa {encomenda.tentativas_contato}).",
        "success",
    )
    return redirect(url_for("portaria_encomendas"))


@portaria_required
def portaria_mudanca_chegar(agendamento_id):
    from app.routes import _agendamento_do_tenant, _registrar_auditoria

    usuario = get_current_user()
    hoje = _hoje_sao_paulo()
    agendamento = _agendamento_do_tenant(
        agendamento_id, condominio_id_obrigatorio(usuario)
    )

    if agendamento.status != StatusAgendamentoMudanca.APROVADA:
        flash("Somente mudanças aprovadas podem ter chegada registrada.", "warning")
        return redirect(url_for("portaria_dashboard"))

    if agendamento.data_mudanca != hoje:
        flash("O check-in de chegada só é permitido no dia da mudança.", "warning")
        return redirect(url_for("portaria_dashboard"))

    if agendamento.data_chegada:
        flash("A chegada deste caminhão já foi registrada.", "info")
        return redirect(url_for("portaria_dashboard"))

    agendamento.data_chegada = _agora_sao_paulo()
    # Registra o usuário logado (porteiro nominal ou admin em atuação).
    agendamento.porteiro_id = usuario.id
    _registrar_auditoria(
        usuario,
        f"{'Admin' if usuario.role == Role.ADMIN else 'Portaria'} "
        f"'{usuario.username}' registrou chegada do caminhão ({agendamento.tipo}) "
        f"da unidade {agendamento.unidade.identificador} em "
        f"{agendamento.data_chegada.strftime('%d/%m/%Y %H:%M')}.",
    )
    db.session.commit()
    flash(
        f"Chegada registrada para {agendamento.unidade.identificador} "
        f"às {agendamento.data_chegada.strftime('%H:%M')}.",
        "success",
    )
    return redirect(url_for("portaria_dashboard"))


@portaria_required
def portaria_mudancas():
    return redirect(url_for("portaria_dashboard"))


def register(app):
    """Registra as rotas da portaria preservando os endpoints legados."""
    app.add_url_rule(
        "/portaria/logout",
        "portaria_logout",
        portaria_logout,
        methods=["GET"],
    )
    app.add_url_rule(
        "/portaria",
        "portaria_index",
        portaria_dashboard,
        methods=["GET"],
    )
    app.add_url_rule(
        "/portaria/dashboard",
        "portaria_dashboard",
        portaria_dashboard,
        methods=["GET"],
    )
    app.add_url_rule(
        "/portaria/acesso",
        "portaria_acesso",
        portaria_acesso,
        methods=["GET"],
    )
    app.add_url_rule(
        "/portaria/acesso/entrada",
        "portaria_acesso_entrada",
        portaria_acesso_entrada,
        methods=["POST"],
    )
    app.add_url_rule(
        "/portaria/acesso/autorizada/<int:auth_id>",
        "portaria_acesso_autorizada",
        portaria_acesso_autorizada,
        methods=["POST"],
    )
    app.add_url_rule(
        "/portaria/acesso/saida/<int:registro_id>",
        "portaria_acesso_saida",
        portaria_acesso_saida,
        methods=["POST"],
    )
    app.add_url_rule(
        "/portaria/encomendas",
        "portaria_encomendas",
        portaria_encomendas,
        methods=["GET"],
    )
    app.add_url_rule(
        "/portaria/encomendas/receber",
        "portaria_encomendas_receber",
        portaria_encomendas_receber,
        methods=["POST"],
    )
    app.add_url_rule(
        "/portaria/encomendas/entregar/<int:encomenda_id>",
        "portaria_encomendas_entregar",
        portaria_encomendas_entregar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/portaria/encomendas/entregar_lote",
        "portaria_encomendas_entregar_lote",
        portaria_encomendas_entregar_lote,
        methods=["POST"],
    )
    app.add_url_rule(
        "/portaria/encomendas/notificar/<int:id>",
        "portaria_encomendas_notificar",
        portaria_encomendas_notificar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/portaria/encomendas/<int:id>/reenviar_notificacao",
        "portaria_encomendas_reenviar_notificacao",
        portaria_encomendas_notificar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/portaria/mudanca/<int:agendamento_id>/chegar",
        "portaria_mudanca_chegar",
        portaria_mudanca_chegar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/portaria/mudancas",
        "portaria_mudancas",
        portaria_mudancas,
        methods=["GET"],
    )
