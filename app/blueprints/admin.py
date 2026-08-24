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

from flask import flash, redirect, render_template, request, url_for
from sqlalchemy import and_, case, func, or_, text

from app import db
from app.auth import (
    admin_or_assistente_required,
    admin_or_sindico_required,
    admin_required,
    condominio_id_obrigatorio,
    get_current_user,
    logout_usuario,
)
from app.models import (
    AgendamentoMudanca,
    Cupom,
    Encomenda,
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
    StatusUnidade,
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
    base_unidades = Unidade.query.filter(Unidade.condominio_id == condominio_id)

    total_aprovados = base_unidades.filter_by(
        status=StatusUnidade.REGISTRADA
    ).count()

    aguardando_registro = base_unidades.filter_by(
        status=StatusUnidade.APROVADA
    ).count()

    documentos_pendentes = base_unidades.filter(
        or_(
            Unidade.documento_status.in_(
                [StatusDocumento.PENDENTE, StatusDocumento.NAO_ENVIADO]
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
        current_user=usuario,
    )


@admin_or_assistente_required
def admin_index():
    usuario = get_current_user()
    condominio_id = condominio_id_obrigatorio(usuario)

    aguardando_registro = (
        Unidade.query.filter_by(
            condominio_id=condominio_id, status=StatusUnidade.APROVADA
        )
        .order_by(Unidade.bloco, Unidade.apartamento)
        .all()
    )
    finalizados = (
        Unidade.query.filter_by(
            condominio_id=condominio_id, status=StatusUnidade.REGISTRADA
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

    return render_template(
        "dashboard_admin.html",
        aguardando_registro=aguardando_registro,
        finalizados=finalizados,
        sindicos=sindicos,
        equipe_acessos=equipe_acessos,
        current_user=usuario,
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
def admin_validar_documento(unidade_id):
    from app.routes import _unidade_do_tenant

    unidade = _unidade_do_tenant(unidade_id, condominio_id_obrigatorio())
    unidade.documento_status = StatusDocumento.ENTREGUE
    db.session.commit()
    flash(
        f"Documento da unidade Bloco {unidade.bloco}, Apto {unidade.apartamento} "
        f"marcado como entregue/validado.",
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
    unidade.documento_status = StatusDocumento.ENTREGUE
    if unidade.contrato_locacao_status != StatusDocumento.NAO_APLICAVEL:
        unidade.contrato_locacao_status = StatusDocumento.ENTREGUE

    db.session.commit()
    flash(
        f"Documentos da unidade Bloco {unidade.bloco}, Apto {unidade.apartamento} "
        f"marcados como entregues/validados.",
        "success",
    )
    return redirect(url_for("admin_index"))


@admin_required
def admin_atualizar_status_documentos(unidade_id):
    from app.routes import _unidade_do_tenant

    unidade = _unidade_do_tenant(unidade_id, condominio_id_obrigatorio())
    documento_status = request.form.get("documento_status", "").strip()
    contrato_status = request.form.get("contrato_locacao_status", "").strip()
    status_permitidos = {StatusDocumento.PENDENTE, StatusDocumento.ENTREGUE}

    if documento_status in status_permitidos:
        unidade.documento_status = documento_status

    if unidade.contrato_locacao_status != StatusDocumento.NAO_APLICAVEL:
        if contrato_status in status_permitidos:
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


@admin_required
def admin_alterar_senha_sindico():
    from app.routes import _label_agrupamentos_sindico, _usuario_do_tenant

    condominio_id = condominio_id_obrigatorio()
    usuario_id = request.form.get("usuario_id", type=int)
    nova_senha = request.form.get("nova_senha", "").strip()

    if not usuario_id or not nova_senha:
        flash("Informe o síndico e a nova senha.", "danger")
        return redirect(url_for("admin_index"))

    if len(nova_senha) < 6:
        flash("A nova senha deve ter ao menos 6 caracteres.", "danger")
        return redirect(url_for("admin_index"))

    sindico = _usuario_do_tenant(usuario_id, condominio_id)
    if sindico.role != Role.SINDICO:
        flash("Síndico não encontrado.", "danger")
        return redirect(url_for("admin_index"))

    sindico.set_password(nova_senha)
    db.session.commit()
    flash(
        f"Senha do síndico ({_label_agrupamentos_sindico(sindico)}) "
        "atualizada com sucesso.",
        "success",
    )
    return redirect(url_for("admin_index"))


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


@admin_required
def admin_criar_usuario():
    usuario_logado = get_current_user()
    condominio_id = condominio_id_obrigatorio(usuario_logado)
    blocos = [f"Bloco {indice}" for indice in range(1, 9)]

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        senha = request.form.get("senha", "")
        tipo_acesso = request.form.get("tipo_acesso", "").strip()
        bloco_responsavel = request.form.get("bloco_responsavel", "").strip()

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

        if role == Role.SINDICO and bloco_responsavel not in blocos:
            flash("Selecione um bloco válido para o síndico.", "danger")
            return render_template("criar_usuario.html", blocos=blocos)

        if Usuario.query.filter_by(username=username).first():
            flash("Já existe um usuário com esse login.", "warning")
            return render_template("criar_usuario.html", blocos=blocos)

        novo_usuario = Usuario(
            username=username,
            role=role,
            condominio_id=condominio_id,
        )
        novo_usuario.set_password(senha)
        db.session.add(novo_usuario)
        db.session.flush()

        if role == Role.SINDICO:
            db.session.add(
                SindicoAgrupamento(
                    usuario_id=novo_usuario.id,
                    condominio_id=condominio_id,
                    nome_agrupamento=bloco_responsavel,
                )
            )

        db.session.commit()

        flash("Usuário criado com sucesso.", "success")
        return redirect(url_for("admin_index"))

    return render_template("criar_usuario.html", blocos=blocos)


@admin_required
def admin_excluir_usuario(usuario_id):
    from app.routes import _registrar_auditoria, _usuario_do_tenant

    usuario_logado = get_current_user()
    condominio_id = condominio_id_obrigatorio(usuario_logado)
    usuario_alvo = _usuario_do_tenant(usuario_id, condominio_id)

    if usuario_alvo.id == usuario_logado.id:
        flash("Você não pode excluir o próprio acesso.", "danger")
        return redirect(url_for("admin_index"))

    if usuario_alvo.role not in (Role.ASSISTENTE, Role.SINDICO, Role.PORTEIRO):
        flash(
            "Apenas acessos de assistente, síndico ou porteiro podem ser revogados aqui.",
            "warning",
        )
        return redirect(url_for("admin_index"))

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
    return redirect(url_for("admin_index"))


@admin_or_sindico_required
def admin_ocorrencias():
    """Kanban de ocorrências do condomínio do admin/síndico logado."""
    from app.routes import _blocos_codigo_sindico, _redirect_login_tenant

    usuario = get_current_user()
    condominio_id = condominio_id_obrigatorio(usuario)
    if not condominio_id:
        flash("Conta sem condomínio vinculado.", "danger")
        return _redirect_login_tenant()

    query = Ocorrencia.query.filter_by(condominio_id=condominio_id)
    if usuario.role == Role.SINDICO:
        # Síndico só vê ocorrências das unidades do(s) próprio(s) bloco(s) —
        # mesmo recorte de jurisdição aplicado em todo o resto do app.
        blocos_sindico = _blocos_codigo_sindico(usuario)
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


def register(app):
    """Registra as rotas do admin preservando os endpoints legados."""
    app.add_url_rule(
        "/admin/login", "admin_login", admin_login, methods=["GET", "POST"]
    )
    app.add_url_rule("/admin/logout", "admin_logout", admin_logout, methods=["GET"])
    app.add_url_rule(
        "/admin/dashboard", "admin_dashboard", admin_dashboard, methods=["GET"]
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
        "/admin/alterar-senha-sindico",
        "admin_alterar_senha_sindico",
        admin_alterar_senha_sindico,
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
