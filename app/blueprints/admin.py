"""Painel do admin/assistente local: dashboard, unidades, usuários, ocorrências e mudanças.

Extraído de app/routes.py seguindo o mesmo padrão dos módulos anteriores
(parceiro, superadmin, sindico): sem a classe Blueprint do Flask, apenas
`register(app)` chamando `app.add_url_rule` para preservar os endpoints
originais.

`admin_ocorrencias`/`admin_ocorrencias_atualizar_status` usam o decorator
`admin_or_sindico_required` (também acessível pelo síndico) — ficaram aqui
por serem nomeadas `admin_*` no código original, não por serem exclusivas
do admin.

Como no módulo do síndico, várias funções privadas continuam em
app/routes.py por serem compartilhadas com módulos ainda não extraídos
(`_validar_data_mudanca` com `mudancas_morador`, `_unidade_do_tenant` /
`_usuario_do_tenant` / `_agendamento_do_tenant` / `_ocorrencia_do_tenant`
com portaria, `_registrar_auditoria` e `_label_agrupamentos_sindico` de
forma ampla) — são só importadas aqui, dentro de cada view.

`_aplicar_filtro_resgates_condominio` e `_montar_analytics_clube` vieram
junto por serem usadas exclusivamente por `admin_clube_vantagens`.
"""

from datetime import date, datetime, timedelta
import os
import secrets

from flask import current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename
from sqlalchemy import and_, case, func, or_, text

from app import db
from app.auth import (
    admin_or_assistente_required,
    admin_or_sindico_required,
    admin_required,
    condominio_id_obrigatorio,
    get_current_user,
    logout_usuario,
    normalizar_slug,
    validar_slug,
)
from app.models import (
    AgendamentoMudanca,
    Condominio,
    CredencialAcesso,
    Cupom,
    Encomenda,
    Guarita,
    LogAuditoria,
    ItemChecklist,
    Ocorrencia,
    Parceiro,
    Pessoa,
    ResgateCupom,
    Role,
    SindicoAgrupamento,
    StatusAgendamentoMudanca,
    StatusDocumento,
    StatusEncomenda,
    StatusOcorrencia,
    StatusPessoa,
    StatusUnidade,
    TipoRespostaChecklist,
    Unidade,
    Usuario,
    VinculoPessoa,
)


def _aplicar_filtro_resgates_condominio(query, condominio_id, unidade_ja_joinada=False):
    """Restringe métricas de resgate às unidades do condomínio (admin local)."""
    if condominio_id is None:
        return query
    if not unidade_ja_joinada:
        query = query.join(Unidade, ResgateCupom.unidade_id == Unidade.id)
    return query.filter(Unidade.condominio_id == condominio_id)


def _montar_analytics_clube(condominio_id=None):
    total_resgates_q = db.session.query(func.count(ResgateCupom.id))
    total_resgates_q = _aplicar_filtro_resgates_condominio(total_resgates_q, condominio_id)
    total_resgates = total_resgates_q.scalar() or 0

    total_cupons_ativos = (
        db.session.query(func.count(Cupom.id)).filter(Cupom.ativo.is_(True)).scalar() or 0
    )

    cupons_por_parceiro_rows = (
        db.session.query(
            Parceiro.nome_empresa,
            func.count(Cupom.id).label("total"),
        )
        .outerjoin(Cupom, Cupom.parceiro_id == Parceiro.id)
        .group_by(Parceiro.id, Parceiro.nome_empresa)
        .order_by(Parceiro.nome_empresa)
        .all()
    )

    resgates_por_bloco_q = (
        db.session.query(
            Unidade.bloco,
            func.count(ResgateCupom.id).label("total"),
        )
        .join(ResgateCupom, ResgateCupom.unidade_id == Unidade.id)
    )
    resgates_por_bloco_q = _aplicar_filtro_resgates_condominio(
        resgates_por_bloco_q, condominio_id, unidade_ja_joinada=True
    )
    resgates_por_bloco_rows = (
        resgates_por_bloco_q.group_by(Unidade.bloco)
        .order_by(func.count(ResgateCupom.id).desc())
        .all()
    )

    top_unidades_q = (
        db.session.query(
            Unidade.bloco,
            Unidade.apartamento,
            func.count(ResgateCupom.id).label("total"),
        )
        .join(ResgateCupom, ResgateCupom.unidade_id == Unidade.id)
    )
    top_unidades_q = _aplicar_filtro_resgates_condominio(
        top_unidades_q, condominio_id, unidade_ja_joinada=True
    )
    top_unidades_rows = (
        top_unidades_q.group_by(Unidade.id, Unidade.bloco, Unidade.apartamento)
        .order_by(func.count(ResgateCupom.id).desc())
        .limit(10)
        .all()
    )

    status_q = db.session.query(ResgateCupom.status, func.count(ResgateCupom.id))
    status_q = _aplicar_filtro_resgates_condominio(status_q, condominio_id)
    status_rows = status_q.group_by(ResgateCupom.status).all()
    status_map = {status: quantidade for status, quantidade in status_rows}
    resgates_ativos = status_map.get("Ativo", 0)
    resgates_utilizados = status_map.get("Utilizado", 0)
    taxa_conversao = (
        round((resgates_utilizados / total_resgates) * 100, 1) if total_resgates else 0.0
    )

    evolucao_q = db.session.query(
        func.date(ResgateCupom.data_resgate).label("data"),
        func.count(ResgateCupom.id).label("total"),
    )
    evolucao_q = _aplicar_filtro_resgates_condominio(evolucao_q, condominio_id)
    evolucao_rows = (
        evolucao_q.group_by(func.date(ResgateCupom.data_resgate))
        .order_by(func.date(ResgateCupom.data_resgate))
        .all()
    )

    parceiro_popular_q = (
        db.session.query(
            Parceiro.nome_empresa,
            func.count(ResgateCupom.id).label("total"),
        )
        .join(Cupom, Cupom.parceiro_id == Parceiro.id)
        .join(ResgateCupom, ResgateCupom.cupom_id == Cupom.id)
    )
    parceiro_popular_q = _aplicar_filtro_resgates_condominio(
        parceiro_popular_q, condominio_id
    )
    parceiro_popular_row = (
        parceiro_popular_q.group_by(Parceiro.id, Parceiro.nome_empresa)
        .order_by(func.count(ResgateCupom.id).desc())
        .first()
    )

    cupons_conversao_q = (
        db.session.query(
            Cupom.titulo,
            Parceiro.nome_empresa,
            func.count(ResgateCupom.id).label("total_resgates"),
            func.sum(
                case((ResgateCupom.status == "Utilizado", 1), else_=0)
            ).label("utilizados"),
        )
        .join(Parceiro, Cupom.parceiro_id == Parceiro.id)
        .join(ResgateCupom, ResgateCupom.cupom_id == Cupom.id)
    )
    cupons_conversao_q = _aplicar_filtro_resgates_condominio(
        cupons_conversao_q, condominio_id
    )
    cupons_conversao_rows = cupons_conversao_q.group_by(
        Cupom.id, Cupom.titulo, Parceiro.nome_empresa
    ).all()

    cupons_conversao = []
    for titulo, parceiro_nome, total_cupom_resgates, utilizados in cupons_conversao_rows:
        utilizados = int(utilizados or 0)
        taxa_cupom = (
            round((utilizados / total_cupom_resgates) * 100, 1)
            if total_cupom_resgates
            else 0.0
        )
        cupons_conversao.append(
            {
                "titulo": titulo,
                "parceiro": parceiro_nome,
                "resgates": total_cupom_resgates,
                "utilizados": utilizados,
                "taxa": taxa_cupom,
            }
        )
    cupons_conversao.sort(key=lambda item: (item["taxa"], item["utilizados"]), reverse=True)

    unidade_destaque = top_unidades_rows[0] if top_unidades_rows else None

    return {
        "charts": {
            "cupons_por_parceiro": {
                "labels": [row[0] for row in cupons_por_parceiro_rows],
                "values": [row[1] for row in cupons_por_parceiro_rows],
            },
            "resgates_por_bloco": {
                "labels": [f"Bloco {row[0]}" for row in resgates_por_bloco_rows],
                "values": [row[1] for row in resgates_por_bloco_rows],
            },
            "evolucao_resgates": {
                "labels": [
                    datetime.strptime(str(row[0]), "%Y-%m-%d").strftime("%d/%m/%Y")
                    for row in evolucao_rows
                ],
                "values": [row[1] for row in evolucao_rows],
            },
        },
        "status_resgates": {
            "ativo": resgates_ativos,
            "utilizado": resgates_utilizados,
            "taxa_conversao": taxa_conversao,
        },
        "metricas": {
            "total_cupons_ativos": total_cupons_ativos,
            "total_resgates": total_resgates,
            "parceiro_popular": parceiro_popular_row[0] if parceiro_popular_row else "—",
            "parceiro_popular_count": parceiro_popular_row[1] if parceiro_popular_row else 0,
            "unidade_engajada": (
                f"Bloco {unidade_destaque[0]} / Apto {unidade_destaque[1]}"
                if unidade_destaque
                else "—"
            ),
            "unidade_engajada_count": unidade_destaque[2] if unidade_destaque else 0,
        },
        "top5_unidades": [
            {
                "bloco": row[0],
                "apartamento": row[1],
                "total": row[2],
            }
            for row in top_unidades_rows[:5]
        ],
        "top10_unidades": [
            {
                "bloco": row[0],
                "apartamento": row[1],
                "total": row[2],
            }
            for row in top_unidades_rows
        ],
        "cupons_conversao": cupons_conversao,
    }


def admin_login():
    """Legacy: redireciona para a porta de entrada do tenant PRP."""
    return redirect(url_for("tenant_login", slug="prp"))


def admin_logout():
    from app.routes import _slug_sessao_ou_prp

    slug = _slug_sessao_ou_prp()
    logout_usuario()
    flash("Sessão encerrada.", "info")
    return redirect(url_for("tenant_login", slug=slug))


@admin_required
def admin_dashboard():
    usuario = get_current_user()
    condominio_id = condominio_id_obrigatorio(usuario)
    inicio_janela = datetime.utcnow() - timedelta(days=30)

    # Isolamento multi-tenant: todas as métricas escopadas ao condomínio logado.
    base_unidades = Unidade.query.filter(
        Unidade.condominio_id == condominio_id,
        Unidade.eh_setor_interno.is_(False),
    )

    total_aprovados = base_unidades.filter_by(
        status=StatusUnidade.REGISTRADA
    ).count()

    aguardando_registro = base_unidades.filter_by(
        status=StatusUnidade.APROVADA
    ).count()

    documentos_pendentes = base_unidades.filter(
        or_(
            Unidade.documento_status.in_(
                [
                    StatusDocumento.PENDENTE,
                    StatusDocumento.NAO_ENVIADO,
                    StatusDocumento.REJEITADO,
                ]
            ),
            and_(
                Unidade.pessoas.any(
                    and_(
                        Pessoa.is_responsavel.is_(True),
                        Pessoa.vinculo == VinculoPessoa.LOCATARIO,
                    )
                ),
                Unidade.contrato_locacao_status.in_(
                    [StatusDocumento.PENDENTE, StatusDocumento.NAO_ENVIADO]
                ),
            ),
        )
    ).count()

    cadastros_por_bloco_rows = (
        db.session.query(Unidade.bloco, func.count(Unidade.id).label("total"))
        .filter(
            Unidade.condominio_id == condominio_id,
            Unidade.eh_setor_interno.is_(False),
            Unidade.status.in_([StatusUnidade.APROVADA, StatusUnidade.REGISTRADA]),
        )
        .group_by(Unidade.bloco)
        .order_by(Unidade.bloco)
        .all()
    )
    cadastros_por_bloco = [
        {"bloco": row.bloco, "total": row.total} for row in cadastros_por_bloco_rows
    ]

    cadastros_por_data_rows = (
        db.session.query(
            func.date(Unidade.data_criacao).label("data"),
            func.count(Unidade.id).label("total"),
        )
        .filter(
            Unidade.condominio_id == condominio_id,
            Unidade.eh_setor_interno.is_(False),
            Unidade.data_criacao >= inicio_janela,
        )
        .group_by(func.date(Unidade.data_criacao))
        .order_by(func.date(Unidade.data_criacao))
        .all()
    )
    cadastros_por_data = [
        {
            "data": row.data.isoformat()
            if hasattr(row.data, "isoformat")
            else str(row.data),
            "total": row.total,
        }
        for row in cadastros_por_data_rows
    ]

    proporcao_status_rows = (
        db.session.query(Unidade.status, func.count(Unidade.id).label("total"))
        .filter(Unidade.condominio_id == condominio_id)
        .group_by(Unidade.status)
        .order_by(Unidade.status)
        .all()
    )
    proporcao_status = [
        {"status": row.status, "total": row.total} for row in proporcao_status_rows
    ]

    return render_template(
        "admin_dashboard.html",
        total_aprovados=total_aprovados,
        aguardando_registro=aguardando_registro,
        documentos_pendentes=documentos_pendentes,
        cadastros_por_bloco=cadastros_por_bloco,
        cadastros_por_data=cadastros_por_data,
        proporcao_status=proporcao_status,
        encomendas_setores=encomendas_pendentes_setores(condominio_id),
        current_user=usuario,
    )


@admin_or_assistente_required
def admin_index():
    usuario = get_current_user()
    condominio_id = condominio_id_obrigatorio(usuario)

    aguardando_registro = (
        Unidade.query.filter(
            Unidade.condominio_id == condominio_id,
            Unidade.eh_setor_interno.is_(False),
            or_(
                Unidade.status == StatusUnidade.APROVADA,
                Unidade.atualizacao_pendente.is_(True),
            ),
        )
        .order_by(Unidade.bloco, Unidade.apartamento)
        .all()
    )
    finalizados = (
        Unidade.query.filter(
            Unidade.condominio_id == condominio_id,
            Unidade.eh_setor_interno.is_(False),
            Unidade.status == StatusUnidade.REGISTRADA,
            Unidade.atualizacao_pendente.is_(False),
        )
        .order_by(Unidade.bloco, Unidade.apartamento)
        .all()
    )
    sindicos = (
        Usuario.query.filter_by(role=Role.SINDICO, condominio_id=condominio_id)
        .order_by(Usuario.username)
        .all()
    )
    equipe_acessos = (
        Usuario.query.filter(
            Usuario.condominio_id == condominio_id,
            Usuario.role.in_(
                [Role.ADMIN, Role.ASSISTENTE, Role.SINDICO, Role.PORTEIRO]
            ),
        )
        .order_by(Usuario.role, Usuario.username)
        .all()
    )

    abas_validas = {"aguardando", "finalizados", "sindicos", "equipe"}
    aba_ativa = request.args.get("tab", "aguardando")
    if aba_ativa not in abas_validas:
        aba_ativa = "aguardando"
    if aba_ativa == "sindicos":
        aba_ativa = "equipe" if usuario.role == Role.ADMIN else "aguardando"
    if aba_ativa == "equipe" and usuario.role != Role.ADMIN:
        aba_ativa = "aguardando"

    return render_template(
        "dashboard_admin.html",
        aguardando_registro=aguardando_registro,
        finalizados=finalizados,
        sindicos=sindicos,
        equipe_acessos=equipe_acessos,
        current_user=usuario,
        aba_ativa=aba_ativa,
        blocos_residenciais=_blocos_residenciais(),
    )


@admin_required
def admin_clube_vantagens():
    """
    Admin local: apenas Relatórios/Analytics do próprio condomínio.

    Clube de Vantagens é catálogo GLOBAL (Parceiro/Cupom sem condominio_id).
    Mutação de parceiros fica exclusiva do Super Admin (/superadmin/parceiros).
    """
    usuario = get_current_user()
    analytics = _montar_analytics_clube(condominio_id=usuario.condominio_id)

    return render_template(
        "admin_clube_vantagens.html",
        current_user=usuario,
        active_tab="analytics",
        analytics=analytics,
    )


@admin_required
def admin_clube_vantagens_analytics():
    return redirect(url_for("admin_clube_vantagens"))


@admin_or_assistente_required
def admin_registrar(unidade_id):
    from app.routes import _unidade_do_tenant

    condominio_id = condominio_id_obrigatorio()
    unidade = _unidade_do_tenant(unidade_id, condominio_id)

    if unidade.status != StatusUnidade.APROVADA:
        flash("Apenas unidades aprovadas podem ser registradas.", "warning")
        return redirect(url_for("admin_index"))

    unidade.status = StatusUnidade.REGISTRADA
    db.session.commit()
    flash(f"Unidade {unidade.identificador} marcada como registrada.", "success")
    return redirect(url_for("admin_index"))


@admin_or_assistente_required
def admin_aprovar_atualizacao(unidade_id):
    """Aprova atualização cadastral sem derrubar o status Aprovada/Registrada."""
    from app.routes import _registrar_auditoria, _unidade_do_tenant

    condominio_id = condominio_id_obrigatorio()
    unidade = _unidade_do_tenant(unidade_id, condominio_id)
    usuario = get_current_user()

    if not unidade.atualizacao_pendente:
        flash("Esta unidade não possui atualização pendente.", "warning")
        return redirect(url_for("admin_index"))

    for pessoa in unidade.pessoas.all():
        pessoa.status = StatusPessoa.APROVADO
    unidade.atualizacao_pendente = False
    if usuario:
        _registrar_auditoria(
            usuario,
            f"Atualização cadastral aprovada — Bloco {unidade.bloco}, "
            f"Apto {unidade.apartamento}.",
        )
    db.session.commit()
    flash(
        f"Atualização da unidade {unidade.identificador} aprovada. "
        "Moradores marcados como aprovados.",
        "success",
    )
    return redirect(url_for("admin_index"))


@admin_or_assistente_required
def admin_unidade_alterar_senha(unidade_id):
    from app.routes import _unidade_do_tenant

    condominio_id = condominio_id_obrigatorio()
    unidade = _unidade_do_tenant(unidade_id, condominio_id)
    nova_senha = request.form.get("nova_senha", "").strip()

    if not nova_senha:
        flash("Informe a nova senha.", "danger")
        return redirect(url_for("admin_index"))
    if len(nova_senha) < 6:
        flash("A senha deve ter ao menos 6 caracteres.", "danger")
        return redirect(url_for("admin_index"))

    unidade.set_password(nova_senha)
    db.session.commit()
    flash(f"Senha da unidade {unidade.identificador} alterada com sucesso.", "success")
    return redirect(url_for("admin_index"))


@admin_or_assistente_required
def admin_excluir_unidade(unidade_id):
    from app.routes import _unidade_do_tenant

    usuario = get_current_user()
    if usuario.role != Role.ADMIN:
        flash("Acesso negado.", "danger")
        return redirect(url_for("admin_index"))

    condominio_id = condominio_id_obrigatorio(usuario)
    unidade = _unidade_do_tenant(unidade_id, condominio_id)

    encomenda_pendente = Encomenda.query.filter_by(
        unidade_id=unidade.id, status=StatusEncomenda.PENDENTE
    ).first()
    if encomenda_pendente:
        # Encomenda/RegistroAcesso/Ocorrência/Notificação não têm cascade de
        # exclusão a partir de Unidade (são histórico, não dado do morador).
        # Uma encomenda pendente referenciando uma unidade apagada travaria a
        # portaria com um erro ao tentar processá-la.
        flash(
            "Esta unidade possui encomenda(s) pendente(s) de entrega. "
            "Registre a entrega (ou trate a encomenda) antes de excluir o cadastro.",
            "danger",
        )
        return redirect(url_for("admin_index"))

    db.session.delete(unidade)
    db.session.commit()

    flash(
        "Cadastro da unidade apagado com sucesso. Ela está livre para novo registro.",
        "success",
    )
    return redirect(url_for("admin_index"))


@admin_required
def admin_validar_documentacao(unidade_id):
    from app.drive_api import delete_from_drive
    from app.models import PerfilDestinoNotificacao
    from app.routes import (
        _criar_notificacao,
        _registrar_auditoria,
        _unidade_do_tenant,
    )

    condominio_id = condominio_id_obrigatorio()
    unidade = _unidade_do_tenant(unidade_id, condominio_id)
    acao = (request.form.get("acao") or "").strip().lower()
    usuario = get_current_user()

    if acao == "aprovar":
        unidade.documento_status = StatusDocumento.APROVADO
        if usuario:
            _registrar_auditoria(
                usuario,
                f"Documentação aprovada — Bloco {unidade.bloco}, "
                f"Apto {unidade.apartamento}.",
            )
        db.session.commit()
        flash(
            f"Documentação da unidade Bloco {unidade.bloco}, "
            f"Apto {unidade.apartamento} aprovada.",
            "success",
        )
        return redirect(url_for("admin_index"))

    if acao == "rejeitar":
        if unidade.documento_drive_id:
            delete_from_drive(unidade.documento_drive_id)
        if unidade.documento2_drive_id:
            delete_from_drive(unidade.documento2_drive_id)

        unidade.documento_drive_id = None
        unidade.documento_url = None
        unidade.documento2_drive_id = None
        unidade.documento2_url = None
        unidade.documento_status = StatusDocumento.REJEITADO

        _criar_notificacao(
            condominio_id,
            PerfilDestinoNotificacao.MORADOR,
            "Documentação invalidada",
            (
                "A documentação da sua unidade foi invalidada pela administração. "
                "É necessário enviar novamente os documentos pelo painel "
                "(Atualizar Dados)."
            ),
            unidade_id=unidade.id,
        )
        if usuario:
            _registrar_auditoria(
                usuario,
                f"Documentação rejeitada/invalidada — Bloco {unidade.bloco}, "
                f"Apto {unidade.apartamento}.",
            )
        db.session.commit()
        flash(
            f"Documentação da unidade Bloco {unidade.bloco}, "
            f"Apto {unidade.apartamento} invalidada. O morador foi notificado.",
            "warning",
        )
        return redirect(url_for("admin_index"))

    flash("Ação de validação documental inválida.", "danger")
    return redirect(url_for("admin_index"))


@admin_required
def admin_validar_documento(unidade_id):
    from app.routes import _unidade_do_tenant

    unidade = _unidade_do_tenant(unidade_id, condominio_id_obrigatorio())
    unidade.documento_status = StatusDocumento.APROVADO
    db.session.commit()
    flash(
        f"Documento da unidade Bloco {unidade.bloco}, Apto {unidade.apartamento} "
        f"marcado como aprovado.",
        "success",
    )
    return redirect(url_for("admin_index"))


@admin_required
def admin_validar_contrato_locacao(unidade_id):
    from app.routes import _unidade_do_tenant

    unidade = _unidade_do_tenant(unidade_id, condominio_id_obrigatorio())

    if unidade.contrato_locacao_status == StatusDocumento.NAO_APLICAVEL:
        flash(
            f"Contrato de locação não se aplica à unidade Bloco {unidade.bloco}, "
            f"Apto {unidade.apartamento}.",
            "warning",
        )
        return redirect(url_for("admin_index"))

    unidade.contrato_locacao_status = StatusDocumento.ENTREGUE
    db.session.commit()
    flash(
        f"Contrato de locação da unidade Bloco {unidade.bloco}, "
        f"Apto {unidade.apartamento} marcado como entregue/validado.",
        "success",
    )
    return redirect(url_for("admin_index"))


@admin_required
def admin_validar_documentos(unidade_id):
    from app.routes import _unidade_do_tenant

    unidade = _unidade_do_tenant(unidade_id, condominio_id_obrigatorio())
    unidade.documento_status = StatusDocumento.APROVADO
    if unidade.contrato_locacao_status != StatusDocumento.NAO_APLICAVEL:
        unidade.contrato_locacao_status = StatusDocumento.ENTREGUE

    db.session.commit()
    flash(
        f"Documentos da unidade Bloco {unidade.bloco}, Apto {unidade.apartamento} "
        f"marcados como aprovados/validados.",
        "success",
    )
    return redirect(url_for("admin_index"))


@admin_required
def admin_atualizar_status_documentos(unidade_id):
    from app.routes import _unidade_do_tenant

    unidade = _unidade_do_tenant(unidade_id, condominio_id_obrigatorio())
    documento_status = request.form.get("documento_status", "").strip()
    contrato_status = request.form.get("contrato_locacao_status", "").strip()
    status_permitidos = {
        StatusDocumento.PENDENTE,
        StatusDocumento.ENTREGUE,
        StatusDocumento.APROVADO,
        StatusDocumento.REJEITADO,
    }

    if documento_status in status_permitidos:
        unidade.documento_status = documento_status

    if unidade.contrato_locacao_status != StatusDocumento.NAO_APLICAVEL:
        if contrato_status in {StatusDocumento.PENDENTE, StatusDocumento.ENTREGUE}:
            unidade.contrato_locacao_status = contrato_status
    else:
        unidade.contrato_locacao_status = StatusDocumento.NAO_APLICAVEL

    db.session.commit()
    flash(
        f"Status dos documentos da unidade Bloco {unidade.bloco}, "
        f"Apto {unidade.apartamento} atualizados.",
        "success",
    )
    return redirect(url_for("admin_index"))


def _redirect_equipe_acessos():
    return redirect(url_for("admin_index", tab="equipe"))


def _guarita_do_tenant(guarita_id, condominio_id):
    return Guarita.query.filter_by(
        id=guarita_id, condominio_id=condominio_id
    ).first_or_404()


def _item_checklist_do_tenant(item_id, condominio_id):
    return ItemChecklist.query.filter_by(
        id=item_id, condominio_id=condominio_id
    ).first_or_404()


@admin_required
def admin_portaria_configuracoes():
    condominio_id = condominio_id_obrigatorio()
    condominio = Condominio.query.filter_by(id=condominio_id).first_or_404()
    guaritas = (
        Guarita.query.filter_by(condominio_id=condominio_id)
        .order_by(Guarita.nome.asc(), Guarita.id.asc())
        .all()
    )
    itens_checklist = (
        ItemChecklist.query.filter_by(condominio_id=condominio_id)
        .order_by(
            ItemChecklist.guarita_id.asc(),
            ItemChecklist.nome_item.asc(),
            ItemChecklist.id.asc(),
        )
        .all()
    )
    guaritas_ativas = [g for g in guaritas if g.ativa]
    return render_template(
        "admin/config_portaria.html",
        condominio=condominio,
        guaritas=guaritas,
        guaritas_ativas=guaritas_ativas,
        itens_checklist=itens_checklist,
    )


@admin_required
def admin_portaria_configuracoes_gerais():
    from app.routes import _registrar_auditoria

    condominio_id = condominio_id_obrigatorio()
    condominio = Condominio.query.filter_by(id=condominio_id).first_or_404()
    condominio.permitir_apoio = request.form.get("permitir_apoio") == "1"
    condominio.permitir_ronda = request.form.get("permitir_ronda") == "1"
    usuario = get_current_user()
    if usuario:
        _registrar_auditoria(
            usuario,
            "Configurações gerais do livro de serviço atualizadas "
            f"(apoio={'sim' if condominio.permitir_apoio else 'não'}, "
            f"ronda={'sim' if condominio.permitir_ronda else 'não'}).",
        )
    db.session.commit()
    flash("Configurações gerais da portaria salvas.", "success")
    return redirect(url_for("admin_portaria_configuracoes"))


@admin_required
def admin_portaria_guarita_salvar():
    from app.routes import _registrar_auditoria

    condominio_id = condominio_id_obrigatorio()
    nome = (request.form.get("nome") or "").strip()
    if not nome or len(nome) > 100:
        flash("Informe um nome de guarita com até 100 caracteres.", "danger")
        return redirect(url_for("admin_portaria_configuracoes"))

    guarita_id = request.form.get("guarita_id", type=int)
    usuario = get_current_user()
    if guarita_id:
        guarita = _guarita_do_tenant(guarita_id, condominio_id)
        guarita.nome = nome
        mensagem = f"Guarita atualizada: {nome} (ID {guarita.id})."
        flash("Guarita atualizada.", "success")
    else:
        guarita = Guarita(nome=nome, condominio_id=condominio_id, ativa=True)
        db.session.add(guarita)
        db.session.flush()
        mensagem = f"Guarita criada: {nome} (ID {guarita.id})."
        flash("Guarita criada.", "success")

    if usuario:
        _registrar_auditoria(usuario, mensagem)
    db.session.commit()
    return redirect(url_for("admin_portaria_configuracoes"))


@admin_required
def admin_portaria_guarita_toggle(guarita_id):
    from app.routes import _registrar_auditoria

    condominio_id = condominio_id_obrigatorio()
    guarita = _guarita_do_tenant(guarita_id, condominio_id)
    guarita.ativa = not bool(guarita.ativa)
    usuario = get_current_user()
    if usuario:
        estado = "ativada" if guarita.ativa else "desativada"
        _registrar_auditoria(
            usuario, f"Guarita {estado}: {guarita.nome} (ID {guarita.id})."
        )
    db.session.commit()
    flash(
        f"Guarita «{guarita.nome}» {'ativada' if guarita.ativa else 'desativada'}.",
        "success",
    )
    return redirect(url_for("admin_portaria_configuracoes"))


@admin_required
def admin_portaria_checklist_salvar():
    from app.routes import _registrar_auditoria

    condominio_id = condominio_id_obrigatorio()
    nome_item = (request.form.get("nome_item") or "").strip()
    tipo_resposta = (request.form.get("tipo_resposta") or "").strip()
    guarita_id = request.form.get("guarita_id", type=int)
    if not nome_item or len(nome_item) > 120:
        flash("Informe o nome do item com até 120 caracteres.", "danger")
        return redirect(url_for("admin_portaria_configuracoes"))
    if tipo_resposta not in TipoRespostaChecklist.CHOICES:
        flash("Tipo de resposta inválido.", "danger")
        return redirect(url_for("admin_portaria_configuracoes"))
    if not guarita_id:
        flash("Selecione a guarita do item de checklist.", "danger")
        return redirect(url_for("admin_portaria_configuracoes"))

    guarita = _guarita_do_tenant(guarita_id, condominio_id)

    item_id = request.form.get("item_id", type=int)
    usuario = get_current_user()
    if item_id:
        item = _item_checklist_do_tenant(item_id, condominio_id)
        item.nome_item = nome_item
        item.tipo_resposta = tipo_resposta
        item.guarita_id = guarita.id
        mensagem = (
            f"Item de checklist atualizado: {nome_item} "
            f"(guarita {guarita.nome}, ID {item.id})."
        )
        flash("Item de checklist atualizado.", "success")
    else:
        item = ItemChecklist(
            nome_item=nome_item,
            tipo_resposta=tipo_resposta,
            condominio_id=condominio_id,
            guarita_id=guarita.id,
            ativo=True,
        )
        db.session.add(item)
        db.session.flush()
        mensagem = (
            f"Item de checklist criado: {nome_item} "
            f"(guarita {guarita.nome}, ID {item.id})."
        )
        flash("Item de checklist criado.", "success")

    if usuario:
        _registrar_auditoria(usuario, mensagem)
    db.session.commit()
    return redirect(url_for("admin_portaria_configuracoes"))


@admin_required
def admin_portaria_checklist_toggle(item_id):
    from app.routes import _registrar_auditoria

    condominio_id = condominio_id_obrigatorio()
    item = _item_checklist_do_tenant(item_id, condominio_id)
    item.ativo = not bool(item.ativo)
    usuario = get_current_user()
    if usuario:
        estado = "ativado" if item.ativo else "desativado"
        _registrar_auditoria(
            usuario, f"Item de checklist {estado}: {item.nome_item} (ID {item.id})."
        )
    db.session.commit()
    flash(
        f"Item «{item.nome_item}» {'ativado' if item.ativo else 'desativado'}.",
        "success",
    )
    return redirect(url_for("admin_portaria_configuracoes"))


@admin_required
def admin_alterar_senha_usuario(usuario_id):
    from app.routes import _registrar_auditoria, _usuario_do_tenant

    usuario_logado = get_current_user()
    condominio_id = condominio_id_obrigatorio(usuario_logado)
    condominio_sessao = session.get("condominio_id")

    if not condominio_id or condominio_id != condominio_sessao:
        flash("Acesso negado.", "danger")
        return _redirect_equipe_acessos()

    usuario_alvo = _usuario_do_tenant(usuario_id, condominio_id)
    if usuario_alvo.condominio_id != condominio_sessao:
        flash("Acesso negado.", "danger")
        return _redirect_equipe_acessos()

    if usuario_alvo.id == usuario_logado.id:
        flash("Você não pode alterar a própria senha por esta tela.", "danger")
        return _redirect_equipe_acessos()

    if usuario_alvo.role not in (Role.ASSISTENTE, Role.SINDICO, Role.PORTEIRO):
        flash(
            "Apenas senhas de assistente, síndico ou porteiro podem ser alteradas aqui.",
            "warning",
        )
        return _redirect_equipe_acessos()

    nova_senha = request.form.get("nova_senha", "").strip()
    if not nova_senha or len(nova_senha) < 6:
        flash("A nova senha deve ter ao menos 6 caracteres.", "danger")
        return _redirect_equipe_acessos()

    usuario_alvo.set_password(nova_senha)
    _registrar_auditoria(
        usuario_logado,
        f"Senha do usuário '{usuario_alvo.username}' redefinida pelo administrador "
        f"'{usuario_logado.username}'.",
    )
    db.session.commit()
    flash(f"Senha de '{usuario_alvo.username}' atualizada com sucesso.", "success")
    return _redirect_equipe_acessos()


@admin_required
def admin_salvar_proprietario(unidade_id):
    from app.routes import _unidade_do_tenant

    unidade = _unidade_do_tenant(unidade_id, condominio_id_obrigatorio())
    unidade.proprietario_nome = request.form.get("proprietario_nome", "").strip() or None
    unidade.proprietario_cpf = request.form.get("proprietario_cpf", "").strip() or None
    unidade.proprietario_telefone = (
        request.form.get("proprietario_telefone", "").strip() or None
    )
    unidade.proprietario_email = request.form.get("proprietario_email", "").strip() or None
    db.session.commit()
    flash(
        f"Dados do proprietário da unidade Bloco {unidade.bloco}, "
        f"Apto {unidade.apartamento} salvos com sucesso.",
        "success",
    )
    return redirect(url_for("admin_index"))


def _blocos_residenciais():
    from app.utils import get_blocos

    return list(get_blocos())


def _ler_escopo_sindico_form():
    """Retorna ('*', True), ('1,2', True) ou (None, False) se nada foi marcado."""
    validos = _blocos_residenciais()
    if request.form.get("escopo_todos") == "1":
        return "*", True
    escolhidos = []
    for codigo in request.form.getlist("blocos_escopo"):
        codigo = str(codigo).strip()
        if codigo in validos and codigo not in escolhidos:
            escolhidos.append(codigo)
    if not escolhidos:
        return None, False
    return ",".join(escolhidos), True


def _ler_permissoes_sindico_form():
    return (
        request.form.get("perm_portaria") == "1",
        request.form.get("perm_reservas_geral") == "1",
        request.form.get("perm_configuracoes") == "1",
    )


def _sincronizar_agrupamentos_sindico(usuario):
    """Espelha blocos_escopo em SindicoAgrupamento (um registro por bloco)."""
    SindicoAgrupamento.query.filter_by(
        usuario_id=usuario.id,
        condominio_id=usuario.condominio_id,
    ).delete(synchronize_session=False)
    codigos = usuario.get_blocos_permitidos()
    if codigos is None:
        codigos = _blocos_residenciais()
    for codigo in codigos:
        db.session.add(
            SindicoAgrupamento(
                usuario_id=usuario.id,
                condominio_id=usuario.condominio_id,
                nome_agrupamento=codigo,
            )
        )


def _aplicar_escopo_sindico(usuario):
    escopo, ok = _ler_escopo_sindico_form()
    if not ok:
        return "Selecione ao menos um bloco ou marque Todos os Blocos (Síndico Geral)."
    usuario.blocos_escopo = escopo
    (
        usuario.perm_portaria,
        usuario.perm_reservas_geral,
        usuario.perm_configuracoes,
    ) = _ler_permissoes_sindico_form()
    _sincronizar_agrupamentos_sindico(usuario)
    return None


def _bloquear_sindico_sem_configuracao():
    usuario = get_current_user()
    if usuario and usuario.role == Role.SINDICO and not usuario.perm_configuracoes:
        flash(
            "Seu acesso de síndico não inclui configurações e setores administrativos.",
            "warning",
        )
        return redirect(url_for("sindico_dashboard"))
    return None


@admin_required
def admin_criar_usuario():
    from app.routes import _registrar_auditoria

    usuario_logado = get_current_user()
    condominio_id = condominio_id_obrigatorio(usuario_logado)
    blocos = _blocos_residenciais()

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        senha = request.form.get("senha", "")
        tipo_acesso = request.form.get("tipo_acesso", "").strip()

        if not username:
            flash("Informe o login do usuário.", "danger")
            return render_template("criar_usuario.html", blocos=blocos)
        if len(senha) < 6:
            flash("A senha deve ter ao menos 6 caracteres.", "danger")
            return render_template("criar_usuario.html", blocos=blocos)

        mapeamento_tipo = {
            "assistente": Role.ASSISTENTE,
            "sindico": Role.SINDICO,
            "porteiro": Role.PORTEIRO,
        }
        role = mapeamento_tipo.get(tipo_acesso)
        if not role:
            flash("Tipo de acesso inválido.", "danger")
            return render_template("criar_usuario.html", blocos=blocos)

        if Usuario.query.filter_by(username=username).first():
            flash("Já existe um usuário com esse login.", "warning")
            return render_template("criar_usuario.html", blocos=blocos)

        novo_usuario = Usuario(
            username=username,
            role=role,
            condominio_id=condominio_id,
            perm_portaria=False,
            perm_reservas_geral=False,
            perm_configuracoes=False,
        )
        novo_usuario.set_password(senha)
        db.session.add(novo_usuario)
        db.session.flush()

        if role == Role.SINDICO:
            erro = _aplicar_escopo_sindico(novo_usuario)
            if erro:
                db.session.rollback()
                flash(erro, "danger")
                return render_template("criar_usuario.html", blocos=blocos)

        _registrar_auditoria(
            usuario_logado,
            f"Criou acesso de {role} '{username}'.",
        )
        db.session.commit()

        flash("Usuário criado com sucesso.", "success")
        return _redirect_equipe_acessos()

    return render_template("criar_usuario.html", blocos=blocos)


@admin_required
def admin_editar_escopo_sindico(usuario_id):
    from app.routes import _registrar_auditoria, _usuario_do_tenant

    usuario_logado = get_current_user()
    condominio_id = condominio_id_obrigatorio(usuario_logado)
    alvo = _usuario_do_tenant(usuario_id, condominio_id)
    if alvo.role != Role.SINDICO:
        flash("O escopo de blocos vale apenas para síndicos.", "warning")
        return _redirect_equipe_acessos()

    erro = _aplicar_escopo_sindico(alvo)
    if erro:
        flash(erro, "danger")
        return _redirect_equipe_acessos()

    _registrar_auditoria(
        usuario_logado,
        f"Atualizou escopo e permissões do síndico '{alvo.username}'.",
    )
    db.session.commit()
    flash("Escopo e permissões do síndico atualizados.", "success")
    return _redirect_equipe_acessos()


@admin_required
def admin_excluir_usuario(usuario_id):
    from app.routes import _registrar_auditoria, _usuario_do_tenant

    usuario_logado = get_current_user()
    condominio_id = condominio_id_obrigatorio(usuario_logado)
    usuario_alvo = _usuario_do_tenant(usuario_id, condominio_id)

    if usuario_alvo.id == usuario_logado.id:
        flash("Você não pode excluir o próprio acesso.", "danger")
        return _redirect_equipe_acessos()

    if usuario_alvo.role not in (Role.ASSISTENTE, Role.SINDICO, Role.PORTEIRO):
        flash(
            "Apenas acessos de assistente, síndico ou porteiro podem ser revogados aqui.",
            "warning",
        )
        return _redirect_equipe_acessos()

    username_alvo = usuario_alvo.username
    role_alvo = usuario_alvo.role
    SindicoAgrupamento.query.filter_by(
        usuario_id=usuario_alvo.id, condominio_id=condominio_id
    ).delete()
    db.session.delete(usuario_alvo)
    _registrar_auditoria(
        usuario_logado,
        f"Acesso do {role_alvo} '{username_alvo}' foi revogado por "
        f"'{usuario_logado.username}'.",
    )
    db.session.commit()

    flash(f"Acesso de '{username_alvo}' revogado com sucesso.", "success")
    return _redirect_equipe_acessos()


@admin_or_sindico_required
def admin_ocorrencias():
    """Kanban de ocorrências do condomínio do admin/síndico logado."""
    from app.routes import _recorte_blocos_consulta, _redirect_login_tenant

    usuario = get_current_user()
    condominio_id = condominio_id_obrigatorio(usuario)
    if not condominio_id:
        flash("Conta sem condomínio vinculado.", "danger")
        return _redirect_login_tenant()

    blocos_opcoes = []
    bloco_filtro = ""
    query = Ocorrencia.query.filter_by(condominio_id=condominio_id)
    if usuario.role == Role.SINDICO:
        blocos_opcoes, blocos_sindico, bloco_filtro = _recorte_blocos_consulta(usuario)
        query = query.join(Unidade, Ocorrencia.unidade_id == Unidade.id).filter(
            Unidade.bloco.in_(blocos_sindico or [""])
        )

    ocorrencias = query.order_by(Ocorrencia.created_at.desc()).all()
    colunas = {
        StatusOcorrencia.ABERTO: [],
        StatusOcorrencia.EM_ANDAMENTO: [],
        StatusOcorrencia.RESOLVIDO: [],
    }
    for item in ocorrencias:
        colunas.setdefault(item.status, []).append(item)

    return render_template(
        "admin/ocorrencias_kanban.html",
        colunas=colunas,
        status_ocorrencia=StatusOcorrencia,
        current_user=usuario,
        blocos_opcoes=blocos_opcoes,
        bloco_filtro=bloco_filtro,
    )


@admin_or_sindico_required
def admin_ocorrencias_atualizar_status(id):
    """Avança ou retrocede o status com proteção Anti-IDOR por condominio_id."""
    from app.routes import (
        _ocorrencia_do_tenant,
        _redirect_login_tenant,
        _registrar_auditoria,
        _sindico_gerencia_bloco,
    )

    usuario = get_current_user()
    condominio_id = condominio_id_obrigatorio(usuario)
    if not condominio_id:
        flash("Conta sem condomínio vinculado.", "danger")
        return _redirect_login_tenant()

    novo_status = (request.form.get("status") or "").strip()
    if novo_status not in StatusOcorrencia.CHOICES:
        flash("Status inválido.", "danger")
        return redirect(url_for("admin_ocorrencias"))

    ocorrencia = _ocorrencia_do_tenant(id, condominio_id)
    if not ocorrencia:
        flash("Ocorrência não encontrada.", "danger")
        return redirect(url_for("admin_ocorrencias"))

    if usuario.role == Role.SINDICO and not _sindico_gerencia_bloco(
        usuario, ocorrencia.unidade.bloco
    ):
        flash("Você não tem permissão para esta ocorrência.", "danger")
        return redirect(url_for("admin_ocorrencias"))

    status_anterior = ocorrencia.status
    if status_anterior == novo_status:
        return redirect(url_for("admin_ocorrencias"))

    ocorrencia.status = novo_status
    _registrar_auditoria(
        usuario,
        (
            f"Ocorrência #{ocorrencia.id} ({ocorrencia.titulo}) "
            f"alterada de '{status_anterior}' para '{novo_status}'."
        ),
    )
    db.session.commit()
    flash(f"Status atualizado para {novo_status}.", "success")
    return redirect(url_for("admin_ocorrencias"))


@admin_or_assistente_required
def admin_mudancas():
    from app.routes import _agendamento_do_tenant, _registrar_auditoria, _unidade_do_tenant, _validar_data_mudanca

    usuario = get_current_user()
    condominio_id = condominio_id_obrigatorio(usuario)

    if request.method == "POST":
        acao = request.form.get("acao", "").strip()

        if acao == "criar":
            unidade_id = request.form.get("unidade_id", type=int)
            tipo = request.form.get("tipo", "").strip()
            data_str = request.form.get("data_mudanca", "").strip()
            observacoes = request.form.get("observacoes", "").strip() or None

            if not unidade_id:
                flash("Selecione uma unidade válida.", "danger")
                return redirect(url_for("admin_mudancas"))
            unidade = _unidade_do_tenant(unidade_id, condominio_id)

            if tipo not in StatusAgendamentoMudanca.TIPOS:
                flash("Selecione o tipo da mudança (Entrada ou Saída).", "danger")
                return redirect(url_for("admin_mudancas"))

            try:
                data_mudanca = datetime.strptime(data_str, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                flash("Informe uma data válida para a mudança.", "danger")
                return redirect(url_for("admin_mudancas"))

            erro_data = _validar_data_mudanca(data_mudanca)
            if erro_data:
                flash(erro_data, "danger")
                return redirect(url_for("admin_mudancas"))

            agendamento = AgendamentoMudanca(
                unidade_id=unidade.id,
                tipo=tipo,
                data_mudanca=data_mudanca,
                status=StatusAgendamentoMudanca.APROVADA,
                observacoes=observacoes,
                condominio_id=condominio_id,
            )
            db.session.add(agendamento)
            _registrar_auditoria(
                usuario,
                f"Administração cadastrou mudança {tipo} já aprovada para a unidade "
                f"{unidade.identificador} em {data_mudanca.strftime('%d/%m/%Y')}.",
            )
            db.session.commit()
            flash(
                f"Mudança de {tipo.lower()} cadastrada e aprovada para "
                f"{unidade.identificador}.",
                "success",
            )
            return redirect(url_for("admin_mudancas"))

        agendamento_id = request.form.get("agendamento_id", type=int)
        if not agendamento_id:
            flash("Solicitação não encontrada.", "danger")
            return redirect(url_for("admin_mudancas"))
        agendamento = _agendamento_do_tenant(agendamento_id, condominio_id)

        if agendamento.status != StatusAgendamentoMudanca.PENDENTE_ADMINISTRACAO:
            flash(
                "Esta solicitação não está pendente de aprovação da administração.",
                "warning",
            )
            return redirect(url_for("admin_mudancas"))

        if acao == "aprovar":
            # UPDATE condicional: fecha a janela de corrida entre a checagem
            # acima e o commit (duplo clique / aprovar+rejeitar concorrentes).
            resultado = db.session.execute(
                text(
                    "UPDATE agendamentos_mudanca SET status = :novo "
                    "WHERE id = :id AND status = :esperado"
                ),
                {
                    "novo": StatusAgendamentoMudanca.APROVADA,
                    "id": agendamento.id,
                    "esperado": StatusAgendamentoMudanca.PENDENTE_ADMINISTRACAO,
                },
            )
            if resultado.rowcount == 0:
                db.session.rollback()
                flash("Esta solicitação já foi processada por outra ação.", "warning")
                return redirect(url_for("admin_mudancas"))
            _registrar_auditoria(
                usuario,
                f"Administração aprovou definitivamente mudança {agendamento.tipo} "
                f"da unidade {agendamento.unidade.identificador} em "
                f"{agendamento.data_mudanca.strftime('%d/%m/%Y')}.",
            )
            db.session.commit()
            flash("Mudança aprovada definitivamente.", "success")
        elif acao == "rejeitar":
            motivo = request.form.get("motivo_rejeicao", "").strip()
            if not motivo:
                flash("Informe o motivo da rejeição.", "danger")
                return redirect(url_for("admin_mudancas"))
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
                    "esperado": StatusAgendamentoMudanca.PENDENTE_ADMINISTRACAO,
                },
            )
            if resultado.rowcount == 0:
                db.session.rollback()
                flash("Esta solicitação já foi processada por outra ação.", "warning")
                return redirect(url_for("admin_mudancas"))
            _registrar_auditoria(
                usuario,
                f"Administração rejeitou mudança {agendamento.tipo} da unidade "
                f"{agendamento.unidade.identificador}. Motivo: {motivo}",
            )
            db.session.commit()
            flash("Solicitação de mudança rejeitada.", "info")
        else:
            flash("Ação inválida.", "danger")

        return redirect(url_for("admin_mudancas"))

    pendentes = (
        AgendamentoMudanca.query.filter_by(
            condominio_id=condominio_id,
            status=StatusAgendamentoMudanca.PENDENTE_ADMINISTRACAO,
        )
        .order_by(AgendamentoMudanca.data_mudanca.asc())
        .all()
    )
    historico = (
        AgendamentoMudanca.query.filter_by(condominio_id=condominio_id)
        .order_by(AgendamentoMudanca.data_solicitacao.desc())
        .all()
    )
    unidades = (
        Unidade.query.filter_by(condominio_id=condominio_id)
        .order_by(Unidade.bloco, Unidade.apartamento)
        .all()
    )
    return render_template(
        "admin_mudancas.html",
        pendentes=pendentes,
        historico=historico,
        unidades=unidades,
        current_user=usuario,
    )


@admin_required
def admin_controle_acesso():
    """Credenciais de acesso (RFID, biometria, controle, cartão) do condomínio."""
    condominio_id = condominio_id_obrigatorio()
    credenciais = (
        CredencialAcesso.query.join(Pessoa, CredencialAcesso.morador_id == Pessoa.id)
        .join(Unidade, Pessoa.unidade_id == Unidade.id)
        .filter(CredencialAcesso.condominio_id == condominio_id)
        .order_by(CredencialAcesso.ativa.desc(), Pessoa.nome_completo)
        .all()
    )
    moradores = (
        Pessoa.query.join(Unidade, Pessoa.unidade_id == Unidade.id)
        .filter(
            Unidade.condominio_id == condominio_id,
            Pessoa.status == StatusPessoa.APROVADO,
            Pessoa.eh_morador.is_(True),
            Unidade.status.in_((StatusUnidade.APROVADA, StatusUnidade.REGISTRADA)),
        )
        .order_by(Unidade.bloco, Unidade.apartamento, Pessoa.nome_completo)
        .all()
    )
    grupos = []
    indice = {}
    for morador in moradores:
        grupo = indice.get(morador.unidade_id)
        if grupo is None:
            grupo = {"unidade": morador.unidade, "moradores": []}
            indice[morador.unidade_id] = grupo
            grupos.append(grupo)
        grupo["moradores"].append(morador)
    return render_template(
        "admin/controle_acesso.html",
        credenciais=credenciais,
        moradores_por_unidade=grupos,
        tipos_credencial=CredencialAcesso.TIPOS,
    )


@admin_required
def admin_controle_acesso_salvar():
    from app.routes import _registrar_auditoria

    condominio_id = condominio_id_obrigatorio()
    morador_id = request.form.get("morador_id", type=int)
    tipo = (request.form.get("tipo") or "").strip()
    codigo = (request.form.get("codigo_identificador") or "").strip()
    if not morador_id or tipo not in CredencialAcesso.TIPOS or not codigo:
        flash("Informe o morador, o tipo e o código da credencial.", "danger")
        return redirect(url_for("admin_controle_acesso"))

    morador = (
        Pessoa.query.join(Unidade, Pessoa.unidade_id == Unidade.id)
        .filter(
            Pessoa.id == morador_id,
            Unidade.condominio_id == condominio_id,
            Pessoa.status == StatusPessoa.APROVADO,
        )
        .first()
    )
    if morador is None:
        flash("Morador não encontrado neste condomínio.", "danger")
        return redirect(url_for("admin_controle_acesso"))
    if not morador.eh_morador:
        flash(
            "Somente quem reside na unidade pode receber credencial de acesso.",
            "warning",
        )
        return redirect(url_for("admin_controle_acesso"))

    duplicada = CredencialAcesso.query.filter_by(
        condominio_id=condominio_id,
        codigo_identificador=codigo,
        ativa=True,
    ).first()
    if duplicada is not None:
        flash("Já existe uma credencial ativa com este código neste condomínio.", "warning")
        return redirect(url_for("admin_controle_acesso"))

    credencial = CredencialAcesso(
        tipo=tipo,
        codigo_identificador=codigo,
        morador_id=morador.id,
        condominio_id=condominio_id,
        ativa=True,
    )
    db.session.add(credencial)
    _registrar_auditoria(
        get_current_user(),
        f"Credencial emitida: {tipo} {codigo} para {morador.nome_completo} "
        f"({morador.unidade.bloco}/{morador.unidade.apartamento}).",
    )
    db.session.commit()
    flash("Credencial cadastrada.", "success")
    return redirect(url_for("admin_controle_acesso"))


@admin_required
def admin_controle_acesso_revogar(credencial_id):
    from app.routes import _registrar_auditoria

    condominio_id = condominio_id_obrigatorio()
    credencial = CredencialAcesso.query.filter_by(
        id=credencial_id,
        condominio_id=condominio_id,
    ).first_or_404()
    if not credencial.ativa:
        flash("Esta credencial já está revogada.", "info")
        return redirect(url_for("admin_controle_acesso"))

    credencial.ativa = False
    morador = credencial.morador
    _registrar_auditoria(
        get_current_user(),
        f"Credencial revogada: {credencial.tipo} {credencial.codigo_identificador} "
        f"de {morador.nome_completo}.",
    )
    db.session.commit()
    flash("Credencial revogada. O registro permanece para auditoria.", "success")
    return redirect(url_for("admin_controle_acesso"))


def encomendas_pendentes_setores(condominio_id):
    """Pacotes ainda na portaria destinados a setores internos do condomínio."""
    if not condominio_id:
        return []
    return (
        Encomenda.query.join(Unidade, Encomenda.unidade_id == Unidade.id)
        .filter(
            Encomenda.condominio_id == condominio_id,
            Encomenda.status == StatusEncomenda.PENDENTE,
            Unidade.condominio_id == condominio_id,
            Unidade.eh_setor_interno.is_(True),
        )
        .order_by(Encomenda.data_recebimento.asc())
        .all()
    )


def _setor_interno_do_tenant(setor_id, condominio_id):
    return Unidade.query.filter_by(
        id=setor_id,
        condominio_id=condominio_id,
        eh_setor_interno=True,
    ).first()


def _digitos_contato(valor):
    return "".join(ch for ch in (valor or "") if ch.isdigit())


def _email_contato_valido(email):
    if not email:
        return True
    if len(email) > 120 or "@" not in email:
        return False
    local, _, dominio = email.partition("@")
    return bool(local) and "." in dominio and " " not in email


@admin_or_sindico_required
def admin_setores():
    bloqueio = _bloquear_sindico_sem_configuracao()
    if bloqueio:
        return bloqueio
    condominio_id = condominio_id_obrigatorio()
    setores = (
        Unidade.query.filter_by(
            condominio_id=condominio_id,
            eh_setor_interno=True,
        )
        .order_by(Unidade.apartamento.asc())
        .all()
    )
    for setor in setores:
        setor.contatos_ordenados = (
            setor.pessoas.order_by(
                Pessoa.is_responsavel.desc(),
                Pessoa.nome_completo.asc(),
            ).all()
        )
    return render_template(
        "admin/setores.html",
        setores=setores,
        encomendas_setores=encomendas_pendentes_setores(condominio_id),
        setor_principal="Administração",
        current_user=get_current_user(),
    )


@admin_or_sindico_required
def admin_setores_criar():
    bloqueio = _bloquear_sindico_sem_configuracao()
    if bloqueio:
        return bloqueio
    from secrets import token_urlsafe

    from werkzeug.security import generate_password_hash

    from app.routes import _registrar_auditoria
    from app.utils import BLOCO_SETORES

    condominio_id = condominio_id_obrigatorio()
    nome = " ".join((request.form.get("nome") or "").split())
    if not nome:
        flash("Informe o nome do setor.", "danger")
        return redirect(url_for("admin_setores"))
    if len(nome) > 20:
        flash("O nome do setor pode ter no máximo 20 caracteres.", "danger")
        return redirect(url_for("admin_setores"))

    existentes = Unidade.query.filter_by(
        condominio_id=condominio_id,
        bloco=BLOCO_SETORES,
        eh_setor_interno=True,
    ).all()
    if any(setor.apartamento.casefold() == nome.casefold() for setor in existentes):
        flash("Já existe um setor com esse nome neste condomínio.", "warning")
        return redirect(url_for("admin_setores"))

    setor = Unidade(
        condominio_id=condominio_id,
        bloco=BLOCO_SETORES,
        apartamento=nome,
        password_hash=generate_password_hash(token_urlsafe(32)),
        status=StatusUnidade.APROVADA,
        eh_setor_interno=True,
    )
    db.session.add(setor)
    _registrar_auditoria(
        get_current_user(),
        f"Setor interno criado: {nome}.",
    )
    db.session.commit()
    flash(f"Setor {nome} criado.", "success")
    return redirect(url_for("admin_setores"))


@admin_or_sindico_required
def admin_setores_excluir(setor_id):
    bloqueio = _bloquear_sindico_sem_configuracao()
    if bloqueio:
        return bloqueio
    from sqlalchemy.exc import IntegrityError

    from app.routes import _registrar_auditoria
    from app.utils import SETOR_ADMINISTRACAO

    condominio_id = condominio_id_obrigatorio()
    setor = _setor_interno_do_tenant(setor_id, condominio_id)
    if setor is None:
        flash("Setor não encontrado.", "danger")
        return redirect(url_for("admin_setores"))
    if setor.apartamento.casefold() == SETOR_ADMINISTRACAO.casefold():
        flash("O setor Administração é permanente e não pode ser excluído.", "warning")
        return redirect(url_for("admin_setores"))

    encomendas = Encomenda.query.filter_by(
        condominio_id=condominio_id,
        unidade_id=setor.id,
    )
    if encomendas.filter_by(status=StatusEncomenda.PENDENTE).first():
        flash(
            "Não é possível excluir: há encomendas pendentes de retirada neste setor.",
            "warning",
        )
        return redirect(url_for("admin_setores"))
    if encomendas.first():
        flash(
            "Não é possível excluir: este setor já tem encomendas registradas na portaria.",
            "warning",
        )
        return redirect(url_for("admin_setores"))

    nome = setor.apartamento
    db.session.delete(setor)
    try:
        _registrar_auditoria(
            get_current_user(),
            f"Setor interno excluído: {nome}.",
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash(
            "Não é possível excluir: este setor ainda tem registros vinculados.",
            "warning",
        )
        return redirect(url_for("admin_setores"))
    flash(f"Setor {nome} excluído.", "success")
    return redirect(url_for("admin_setores"))


@admin_or_sindico_required
def admin_setores_contato_salvar(setor_id):
    bloqueio = _bloquear_sindico_sem_configuracao()
    if bloqueio:
        return bloqueio
    from app.routes import _registrar_auditoria

    condominio_id = condominio_id_obrigatorio()
    setor = _setor_interno_do_tenant(setor_id, condominio_id)
    if setor is None:
        flash("Setor não encontrado.", "danger")
        return redirect(url_for("admin_setores"))

    nome = " ".join((request.form.get("nome") or "").split())
    telefone = " ".join((request.form.get("telefone") or "").split())
    email = (request.form.get("email") or "").strip()
    principal = request.form.get("responsavel") == "1"
    pessoa_id = (request.form.get("pessoa_id") or "").strip()

    if not nome or len(nome) > 200:
        flash("Informe o nome ou cargo do contato.", "danger")
        return redirect(url_for("admin_setores"))
    if len(_digitos_contato(telefone)) < 10 or len(telefone) > 20:
        flash("Informe um telefone ou WhatsApp com DDD.", "danger")
        return redirect(url_for("admin_setores"))
    if not _email_contato_valido(email):
        flash("E-mail inválido.", "danger")
        return redirect(url_for("admin_setores"))

    pessoa = None
    if pessoa_id:
        if not pessoa_id.isdigit():
            flash("Contato não encontrado.", "danger")
            return redirect(url_for("admin_setores"))
        pessoa = Pessoa.query.filter_by(id=int(pessoa_id), unidade_id=setor.id).first()
        if pessoa is None:
            flash("Contato não encontrado.", "danger")
            return redirect(url_for("admin_setores"))

    if principal:
        for outro in setor.pessoas.all():
            if pessoa is None or outro.id != pessoa.id:
                outro.is_responsavel = False

    if pessoa is None:
        pessoa = Pessoa(
            unidade_id=setor.id,
            cpf="",
            vinculo=VinculoPessoa.MORADOR,
            data_nascimento=None,
            eh_proprietario=False,
        )
        db.session.add(pessoa)
        acao = "adicionado"
    else:
        acao = "atualizado"

    pessoa.nome_completo = nome
    pessoa.telefone = telefone
    pessoa.email = email or None
    pessoa.is_responsavel = principal
    pessoa.eh_morador = True
    pessoa.autoriza_interfone = True
    pessoa.eh_proprietario = False
    pessoa.vinculo = VinculoPessoa.MORADOR
    pessoa.status = StatusPessoa.APROVADO
    pessoa.data_nascimento = None
    pessoa.cpf = pessoa.cpf or ""

    _registrar_auditoria(
        get_current_user(),
        f"Contato {acao} no setor {setor.apartamento}: {nome}.",
    )
    db.session.commit()
    flash(f"Contato {acao} em {setor.apartamento}.", "success")
    return redirect(url_for("admin_setores"))


@admin_or_sindico_required
def admin_setores_contato_excluir(setor_id, pessoa_id):
    bloqueio = _bloquear_sindico_sem_configuracao()
    if bloqueio:
        return bloqueio
    from app.routes import _registrar_auditoria

    condominio_id = condominio_id_obrigatorio()
    setor = _setor_interno_do_tenant(setor_id, condominio_id)
    if setor is None:
        flash("Setor não encontrado.", "danger")
        return redirect(url_for("admin_setores"))
    pessoa = Pessoa.query.filter_by(id=pessoa_id, unidade_id=setor.id).first()
    if pessoa is None:
        flash("Contato não encontrado.", "danger")
        return redirect(url_for("admin_setores"))
    nome = pessoa.nome_completo
    db.session.delete(pessoa)
    _registrar_auditoria(
        get_current_user(),
        f"Contato removido do setor {setor.apartamento}: {nome}.",
    )
    db.session.commit()
    flash(f"Contato {nome} removido.", "success")
    return redirect(url_for("admin_setores"))


PLANOS_CONDOMINIO = ("Essencial", "Profissional", "Enterprise")
FUSOS_CONDOMINIO = (
    "America/Sao_Paulo",
    "America/Bahia",
    "America/Fortaleza",
    "America/Recife",
    "America/Belem",
    "America/Manaus",
    "America/Cuiaba",
    "America/Rio_Branco",
    "America/Noronha",
)
TIPOS_DIVISAO_CONDOMINIO = (
    ("bloco_apto", "Blocos e apartamentos"),
    ("quadra_casa", "Quadras e casas"),
    ("torre_unica", "Torre única"),
)
_NOME_CONTATO_ADMINISTRACAO = "Administração Geral"


def _condominio_para_dados(usuario):
    """Tenant do formulário. Admin e síndico não escolhem outro condomínio."""
    if usuario.role == Role.SUPERADMIN:
        bruto = (request.values.get("id") or "").strip()
        if not bruto.isdigit():
            return None
        return db.session.get(Condominio, int(bruto))
    if not usuario.condominio_id:
        return None
    return db.session.get(Condominio, usuario.condominio_id)


def _texto_limitado(nome, limite):
    valor = " ".join((request.form.get(nome) or "").split())
    if len(valor) > limite:
        return None, f"O campo {nome.replace('_', ' ')} passa de {limite} caracteres."
    return (valor or None), None


def _validar_telefone_condominio(valor, rotulo):
    if not valor:
        return None
    if len(_digitos_contato(valor)) < 10:
        return f"Informe o {rotulo} com DDD."
    return None


def _sincronizar_contato_administracao(condominio):
    """Cria ou atualiza o contato institucional do setor Administração.

    Não mexe no setor quando já existem outros contatos e o institucional
    ainda não foi criado.
    """
    from app import garantir_setor_administracao

    telefone = (condominio.telefone_whatsapp or condominio.telefone_fixo or "").strip()
    email = (condominio.email_contato or "").strip()
    if not telefone or not email:
        return "sem_dados"
    if len(telefone) > 20:
        return "telefone_longo"

    setor = garantir_setor_administracao(condominio)
    if setor is None:
        return "sem_setor"
    pessoas = list(setor.pessoas)
    institucional = next(
        (
            pessoa
            for pessoa in pessoas
            if pessoa.nome_completo == _NOME_CONTATO_ADMINISTRACAO
        ),
        None,
    )
    if pessoas and institucional is None:
        return "ignorado"

    if institucional is None:
        institucional = Pessoa(
            unidade_id=setor.id,
            cpf="",
            vinculo=VinculoPessoa.MORADOR,
            data_nascimento=None,
            eh_proprietario=False,
        )
        db.session.add(institucional)
        acao = "criado"
    else:
        acao = "atualizado"

    for outro in pessoas:
        if outro is not institucional:
            outro.is_responsavel = False
    institucional.nome_completo = _NOME_CONTATO_ADMINISTRACAO
    institucional.telefone = telefone
    institucional.email = email
    institucional.is_responsavel = True
    institucional.eh_morador = True
    institucional.autoriza_interfone = True
    institucional.eh_proprietario = False
    institucional.vinculo = VinculoPessoa.MORADOR
    institucional.status = StatusPessoa.APROVADO
    institucional.cpf = institucional.cpf or ""
    return acao


def _aplicar_dados_condominio(condominio, usuario):
    """Grava só os campos que o papel pode alterar. Devolve mensagem de erro."""
    papel = usuario.role
    editar_saas = papel == Role.SUPERADMIN
    editar_operacao = papel in (Role.SUPERADMIN, Role.ADMIN)
    editar_contatos = papel in (Role.SUPERADMIN, Role.ADMIN, Role.SINDICO)

    if editar_operacao or editar_saas:
        nome, erro = _texto_limitado("nome", 200)
        if erro or not nome:
            return erro or "Informe o nome do estabelecimento."
        condominio.nome = nome
        fuso = (request.form.get("fuso_horario") or "").strip()
        if fuso not in FUSOS_CONDOMINIO:
            return "Selecione um fuso horário válido."
        condominio.fuso_horario = fuso

    if editar_saas:
        razao, erro = _texto_limitado("razao_social", 200)
        if erro:
            return erro
        condominio.razao_social = razao
        cnpj = " ".join((request.form.get("cnpj") or "").split())
        if cnpj and (len(cnpj) > 18 or len(_digitos_contato(cnpj)) != 14):
            return "Informe um CNPJ válido."
        condominio.cnpj = cnpj or None
        slug = normalizar_slug(request.form.get("slug", ""))
        if not validar_slug(slug):
            return "Informe um slug válido (letras minúsculas, números e hifens)."
        ocupado = Condominio.query.filter(
            Condominio.slug == slug,
            Condominio.id != condominio.id,
        ).first()
        if ocupado:
            return "Já existe um condomínio com este slug."
        condominio.slug = slug
        plano = (request.form.get("plano") or "").strip()
        if plano not in PLANOS_CONDOMINIO:
            return "Selecione um plano válido."
        condominio.plano = plano

    if editar_contatos:
        telefone_fixo, erro = _texto_limitado("telefone_fixo", 30)
        if erro:
            return erro
        telefone_whatsapp, erro = _texto_limitado("telefone_whatsapp", 30)
        if erro:
            return erro
        email = (request.form.get("email_contato") or "").strip().lower()
        if not _email_contato_valido(email):
            return "E-mail oficial inválido."
        erro = _validar_telefone_condominio(telefone_fixo, "telefone fixo")
        if erro:
            return erro
        erro = _validar_telefone_condominio(telefone_whatsapp, "WhatsApp")
        if erro:
            return erro
        cep_digitos = _digitos_contato(request.form.get("cep"))
        if cep_digitos and len(cep_digitos) != 8:
            return "Informe um CEP com 8 dígitos."
        uf = (request.form.get("uf") or "").strip().upper()
        if uf and (len(uf) != 2 or not uf.isalpha()):
            return "Informe a UF com 2 letras."
        logradouro, erro = _texto_limitado("logradouro", 200)
        if erro:
            return erro
        numero, erro = _texto_limitado("numero", 20)
        if erro:
            return erro
        complemento, erro = _texto_limitado("complemento", 120)
        if erro:
            return erro
        bairro, erro = _texto_limitado("bairro", 120)
        if erro:
            return erro
        cidade, erro = _texto_limitado("cidade", 120)
        if erro:
            return erro
        condominio.telefone_fixo = telefone_fixo
        condominio.telefone_whatsapp = telefone_whatsapp
        condominio.email_contato = email or None
        condominio.cep = (
            f"{cep_digitos[:5]}-{cep_digitos[5:]}" if cep_digitos else None
        )
        condominio.logradouro = logradouro
        condominio.numero = numero
        condominio.complemento = complemento
        condominio.bairro = bairro
        condominio.cidade = cidade
        condominio.uf = uf or None

    if editar_operacao:
        tipo = (request.form.get("tipo_divisao") or "").strip()
        if tipo not in {item[0] for item in TIPOS_DIVISAO_CONDOMINIO}:
            return "Selecione o tipo de divisão."
        bruto_total = (request.form.get("total_unidades_previsto") or "").strip()
        if not bruto_total:
            total = None
        elif not bruto_total.isdigit() or int(bruto_total) > 100000:
            return "Informe o total de unidades como um número válido."
        else:
            total = int(bruto_total)
        responsavel, erro = _texto_limitado("nome_responsavel_gestao", 200)
        if erro:
            return erro
        horario, erro = _texto_limitado("horario_mudancas", 200)
        if erro:
            return erro
        bruto_mandato = (request.form.get("fim_mandato") or "").strip()
        if not bruto_mandato:
            mandato = None
        else:
            try:
                mandato = datetime.strptime(bruto_mandato, "%Y-%m-%d").date()
            except ValueError:
                return "Informe o fim do mandato como data válida."
        condominio.tipo_divisao = tipo
        condominio.total_unidades_previsto = total
        condominio.nome_responsavel_gestao = responsavel
        condominio.horario_mudancas = horario
        condominio.fim_mandato = mandato
    return None


_LOGO_CONDOMINIO_EXT = {"png", "jpg", "jpeg", "webp"}
_PDF_CONDOMINIO_EXT = {"pdf"}
_LOGO_CONDOMINIO_MAX = 2 * 1024 * 1024
_PDF_CONDOMINIO_MAX = 8 * 1024 * 1024


def _assinatura_imagem(conteudo, extensao):
    if extensao == "png":
        return conteudo.startswith(b"\x89PNG\r\n\x1a\n")
    if extensao in ("jpg", "jpeg"):
        return conteudo.startswith(b"\xff\xd8\xff")
    if extensao == "webp":
        return (
            len(conteudo) >= 12
            and conteudo[:4] == b"RIFF"
            and conteudo[8:12] == b"WEBP"
        )
    return False


def _gravar_upload_condominio(arquivo, pasta, extensoes, prefixo, limite, validador):
    if not arquivo or not arquivo.filename:
        return None, None
    original = secure_filename(arquivo.filename)
    if not original or "." not in original:
        return None, "formato"
    extensao = original.rsplit(".", 1)[-1].lower()
    if extensao not in extensoes:
        return None, "formato"
    conteudo = arquivo.read()
    if not conteudo:
        return None, "vazio"
    if len(conteudo) > limite:
        return None, "tamanho"
    if not validador(conteudo, extensao):
        return None, "formato"
    os.makedirs(pasta, exist_ok=True)
    nome = f"{prefixo}_{secrets.token_hex(8)}.{extensao}"
    with open(os.path.join(pasta, nome), "wb") as saida:
        saida.write(conteudo)
    return nome, None


def _remover_upload_condominio(pasta, nome):
    if not isinstance(nome, str) or not nome:
        return
    base = os.path.basename(nome)
    if base != nome or ".." in base:
        return
    caminho = os.path.join(pasta, base)
    if os.path.isfile(caminho):
        os.remove(caminho)


def _aplicar_arquivos_condominio(condominio, usuario):
    """Substitui logo e PDFs. Síndico e assistente não gravam arquivo."""
    if usuario.role not in (Role.ADMIN, Role.SUPERADMIN):
        return None

    slug = condominio.slug or f"c{condominio.id}"
    pasta_logo = current_app.config["UPLOAD_LOGOS_FOLDER"]
    pasta_doc = current_app.config["UPLOAD_DOCUMENTOS_FOLDER"]
    gravados = []
    especificacoes = (
        (
            "logo",
            "logo_filename",
            pasta_logo,
            _LOGO_CONDOMINIO_EXT,
            _LOGO_CONDOMINIO_MAX,
            _assinatura_imagem,
            f"{slug}_logo",
            "A logo deve ser PNG, JPG ou WEBP de até 2 MB.",
        ),
        (
            "regimento",
            "regimento_filename",
            pasta_doc,
            _PDF_CONDOMINIO_EXT,
            _PDF_CONDOMINIO_MAX,
            lambda conteudo, _ext: conteudo.startswith(b"%PDF"),
            f"{slug}_regimento",
            "O Regimento Interno deve ser um PDF de até 8 MB.",
        ),
        (
            "convencao",
            "convencao_filename",
            pasta_doc,
            _PDF_CONDOMINIO_EXT,
            _PDF_CONDOMINIO_MAX,
            lambda conteudo, _ext: conteudo.startswith(b"%PDF"),
            f"{slug}_convencao",
            "A Convenção deve ser um PDF de até 8 MB.",
        ),
    )
    try:
        for campo, attr, pasta, extensoes, limite, validador, prefixo, mensagem in especificacoes:
            nome, erro = _gravar_upload_condominio(
                request.files.get(campo),
                pasta,
                extensoes,
                prefixo,
                limite,
                validador,
            )
            if erro:
                raise ValueError(mensagem)
            if nome:
                gravados.append((pasta, nome, attr))
        for pasta, nome, attr in gravados:
            _remover_upload_condominio(pasta, getattr(condominio, attr))
            setattr(condominio, attr, nome)
        return None
    except ValueError as exc:
        for pasta, nome, _attr in gravados:
            _remover_upload_condominio(pasta, nome)
        return str(exc)


def admin_condominio():
    usuario = get_current_user()
    if not usuario or usuario.role not in (Role.ADMIN, Role.SINDICO, Role.SUPERADMIN):
        flash("Acesso restrito à administração do condomínio.", "danger")
        return redirect(url_for("index"))
    if usuario.role != Role.SUPERADMIN and not usuario.condominio_id:
        flash("Conta sem condomínio vinculado. Contate a administração.", "danger")
        return redirect(url_for("index"))
    if usuario.role == Role.SINDICO and not usuario.perm_configuracoes:
        flash(
            "Seu acesso de síndico não inclui configurações e setores administrativos.",
            "warning",
        )
        return redirect(url_for("sindico_dashboard"))

    condominio = _condominio_para_dados(usuario)
    if condominio is None:
        flash("Selecione o condomínio na plataforma para editar os dados.", "warning")
        destino = (
            "superadmin_condominios"
            if usuario.role == Role.SUPERADMIN
            else "admin_dashboard"
        )
        return redirect(url_for(destino))

    if request.method == "POST":
        erro = _aplicar_dados_condominio(condominio, usuario)
        if erro:
            flash(erro, "danger")
            return redirect(url_for("admin_condominio", id=condominio.id))
        erro = _aplicar_arquivos_condominio(condominio, usuario)
        if erro:
            db.session.rollback()
            flash(erro, "danger")
            return redirect(url_for("admin_condominio", id=condominio.id))
        sinc = _sincronizar_contato_administracao(condominio)
        db.session.add(
            LogAuditoria(
                usuario_id=usuario.id,
                condominio_id=condominio.id,
                mensagem=f"Atualizou os dados do condomínio '{condominio.nome}'.",
            )
        )
        db.session.commit()
        if sinc == "criado":
            flash(
                "Dados salvos. O WhatsApp da Administração já aparece no interfone.",
                "success",
            )
        elif sinc == "atualizado":
            flash(
                "Dados salvos. O contato Administração Geral do interfone foi atualizado.",
                "success",
            )
        elif sinc == "ignorado":
            flash(
                "Dados salvos. O setor Administração já tem contatos próprios; "
                "o interfone não foi alterado.",
                "info",
            )
        elif sinc == "telefone_longo":
            flash(
                "Dados salvos. O telefone oficial ficou longo demais para o "
                "contato do interfone (máximo de 20 caracteres).",
                "warning",
            )
        else:
            flash("Dados salvos.", "success")
        return redirect(url_for("admin_condominio", id=condominio.id))

    papel = usuario.role
    return render_template(
        "admin/condominio.html",
        current_user=usuario,
        condominio=condominio,
        editar_saas=papel == Role.SUPERADMIN,
        editar_operacao=papel in (Role.SUPERADMIN, Role.ADMIN),
        editar_contatos=papel in (Role.SUPERADMIN, Role.ADMIN, Role.SINDICO),
        planos=PLANOS_CONDOMINIO,
        fusos=FUSOS_CONDOMINIO,
        tipos_divisao=TIPOS_DIVISAO_CONDOMINIO,
        criado_em=condominio.criado_em or condominio.data_cadastro,
    )


def register(app):
    """Registra as rotas do admin preservando os endpoints legados."""
    app.add_url_rule(
        "/admin/login", "admin_login", admin_login, methods=["GET", "POST"]
    )
    app.add_url_rule("/admin/logout", "admin_logout", admin_logout, methods=["GET"])
    app.add_url_rule(
        "/admin/dashboard", "admin_dashboard", admin_dashboard, methods=["GET"]
    )
    app.add_url_rule(
        "/admin/condominio",
        "admin_condominio",
        admin_condominio,
        methods=["GET", "POST"],
    )
    app.add_url_rule("/admin/setores", "admin_setores", admin_setores, methods=["GET"])
    app.add_url_rule(
        "/admin/setores/novo",
        "admin_setores_criar",
        admin_setores_criar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/setores/<int:setor_id>/excluir",
        "admin_setores_excluir",
        admin_setores_excluir,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/setores/<int:setor_id>/contatos",
        "admin_setores_contato_salvar",
        admin_setores_contato_salvar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/setores/<int:setor_id>/contatos/<int:pessoa_id>/excluir",
        "admin_setores_contato_excluir",
        admin_setores_contato_excluir,
        methods=["POST"],
    )
    app.add_url_rule("/admin", "admin_index", admin_index, methods=["GET"])
    app.add_url_rule(
        "/admin/clube_vantagens",
        "admin_clube_vantagens",
        admin_clube_vantagens,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/clube_vantagens/analytics",
        "admin_clube_vantagens_analytics",
        admin_clube_vantagens_analytics,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/usuarios/novo",
        "admin_criar_usuario",
        admin_criar_usuario,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/admin/usuarios/<int:usuario_id>/escopo",
        "admin_editar_escopo_sindico",
        admin_editar_escopo_sindico,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/usuarios/excluir/<int:usuario_id>",
        "admin_excluir_usuario",
        admin_excluir_usuario,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/registrar/<int:unidade_id>",
        "admin_registrar",
        admin_registrar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/unidade/<int:unidade_id>/aprovar_atualizacao",
        "admin_aprovar_atualizacao",
        admin_aprovar_atualizacao,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/unidades/<int:unidade_id>/alterar_senha",
        "admin_unidade_alterar_senha",
        admin_unidade_alterar_senha,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/excluir-unidade/<int:unidade_id>",
        "admin_excluir_unidade",
        admin_excluir_unidade,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/unidade/<int:unidade_id>/validar_documentacao",
        "admin_validar_documentacao",
        admin_validar_documentacao,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/validar-documento/<int:unidade_id>",
        "admin_validar_documento",
        admin_validar_documento,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/validar-contrato-locacao/<int:unidade_id>",
        "admin_validar_contrato_locacao",
        admin_validar_contrato_locacao,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/validar-documentos/<int:unidade_id>",
        "admin_validar_documentos",
        admin_validar_documentos,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/salvar-proprietario/<int:unidade_id>",
        "admin_salvar_proprietario",
        admin_salvar_proprietario,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/atualizar-status-documentos/<int:unidade_id>",
        "admin_atualizar_status_documentos",
        admin_atualizar_status_documentos,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/usuarios/<int:usuario_id>/alterar-senha",
        "admin_alterar_senha_usuario",
        admin_alterar_senha_usuario,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/ocorrencias",
        "admin_ocorrencias",
        admin_ocorrencias,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/ocorrencias/atualizar_status/<int:id>",
        "admin_ocorrencias_atualizar_status",
        admin_ocorrencias_atualizar_status,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/mudancas",
        "admin_mudancas",
        admin_mudancas,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/admin/portaria/configuracoes",
        "admin_portaria_configuracoes",
        admin_portaria_configuracoes,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/portaria/configuracoes/gerais",
        "admin_portaria_configuracoes_gerais",
        admin_portaria_configuracoes_gerais,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/portaria/guarita/salvar",
        "admin_portaria_guarita_salvar",
        admin_portaria_guarita_salvar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/portaria/guarita/<int:guarita_id>/toggle",
        "admin_portaria_guarita_toggle",
        admin_portaria_guarita_toggle,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/portaria/checklist/salvar",
        "admin_portaria_checklist_salvar",
        admin_portaria_checklist_salvar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/portaria/checklist/<int:item_id>/toggle",
        "admin_portaria_checklist_toggle",
        admin_portaria_checklist_toggle,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/controle-acesso",
        "admin_controle_acesso",
        admin_controle_acesso,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/controle-acesso/salvar",
        "admin_controle_acesso_salvar",
        admin_controle_acesso_salvar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/controle-acesso/<int:credencial_id>/revogar",
        "admin_controle_acesso_revogar",
        admin_controle_acesso_revogar,
        methods=["POST"],
    )
