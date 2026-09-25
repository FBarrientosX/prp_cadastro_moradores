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

import json
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
    Condominio,
    Encomenda,
    Guarita,
    ItemChecklist,
    PerfilDestinoNotificacao,
    Pessoa,
    Plantao,
    RegistroAcesso,
    Role,
    StatusAgendamentoMudanca,
    StatusAutorizacaoAcesso,
    StatusEncomenda,
    StatusPlantao,
    TipoRespostaChecklist,
    TipoVisitante,
    Unidade,
    Usuario,
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


def _guarita_do_tenant(guarita_id, condominio_id):
    """Carrega guarita ativa do mesmo condomínio (anti-IDOR)."""
    return Guarita.query.filter_by(
        id=guarita_id, condominio_id=condominio_id, ativa=True
    ).first_or_404()


def _plantao_do_tenant(plantao_id, condominio_id):
    """Carrega plantão cuja guarita pertence ao condomínio (anti-IDOR)."""
    return (
        Plantao.query.join(Guarita)
        .filter(Plantao.id == plantao_id, Guarita.condominio_id == condominio_id)
        .first_or_404()
    )


def _garantir_guarita_padrao(condominio_id):
    """Cria Portaria Principal se o condomínio ainda não tiver guaritas."""
    if not condominio_id:
        return
    if Guarita.query.filter_by(condominio_id=condominio_id).first():
        return
    db.session.add(
        Guarita(
            nome="Portaria Principal",
            condominio_id=condominio_id,
            ativa=True,
        )
    )
    db.session.commit()


def _porteiros_do_condominio(condominio_id):
    """Porteiros (e segurança, se existir) do tenant para apoio e ronda."""
    return (
        Usuario.query.filter(
            Usuario.condominio_id == condominio_id,
            Usuario.role.in_((Role.PORTEIRO, "seguranca")),
        )
        .order_by(Usuario.username.asc(), Usuario.id.asc())
        .all()
    )


def _itens_checklist_ativos(guarita_id):
    return (
        ItemChecklist.query.filter_by(guarita_id=guarita_id, ativo=True)
        .order_by(ItemChecklist.nome_item.asc(), ItemChecklist.id.asc())
        .all()
    )


def _montar_checklist_abertura(form, itens):
    """Monta o JSON do checklist a partir dos campos `check_<id>` e itens avulsos."""
    por_id = {item.id: item for item in itens}
    respostas = {}
    for chave in form:
        if not str(chave).startswith("check_"):
            continue
        sufixo = str(chave).split("_", 1)[1]
        if not sufixo.isdigit():
            continue
        item = por_id.get(int(sufixo))
        if item is None:
            continue
        valor = (form.get(chave) or "").strip()
        if item.tipo_resposta == TipoRespostaChecklist.BOOLEANO and valor not in (
            "OK",
            "Defeito",
            "Não Se Encontra",
        ):
            raise ValueError(
                f"Informe OK, Defeito ou Não Se Encontra para «{item.nome_item}»."
            )
        respostas[f"check_{item.id}"] = valor

    for item in itens:
        campo = f"check_{item.id}"
        if campo in respostas:
            continue
        if item.tipo_resposta == TipoRespostaChecklist.BOOLEANO:
            raise ValueError(f"Informe o status de «{item.nome_item}».")
        respostas[campo] = ""

    nomes = form.getlist("custom_nome[]")
    quantidades = form.getlist("custom_qtd[]")
    estados = form.getlist("custom_estado[]")
    for nome, qtd, estado in zip(nomes, quantidades, estados):
        nome_limpo = (nome or "").strip()
        qtd_limpo = (qtd or "").strip()
        estado_limpo = (estado or "").strip()
        if not (nome_limpo or qtd_limpo or estado_limpo):
            continue
        if not nome_limpo or not qtd_limpo or not estado_limpo:
            raise ValueError(
                "Preencha nome, quantidade e estado de todos os itens avulsos."
            )
        chave = f"{nome_limpo} (Qtd: {qtd_limpo})"
        respostas[chave] = estado_limpo

    return json.dumps(respostas, ensure_ascii=False)


def _parse_checklist_json(raw, itens_por_id=None):
    """Normaliza checklist legado (lista) ou dinâmico (dict check_<id>) para exibição."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []

    itens_por_id = itens_por_id or {}
    if isinstance(data, list):
        return [
            {
                "label": item.get("label") or item.get("chave") or "Item",
                "valor": item.get("valor") or "—",
            }
            for item in data
            if isinstance(item, dict)
        ]
    if isinstance(data, dict):
        exibicao = []
        for chave, valor in data.items():
            item = None
            texto_chave = str(chave)
            if texto_chave.startswith("check_"):
                sufixo = texto_chave.split("_", 1)[1]
                if sufixo.isdigit():
                    item = itens_por_id.get(int(sufixo))
            label = item.nome_item if item is not None else texto_chave
            exibicao.append({"label": label, "valor": valor if valor else "—"})
        return exibicao
    return []


def _resolver_apoio_ronda(form, condominio, porteiros):
    """Devolve (apoio_id, ronda_id) válidos no tenant, ou None se o campo estiver desligado."""
    ids_validos = {porteiro.id for porteiro in porteiros}

    def _id_opcional(campo, habilitado):
        if not habilitado:
            return None
        bruto = (form.get(campo) or "").strip()
        if not bruto:
            return None
        if not bruto.isdigit():
            raise ValueError("Selecione um porteiro válido.")
        usuario_id = int(bruto)
        if usuario_id not in ids_validos:
            raise ValueError("O porteiro selecionado não pertence a este condomínio.")
        return usuario_id

    return (
        _id_opcional("apoio_id", bool(condominio and condominio.permitir_apoio)),
        _id_opcional("ronda_id", bool(condominio and condominio.permitir_ronda)),
    )


@portaria_required
def portaria_livro():
    from app.routes import _condominio_id_portaria

    usuario = get_current_user()
    condominio_id = _condominio_id_portaria(usuario)
    if not condominio_id:
        flash(
            "Conta de portaria sem condomínio vinculado. Contate a administração.",
            "danger",
        )
        return redirect(url_for("portaria_dashboard"))

    _garantir_guarita_padrao(condominio_id)
    condominio = Condominio.query.filter_by(id=condominio_id).first()
    porteiros = _porteiros_do_condominio(condominio_id)
    itens_por_id = {
        item.id: item
        for item in ItemChecklist.query.filter_by(condominio_id=condominio_id).all()
    }

    guaritas = (
        Guarita.query.filter_by(condominio_id=condominio_id, ativa=True)
        .order_by(Guarita.nome.asc(), Guarita.id.asc())
        .all()
    )
    contexto_base = dict(
        current_user=usuario,
        condominio=condominio,
        permitir_apoio=bool(condominio and condominio.permitir_apoio),
        permitir_ronda=bool(condominio and condominio.permitir_ronda),
        itens_checklist=[],
        porteiros=porteiros,
        guaritas=guaritas,
        guarita=None,
        plantao_aberto=None,
        historico=[],
    )
    if not guaritas:
        flash("Nenhuma guarita cadastrada para este condomínio.", "warning")
        return render_template("portaria/livro.html", **contexto_base)

    guarita_id_param = request.args.get("guarita_id", type=int)
    guarita = None
    if guarita_id_param:
        guarita = next((g for g in guaritas if g.id == guarita_id_param), None)
    if guarita is None:
        guarita = guaritas[0]

    itens_checklist = _itens_checklist_ativos(guarita.id)

    plantao_aberto = (
        Plantao.query.options(
            joinedload(Plantao.porteiro),
            joinedload(Plantao.apoio),
            joinedload(Plantao.ronda),
        )
        .filter_by(guarita_id=guarita.id, status=StatusPlantao.ABERTO)
        .order_by(Plantao.data_abertura.desc())
        .first()
    )

    historico = (
        Plantao.query.options(
            joinedload(Plantao.porteiro),
            joinedload(Plantao.apoio),
            joinedload(Plantao.ronda),
        )
        .filter_by(guarita_id=guarita.id, status=StatusPlantao.FECHADO)
        .order_by(Plantao.data_fechamento.desc(), Plantao.id.desc())
        .limit(20)
        .all()
    )
    for plantao in historico:
        plantao.checklist_itens = _parse_checklist_json(
            plantao.checklist_json, itens_por_id
        )
    if plantao_aberto:
        plantao_aberto.checklist_itens = _parse_checklist_json(
            plantao_aberto.checklist_json, itens_por_id
        )

    contexto_base.update(
        guarita=guarita,
        itens_checklist=itens_checklist,
        plantao_aberto=plantao_aberto,
        historico=historico,
    )
    return render_template("portaria/livro.html", **contexto_base)


@portaria_required
def portaria_plantao_abrir():
    from app.routes import _condominio_id_portaria, _registrar_auditoria

    usuario = get_current_user()
    condominio_id = _condominio_id_portaria(usuario)
    if not condominio_id:
        flash(
            "Conta de portaria sem condomínio vinculado. Contate a administração.",
            "danger",
        )
        return redirect(url_for("portaria_dashboard"))

    guarita_id = request.form.get("guarita_id", type=int)
    if not guarita_id:
        flash("Selecione a guarita para abrir o plantão.", "danger")
        return redirect(url_for("portaria_livro"))

    guarita = _guarita_do_tenant(guarita_id, condominio_id)

    aberto = Plantao.query.filter_by(
        guarita_id=guarita.id, status=StatusPlantao.ABERTO
    ).first()
    if aberto:
        flash(
            f"Já existe plantão aberto na {guarita.nome}. Feche-o antes de abrir outro.",
            "warning",
        )
        return redirect(url_for("portaria_livro", guarita_id=guarita.id))

    condominio = Condominio.query.filter_by(id=condominio_id).first()
    itens = _itens_checklist_ativos(guarita.id)
    porteiros = _porteiros_do_condominio(condominio_id)
    try:
        checklist_json = _montar_checklist_abertura(request.form, itens)
        apoio_id, ronda_id = _resolver_apoio_ronda(
            request.form, condominio, porteiros
        )
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("portaria_livro", guarita_id=guarita.id))

    plantao = Plantao(
        guarita_id=guarita.id,
        porteiro_id=usuario.id,
        apoio_id=apoio_id,
        ronda_id=ronda_id,
        data_abertura=_agora_sao_paulo(),
        status=StatusPlantao.ABERTO,
        checklist_json=checklist_json,
        ocorrencias=None,
    )
    db.session.add(plantao)
    _registrar_auditoria(
        usuario,
        f"Plantão aberto na {guarita.nome} (ID {guarita.id}).",
    )
    db.session.commit()
    flash(f"Plantão aberto na {guarita.nome}.", "success")
    return redirect(url_for("portaria_livro", guarita_id=guarita.id))


@portaria_required
def portaria_plantao_fechar():
    from app.routes import _condominio_id_portaria, _registrar_auditoria

    usuario = get_current_user()
    condominio_id = _condominio_id_portaria(usuario)
    if not condominio_id:
        flash(
            "Conta de portaria sem condomínio vinculado. Contate a administração.",
            "danger",
        )
        return redirect(url_for("portaria_dashboard"))

    plantao_id = request.form.get("plantao_id", type=int)
    if not plantao_id:
        flash("Plantão inválido.", "danger")
        return redirect(url_for("portaria_livro"))

    plantao = _plantao_do_tenant(plantao_id, condominio_id)
    if plantao.status != StatusPlantao.ABERTO:
        flash("Este plantão já está fechado.", "warning")
        return redirect(url_for("portaria_livro", guarita_id=plantao.guarita_id))

    ocorrencias = (request.form.get("ocorrencias") or "").strip()
    plantao.ocorrencias = ocorrencias or plantao.ocorrencias
    plantao.status = StatusPlantao.FECHADO
    plantao.data_fechamento = _agora_sao_paulo()

    guarita_nome = plantao.guarita.nome if plantao.guarita else "guarita"
    _registrar_auditoria(
        usuario,
        f"Plantão fechado na {guarita_nome} (plantão #{plantao.id}).",
    )
    db.session.commit()
    flash("Plantão fechado. Serviço passado com sucesso.", "success")
    return redirect(url_for("portaria_livro", guarita_id=plantao.guarita_id))


@portaria_required
def portaria_plantao_ocorrencia():
    """Registra ocorrência avulsa no plantão aberto (append no texto)."""
    from app.routes import _condominio_id_portaria, _registrar_auditoria

    usuario = get_current_user()
    condominio_id = _condominio_id_portaria(usuario)
    if not condominio_id:
        flash(
            "Conta de portaria sem condomínio vinculado. Contate a administração.",
            "danger",
        )
        return redirect(url_for("portaria_dashboard"))

    plantao_id = request.form.get("plantao_id", type=int)
    texto = (request.form.get("ocorrencia") or "").strip()
    if not plantao_id:
        flash("Plantão inválido.", "danger")
        return redirect(url_for("portaria_livro"))
    if not texto:
        flash("Informe o texto da ocorrência.", "danger")
        return redirect(url_for("portaria_livro"))

    plantao = _plantao_do_tenant(plantao_id, condominio_id)
    if plantao.status != StatusPlantao.ABERTO:
        flash("Só é possível registrar ocorrência em plantão aberto.", "warning")
        return redirect(url_for("portaria_livro", guarita_id=plantao.guarita_id))

    agora = _agora_sao_paulo().strftime("%d/%m/%Y %H:%M")
    linha = f"[{agora}] {texto}"
    if plantao.ocorrencias:
        plantao.ocorrencias = f"{plantao.ocorrencias.rstrip()}\n{linha}"
    else:
        plantao.ocorrencias = linha

    _registrar_auditoria(
        usuario,
        f"Ocorrência avulsa no plantão #{plantao.id} ({plantao.guarita.nome}).",
    )
    db.session.commit()
    flash("Ocorrência registrada no plantão.", "success")
    return redirect(url_for("portaria_livro", guarita_id=plantao.guarita_id))


@portaria_required
def portaria_plantao_evento():
    """Acrescenta um evento com hora ao texto de ocorrências do plantão aberto."""
    from app.routes import _condominio_id_portaria, _registrar_auditoria

    usuario = get_current_user()
    condominio_id = _condominio_id_portaria(usuario)
    if not condominio_id:
        flash(
            "Conta de portaria sem condomínio vinculado. Contate a administração.",
            "danger",
        )
        return redirect(url_for("portaria_dashboard"))

    plantao_id = request.form.get("plantao_id", type=int)
    texto = (request.form.get("evento") or request.form.get("texto") or "").strip()
    if not plantao_id:
        flash("Plantão inválido.", "danger")
        return redirect(url_for("portaria_livro"))
    if not texto:
        flash("Informe o texto do evento.", "danger")
        return redirect(url_for("portaria_livro"))

    plantao = _plantao_do_tenant(plantao_id, condominio_id)
    if plantao.status != StatusPlantao.ABERTO:
        flash("Só é possível registrar evento em plantão aberto.", "warning")
        return redirect(url_for("portaria_livro", guarita_id=plantao.guarita_id))

    hora_atual = _agora_sao_paulo().strftime("%H:%M")
    linha = f"[{hora_atual}] - {texto}\n"
    plantao.ocorrencias = f"{plantao.ocorrencias or ''}{linha}"

    _registrar_auditoria(
        usuario,
        f"Evento registrado no plantão #{plantao.id} ({plantao.guarita.nome}).",
    )
    db.session.commit()
    flash("Evento registrado no plantão.", "success")
    return redirect(url_for("portaria_livro", guarita_id=plantao.guarita_id))


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
    app.add_url_rule(
        "/portaria/livro",
        "portaria_livro",
        portaria_livro,
        methods=["GET"],
    )
    app.add_url_rule(
        "/portaria/plantao/abrir",
        "portaria_plantao_abrir",
        portaria_plantao_abrir,
        methods=["POST"],
    )
    app.add_url_rule(
        "/portaria/plantao/fechar",
        "portaria_plantao_fechar",
        portaria_plantao_fechar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/portaria/plantao/ocorrencia",
        "portaria_plantao_ocorrencia",
        portaria_plantao_ocorrencia,
        methods=["POST"],
    )
    app.add_url_rule(
        "/portaria/plantao/evento",
        "portaria_plantao_evento",
        portaria_plantao_evento,
        methods=["POST"],
    )
