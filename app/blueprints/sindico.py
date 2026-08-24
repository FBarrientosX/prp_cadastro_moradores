"""Painel do síndico: login, dashboard de unidades e aprovação de mudanças.

Extraído de app/routes.py seguindo o mesmo padrão dos módulos anteriores
(parceiro, superadmin): sem a classe Blueprint do Flask, apenas `register(app)`
chamando `app.add_url_rule` para preservar os endpoints originais.

Diferente de parceiro/superadmin, o síndico compartilha várias funções
privadas com outros módulos ainda não extraídos (reservas, admin) — jurisdição
por bloco (`_blocos_codigo_sindico`, `_sindico_gerencia_bloco`,
`_label_agrupamentos_sindico`), carregamento por tenant
(`_unidade_do_tenant`, `_pessoa_do_tenant`, `_agendamento_do_tenant`) e
auditoria/notificação (`_registrar_auditoria`, `_adicionar_notificacao_sindico`,
`_emails_unicos`). Essas continuam em app/routes.py e são só importadas aqui —
mover para lá quebraria os outros módulos que ainda dependem delas.
"""

import traceback

from flask import flash, redirect, render_template, request, session, url_for
from sqlalchemy import case, text

from app import db
from app.auth import (
    condominio_id_obrigatorio,
    get_current_user,
    login_usuario,
    logout_usuario,
    sindico_required,
)
from app.email_service import (
    enviar_email_reprovacao,
    enviar_email_validacao_parcial,
    enviar_email_validacao_sucesso,
)
from app.models import (
    AgendamentoMudanca,
    Role,
    StatusAgendamentoMudanca,
    StatusDocumento,
    StatusUnidade,
    Unidade,
    Usuario,
)
from app.utils import get_apartamentos_bloco


def sindico_login(slug):
    from app.routes import _carregar_condominio_entrada

    condominio, bloqueio = _carregar_condominio_entrada(slug)
    if bloqueio is not None:
        return bloqueio
    session["tenant_slug"] = condominio.slug

    usuario_atual = get_current_user()
    if usuario_atual and usuario_atual.is_sindico:
        if usuario_atual.condominio_id == condominio.id:
            return redirect(url_for("sindico_dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        usuario = Usuario.query.filter_by(
            username=username,
            role=Role.SINDICO,
            condominio_id=condominio.id,
        ).first()
        if usuario and usuario.check_password(password):
            login_usuario(usuario)
            return redirect(url_for("sindico_dashboard"))

        flash("Usuário ou senha inválidos.", "danger")

    return render_template(
        "login.html",
        titulo="Login do Síndico",
        action="sindico",
        condominio=condominio,
        slug=condominio.slug,
    )


def sindico_login_legacy():
    """Legacy: redireciona síndico para o tenant PRP."""
    return redirect(url_for("sindico_login", slug="prp"))


def sindico_logout():
    from app.routes import _slug_sessao_ou_prp

    slug = _slug_sessao_ou_prp()
    logout_usuario()
    flash("Sessão encerrada.", "info")
    return redirect(url_for("sindico_login", slug=slug))


@sindico_required
def sindico_dashboard():
    from app.routes import _blocos_codigo_sindico, _label_agrupamentos_sindico

    usuario = get_current_user()
    condominio_id = condominio_id_obrigatorio(usuario)
    blocos_sindico = _blocos_codigo_sindico(usuario)

    unidades_cadastradas = (
        Unidade.query.filter(
            Unidade.condominio_id == condominio_id,
            Unidade.bloco.in_(blocos_sindico),
        ).all()
        if blocos_sindico
        else []
    )

    mapa_bloco = []
    for bloco_codigo in blocos_sindico:
        unidades_por_apto = {
            u.apartamento: u for u in unidades_cadastradas if u.bloco == bloco_codigo
        }
        for apto in get_apartamentos_bloco(bloco_codigo):
            unidade = unidades_por_apto.get(apto)
            mapa_bloco.append(
                {
                    "bloco": bloco_codigo,
                    "apartamento": apto,
                    "unidade": unidade,
                    "status": unidade.status if unidade else "Aguardando Morador",
                }
            )

    return render_template(
        "dashboard_sindico.html",
        mapa_bloco=mapa_bloco,
        current_user=usuario,
        agrupamentos_label=_label_agrupamentos_sindico(usuario),
    )


@sindico_required
def sindico_aprovar(unidade_id):
    from app.routes import _sindico_gerencia_bloco, _unidade_do_tenant

    usuario = get_current_user()
    unidade = _unidade_do_tenant(unidade_id, condominio_id_obrigatorio(usuario))

    if not _sindico_gerencia_bloco(usuario, unidade.bloco):
        flash("Você não tem permissão para esta unidade.", "danger")
        return redirect(url_for("sindico_dashboard"))

    if unidade.status != StatusUnidade.PENDENTE:
        flash("Apenas cadastros pendentes podem ser aprovados.", "warning")
        return redirect(url_for("sindico_dashboard"))

    unidade.status = StatusUnidade.APROVADA
    db.session.commit()
    flash(f"Unidade {unidade.identificador} aprovada.", "success")
    return redirect(url_for("sindico_dashboard"))


@sindico_required
def sindico_reprovar(unidade_id):
    from app.routes import _sindico_gerencia_bloco, _unidade_do_tenant

    usuario = get_current_user()
    unidade = _unidade_do_tenant(unidade_id, condominio_id_obrigatorio(usuario))

    if not _sindico_gerencia_bloco(usuario, unidade.bloco):
        flash("Você não tem permissão para esta unidade.", "danger")
        return redirect(url_for("sindico_dashboard"))

    if unidade.status != StatusUnidade.PENDENTE:
        flash("Apenas cadastros pendentes podem ser reprovados.", "warning")
        return redirect(url_for("sindico_dashboard"))

    db.session.delete(unidade)
    db.session.commit()
    flash(f"Cadastro da unidade {unidade.identificador} reprovado e removido.", "info")
    return redirect(url_for("sindico_dashboard"))


@sindico_required
def sindico_reprovar_pessoa(pessoa_id):
    from app.routes import (
        _adicionar_notificacao_sindico,
        _pessoa_do_tenant,
        _registrar_auditoria,
        _sindico_gerencia_bloco,
    )

    usuario = get_current_user()
    condominio_id = condominio_id_obrigatorio(usuario)
    pessoa = _pessoa_do_tenant(pessoa_id, condominio_id)
    unidade = pessoa.unidade

    if not _sindico_gerencia_bloco(usuario, unidade.bloco):
        flash("Você não tem permissão para esta unidade.", "danger")
        return redirect(url_for("sindico_dashboard"))

    if unidade.status != StatusUnidade.PENDENTE:
        flash("Apenas moradores de cadastros pendentes podem ser reprovados.", "warning")
        return redirect(url_for("sindico_dashboard"))

    motivos_validos = {
        "Não é morador da unidade",
        "Dados incorretos ou incompletos",
        "Outros",
    }
    motivo = request.form.get("motivo", "").strip()
    if motivo not in motivos_validos:
        flash("Informe o motivo da reprovação do morador.", "danger")
        return redirect(url_for("sindico_dashboard"))

    responsavel = unidade.pessoas.filter_by(is_responsavel=True).first()
    email_responsavel = responsavel.email.strip() if responsavel and responsavel.email else None
    nome_pessoa = pessoa.nome_completo
    identificador_unidade = unidade.identificador

    _adicionar_notificacao_sindico(unidade, nome_pessoa, motivo)
    db.session.delete(pessoa)
    _registrar_auditoria(
        usuario,
        f"O síndico {usuario.username} reprovou/excluiu o morador "
        f"'{nome_pessoa}' da unidade '{identificador_unidade}'. Motivo: {motivo}",
    )
    db.session.commit()

    if email_responsavel:
        try:
            enviar_email_reprovacao(
                email_destino=email_responsavel,
                bloco=unidade.bloco,
                apartamento=unidade.apartamento,
                nome_morador=nome_pessoa,
                motivo=motivo,
            )
        except Exception:
            traceback.print_exc()
            flash(
                "Morador removido, mas não foi possível enviar o e-mail de notificação.",
                "warning",
            )
    else:
        flash(
            "Morador removido, mas a unidade não possui e-mail de responsável cadastrado.",
            "warning",
        )

    flash(
        f"Morador '{nome_pessoa}' reprovado e removido do cadastro da unidade "
        f"{identificador_unidade}.",
        "success",
    )
    return redirect(url_for("sindico_dashboard"))


@sindico_required
def sindico_validar_unidade(unidade_id):
    from app.routes import (
        _adicionar_notificacao_sindico,
        _emails_unicos,
        _registrar_auditoria,
        _sindico_gerencia_bloco,
        _unidade_do_tenant,
    )

    usuario = get_current_user()
    unidade = _unidade_do_tenant(unidade_id, condominio_id_obrigatorio(usuario))

    if not _sindico_gerencia_bloco(usuario, unidade.bloco):
        flash("Você não tem permissão para esta unidade.", "danger")
        return redirect(url_for("sindico_dashboard"))

    if unidade.status != StatusUnidade.PENDENTE:
        flash("Apenas cadastros pendentes podem ser validados.", "warning")
        return redirect(url_for("sindico_dashboard"))

    motivos_validos = {
        "Não é morador da unidade",
        "Dados incorretos ou incompletos",
        "Outros",
    }
    ids_reprovados = set()
    for valor in request.form.getlist("pessoas_reprovadas"):
        try:
            ids_reprovados.add(int(valor))
        except ValueError:
            continue

    moradores = unidade.pessoas.all()
    moradores_aprovados = []
    moradores_reprovados = []

    for pessoa in moradores:
        if pessoa.id not in ids_reprovados:
            moradores_aprovados.append(pessoa)
            continue

        motivo = request.form.get(f"motivo_pessoa_{pessoa.id}", "").strip()
        if motivo not in motivos_validos:
            flash(
                f"Informe um motivo válido para o morador {pessoa.nome_completo}.",
                "danger",
            )
            return redirect(url_for("sindico_dashboard"))

        moradores_reprovados.append({"nome": pessoa.nome_completo, "motivo": motivo})
        _adicionar_notificacao_sindico(unidade, pessoa.nome_completo, motivo)
        _registrar_auditoria(
            usuario,
            f"O síndico {usuario.username} reprovou/excluiu o morador "
            f"'{pessoa.nome_completo}' da unidade '{unidade.identificador}'. "
            f"Motivo: {motivo}",
        )
        db.session.delete(pessoa)

    emails_aprovados = _emails_unicos(moradores_aprovados)
    unidade_identificador = unidade.identificador
    bloco = unidade.bloco
    apartamento = unidade.apartamento

    unidade_tinha_documentos_validados = (
        unidade.documento_status == StatusDocumento.ENTREGUE
        or unidade.contrato_locacao_status == StatusDocumento.ENTREGUE
    )

    if moradores_aprovados:
        unidade.status = StatusUnidade.APROVADA
        _registrar_auditoria(
            usuario,
            f"O síndico {usuario.username} finalizou a validação da unidade "
            f"'{unidade_identificador}' com {len(moradores_aprovados)} morador(es) aprovado(s).",
        )
    elif unidade_tinha_documentos_validados:
        # Não apaga a unidade: documento/contrato já haviam sido validados
        # pela administração anteriormente (unidade REGISTRADA que voltou a
        # Pendente por atualização de cadastro). Excluí-la perderia esses
        # dados sem qualquer aviso ou chance de recuperação.
        unidade.status = StatusUnidade.PENDENTE
        _registrar_auditoria(
            usuario,
            f"O síndico {usuario.username} reprovou todos os moradores da unidade "
            f"'{unidade_identificador}', mas o cadastro da unidade foi mantido "
            "(documentos já haviam sido validados pela administração). A unidade "
            "voltou para Pendente, aguardando novo cadastro de moradores.",
        )
    else:
        db.session.delete(unidade)
        _registrar_auditoria(
            usuario,
            f"O síndico {usuario.username} reprovou todos os moradores da unidade "
            f"'{unidade_identificador}'. Cadastro removido e unidade voltou para "
            "Aguardando Morador.",
        )

    db.session.commit()

    if emails_aprovados:
        for email in emails_aprovados:
            try:
                if moradores_reprovados:
                    enviar_email_validacao_parcial(email, moradores_reprovados)
                else:
                    enviar_email_validacao_sucesso(email, bloco, apartamento)
            except Exception:
                traceback.print_exc()
                flash(
                    f"Validação salva, mas houve falha no envio de e-mail para {email}.",
                    "warning",
                )

    if not moradores_reprovados:
        flash(f"Unidade {unidade_identificador} validada com sucesso.", "success")
    elif moradores_aprovados:
        flash(
            f"Validação concluída na unidade {unidade_identificador} com reprovação parcial.",
            "warning",
        )
    elif unidade_tinha_documentos_validados:
        flash(
            f"Todos os moradores da unidade {unidade_identificador} foram reprovados. "
            "Como a unidade já tinha documentos validados, o cadastro foi mantido "
            "(voltou para Pendente) em vez de excluído.",
            "info",
        )
    else:
        flash(
            f"Todos os moradores da unidade {unidade_identificador} foram reprovados. "
            "A unidade voltou para Aguardando Morador.",
            "info",
        )

    return redirect(url_for("sindico_dashboard"))


@sindico_required
def sindico_mudancas():
    from app.routes import (
        _agendamento_do_tenant,
        _blocos_codigo_sindico,
        _label_agrupamentos_sindico,
        _registrar_auditoria,
        _sindico_gerencia_bloco,
    )

    usuario = get_current_user()
    condominio_id = condominio_id_obrigatorio(usuario)
    blocos_sindico = _blocos_codigo_sindico(usuario)

    if request.method == "POST":
        agendamento_id = request.form.get("agendamento_id", type=int)
        acao = request.form.get("acao", "").strip()
        if not agendamento_id:
            flash("Solicitação não encontrada no seu bloco.", "danger")
            return redirect(url_for("sindico_mudancas"))
        agendamento = _agendamento_do_tenant(agendamento_id, condominio_id)
        if not _sindico_gerencia_bloco(usuario, agendamento.unidade.bloco):
            flash("Solicitação não encontrada no seu bloco.", "danger")
            return redirect(url_for("sindico_mudancas"))

        if agendamento.status != StatusAgendamentoMudanca.PENDENTE_SINDICO:
            flash("Esta solicitação não está pendente de aprovação do síndico.", "warning")
            return redirect(url_for("sindico_mudancas"))

        if acao == "aprovar":
            # UPDATE condicional (só aplica se ainda estiver PENDENTE_SINDICO):
            # fecha a janela de corrida entre a checagem acima e o commit —
            # duas requisições (aprovar + rejeitar) não conseguem mais
            # processar a mesma solicitação com resultado indeterminado.
            resultado = db.session.execute(
                text(
                    "UPDATE agendamentos_mudanca SET status = :novo "
                    "WHERE id = :id AND status = :esperado"
                ),
                {
                    "novo": StatusAgendamentoMudanca.PENDENTE_ADMINISTRACAO,
                    "id": agendamento.id,
                    "esperado": StatusAgendamentoMudanca.PENDENTE_SINDICO,
                },
            )
            if resultado.rowcount == 0:
                db.session.rollback()
                flash("Esta solicitação já foi processada por outra ação.", "warning")
                return redirect(url_for("sindico_mudancas"))
            _registrar_auditoria(
                usuario,
                f"Síndico aprovou mudança {agendamento.tipo} da unidade "
                f"{agendamento.unidade.identificador} em "
                f"{agendamento.data_mudanca.strftime('%d/%m/%Y')}.",
            )
            db.session.commit()
            flash("Mudança encaminhada para aprovação da administração.", "success")
        elif acao == "rejeitar":
            motivo = request.form.get("motivo_rejeicao", "").strip()
            if not motivo:
                flash("Informe o motivo da rejeição.", "danger")
                return redirect(url_for("sindico_mudancas"))
            resultado = db.session.execute(
                text(
                    "UPDATE agendamentos_mudanca "
                    "SET status = :novo, motivo_rejeicao = :motivo "
                    "WHERE id = :id AND status = :esperado"
                ),
                {
                    "novo": StatusAgendamentoMudanca.REJEITADA,
                    "motivo": motivo,
                    "id": agendamento.id,
                    "esperado": StatusAgendamentoMudanca.PENDENTE_SINDICO,
                },
            )
            if resultado.rowcount == 0:
                db.session.rollback()
                flash("Esta solicitação já foi processada por outra ação.", "warning")
                return redirect(url_for("sindico_mudancas"))
            _registrar_auditoria(
                usuario,
                f"Síndico rejeitou mudança {agendamento.tipo} da unidade "
                f"{agendamento.unidade.identificador}. Motivo: {motivo}",
            )
            db.session.commit()
            flash("Solicitação de mudança rejeitada.", "info")
        else:
            flash("Ação inválida.", "danger")

        return redirect(url_for("sindico_mudancas"))

    solicitacoes = (
        AgendamentoMudanca.query.join(Unidade)
        .filter(
            AgendamentoMudanca.condominio_id == condominio_id,
            Unidade.condominio_id == condominio_id,
            Unidade.bloco.in_(blocos_sindico or [""]),
        )
        .order_by(
            case(
                (
                    AgendamentoMudanca.status
                    == StatusAgendamentoMudanca.PENDENTE_SINDICO,
                    0,
                ),
                else_=1,
            ),
            AgendamentoMudanca.data_mudanca.asc(),
            AgendamentoMudanca.data_solicitacao.desc(),
        )
        .all()
        if blocos_sindico
        else []
    )
    return render_template(
        "sindico_mudancas.html",
        solicitacoes=solicitacoes,
        current_user=usuario,
        agrupamentos_label=_label_agrupamentos_sindico(usuario),
        status_pendente_sindico=StatusAgendamentoMudanca.PENDENTE_SINDICO,
    )


def register(app):
    """Registra as rotas do síndico preservando os endpoints legados."""
    app.add_url_rule(
        "/c/<slug>/sindico/login",
        "sindico_login",
        sindico_login,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/sindico/login",
        "sindico_login_legacy",
        sindico_login_legacy,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/sindico/logout", "sindico_logout", sindico_logout, methods=["GET"]
    )
    app.add_url_rule(
        "/sindico", "sindico_dashboard", sindico_dashboard, methods=["GET"]
    )
    app.add_url_rule(
        "/sindico/aprovar/<int:unidade_id>",
        "sindico_aprovar",
        sindico_aprovar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/sindico/reprovar/<int:unidade_id>",
        "sindico_reprovar",
        sindico_reprovar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/sindico/reprovar-pessoa/<int:pessoa_id>",
        "sindico_reprovar_pessoa",
        sindico_reprovar_pessoa,
        methods=["POST"],
    )
    app.add_url_rule(
        "/sindico/validar-unidade/<int:unidade_id>",
        "sindico_validar_unidade",
        sindico_validar_unidade,
        methods=["POST"],
    )
    app.add_url_rule(
        "/sindico/mudancas",
        "sindico_mudancas",
        sindico_mudancas,
        methods=["GET", "POST"],
    )
