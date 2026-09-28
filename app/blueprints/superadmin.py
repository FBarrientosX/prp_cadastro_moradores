"""Painel do Super Admin da plataforma: condomínios (tenants) e parceiros globais.

Extraído de app/routes.py seguindo o mesmo padrão do Portal do Parceiro
(app/blueprints/parceiro.py): sem a classe Blueprint do Flask (ela prefixaria
o endpoint com o nome do blueprint e quebraria os `url_for(...)` já
existentes nos templates), apenas uma função `register(app)` que chama
`app.add_url_rule` preservando os endpoints originais.

`_registrar_auditoria` continua em app/routes.py por ser usada por vários
outros módulos (sindico, admin, portaria) — aqui é só importada.
"""

import os
import random
import secrets
import string

from flask import current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import func
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename

from app import db
from app.auth import (
    get_current_user,
    login_usuario,
    logout_usuario,
    normalizar_slug,
    superadmin_required,
    validar_slug,
)
from app.models import (
    CategoriaParceiro,
    Condominio,
    ConfiguracaoCondominio,
    Cupom,
    Parceiro,
    Role,
    ResgateCupom,
    Unidade,
    Usuario,
)
from app.utils import gerar_api_key, html_rico_form, link_rede_social, salvar_logo_parceiro

_LOGO_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def _normalizar_cor_primaria(valor):
    cor = str(valor or "").strip()
    if len(cor) == 7 and cor.startswith("#"):
        try:
            int(cor[1:], 16)
            return cor.lower()
        except ValueError:
            pass
    return "#0d6efd"


def _salvar_logo_condominio(arquivo, slug):
    """
    Salva logo em static/uploads/logos.
    Retorna (filename, erro). Sem arquivo: (None, None).
    """
    if not arquivo or not arquivo.filename:
        return None, None

    nome_seguro = secure_filename(arquivo.filename)
    if not nome_seguro or "." not in nome_seguro:
        return None, "Formato de imagem não permitido."

    extensao = nome_seguro.rsplit(".", 1)[-1].lower()
    if extensao not in _LOGO_EXTENSIONS:
        return None, "Formato de imagem não permitido."

    pasta = current_app.config["UPLOAD_LOGOS_FOLDER"]
    os.makedirs(pasta, exist_ok=True)
    token = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    nome_final = f"{slug}_{token}.{extensao}"
    arquivo.save(os.path.join(pasta, nome_final))
    return nome_final, None


def superadmin_login():
    usuario_logado = get_current_user()
    if usuario_logado and usuario_logado.role == Role.SUPERADMIN:
        return redirect(url_for("superadmin_dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        usuario = Usuario.query.filter_by(
            username=username, role=Role.SUPERADMIN
        ).first()
        if usuario and usuario.check_password(password):
            login_usuario(usuario)
            return redirect(url_for("superadmin_dashboard"))

        flash("Usuário ou senha inválidos.", "danger")

    return render_template(
        "login.html",
        titulo="Login Super Admin — Plataforma",
        action="superadmin",
    )


def superadmin_logout():
    logout_usuario()
    flash("Sessão encerrada.", "info")
    return redirect(url_for("superadmin_login"))


@superadmin_required
def superadmin_dashboard():
    usuario = get_current_user()
    total_condominios = Condominio.query.count()
    total_parceiros_ativos = Parceiro.query.filter_by(status="Ativo").count()
    total_usuarios = Usuario.query.filter(Usuario.role != Role.SUPERADMIN).count()
    condominios_recentes = (
        Condominio.query.order_by(Condominio.data_cadastro.desc()).limit(5).all()
    )

    return render_template(
        "superadmin_dashboard.html",
        current_user=usuario,
        total_condominios=total_condominios,
        total_parceiros_ativos=total_parceiros_ativos,
        total_usuarios=total_usuarios,
        condominios_recentes=condominios_recentes,
    )


@superadmin_required
def superadmin_condominios():
    usuario = get_current_user()

    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        slug = normalizar_slug(request.form.get("slug", ""))
        cnpj = request.form.get("cnpj", "").strip() or None
        label_agrupamento = request.form.get("label_agrupamento", "Bloco").strip() or "Bloco"
        label_unidade = request.form.get("label_unidade", "Apto").strip() or "Apto"
        usa_agrupamentos = request.form.get("usa_agrupamentos") == "on"
        tem_subsindicos = request.form.get("tem_subsindicos") == "on"
        fluxo_aprovacao_mudanca = (
            request.form.get("fluxo_aprovacao_mudanca", "Dupla").strip() or "Dupla"
        )
        cor_primaria = _normalizar_cor_primaria(request.form.get("cor_primaria"))

        if not nome:
            flash("Informe o nome do condomínio.", "danger")
            return redirect(url_for("superadmin_condominios"))

        if not validar_slug(slug):
            flash(
                "Informe um slug válido (apenas letras minúsculas, números e hifens).",
                "danger",
            )
            return redirect(url_for("superadmin_condominios"))

        if Condominio.query.filter_by(slug=slug).first():
            flash("Já existe um condomínio com este slug.", "warning")
            return redirect(url_for("superadmin_condominios"))

        if fluxo_aprovacao_mudanca not in ("Simples", "Dupla"):
            flash("Fluxo de aprovação de mudança inválido.", "danger")
            return redirect(url_for("superadmin_condominios"))

        logo_filename, erro_logo = _salvar_logo_condominio(request.files.get("logo"), slug)
        if erro_logo:
            flash(erro_logo, "danger")
            return redirect(url_for("superadmin_condominios"))

        condominio = Condominio(
            nome=nome,
            slug=slug,
            cnpj=cnpj,
            ativo=True,
            api_key=gerar_api_key(),
        )
        db.session.add(condominio)
        db.session.flush()
        db.session.add(
            ConfiguracaoCondominio(
                condominio_id=condominio.id,
                label_agrupamento=label_agrupamento,
                label_unidade=label_unidade,
                usa_agrupamentos=usa_agrupamentos,
                tem_subsindicos=tem_subsindicos,
                fluxo_aprovacao_mudanca=fluxo_aprovacao_mudanca,
                cor_primaria=cor_primaria,
                logo_filename=logo_filename,
            )
        )
        db.session.commit()
        flash(
            f"Condomínio '{nome}' cadastrado. Porta de entrada: /c/{slug}/login",
            "success",
        )
        return redirect(url_for("superadmin_condominios"))

    condominios = Condominio.query.order_by(Condominio.nome).all()
    admins_por_condominio = {
        row.condominio_id: row.total
        for row in (
            db.session.query(
                Usuario.condominio_id,
                func.count(Usuario.id).label("total"),
            )
            .filter(
                Usuario.role == Role.ADMIN,
                Usuario.condominio_id.isnot(None),
            )
            .group_by(Usuario.condominio_id)
            .all()
        )
    }

    return render_template(
        "superadmin_condominios.html",
        current_user=usuario,
        condominios=condominios,
        admins_por_condominio=admins_por_condominio,
        labels_agrupamento=("Bloco", "Torre", "Rua", "Quadra", "Setor"),
        labels_unidade=("Apto", "Casa", "Sala", "Loja", "Unidade"),
    )


@superadmin_required
def superadmin_condominio_primeiro_admin(condominio_id):
    condominio = Condominio.query.get_or_404(condominio_id)
    username = request.form.get("username", "").strip()
    senha = request.form.get("senha", "")

    if not username:
        flash("Informe o login do administrador local.", "danger")
        return redirect(url_for("superadmin_condominios"))
    if len(senha) < 6:
        flash("A senha deve ter ao menos 6 caracteres.", "danger")
        return redirect(url_for("superadmin_condominios"))

    if Usuario.query.filter_by(username=username).first():
        flash("Já existe um usuário com esse login.", "warning")
        return redirect(url_for("superadmin_condominios"))

    admin_local = Usuario(
        username=username,
        role=Role.ADMIN,
        condominio_id=condominio.id,
    )
    admin_local.set_password(senha)
    db.session.add(admin_local)
    db.session.commit()

    flash(
        f"Admin local '{username}' criado para o condomínio '{condominio.nome}'.",
        "success",
    )
    return redirect(url_for("superadmin_condominios"))


@superadmin_required
def superadmin_condominio_whitelabel(condominio_id):
    """Atualiza cor primária e logo (white-label) de um condomínio existente."""
    condominio = Condominio.query.get_or_404(condominio_id)
    cfg = condominio.configuracao
    if cfg is None:
        cfg = ConfiguracaoCondominio(condominio_id=condominio.id)
        db.session.add(cfg)
        db.session.flush()

    cfg.cor_primaria = _normalizar_cor_primaria(request.form.get("cor_primaria"))

    novo_logo, erro_logo = _salvar_logo_condominio(
        request.files.get("logo"), condominio.slug or f"condo{condominio.id}"
    )
    if erro_logo:
        flash(erro_logo, "danger")
        return redirect(url_for("superadmin_condominios"))
    if novo_logo:
        cfg.logo_filename = novo_logo

    db.session.commit()
    flash(f"Identidade visual de '{condominio.nome}' atualizada.", "success")
    return redirect(url_for("superadmin_condominios"))


@superadmin_required
def superadmin_condominio_editar(condominio_id):
    """Atualiza dados básicos e configuração operacional. Slug é imutável."""
    condominio = Condominio.query.get_or_404(condominio_id)
    nome = request.form.get("nome", "").strip()
    cnpj = request.form.get("cnpj", "").strip() or None
    label_agrupamento = request.form.get("label_agrupamento", "Bloco").strip() or "Bloco"
    label_unidade = request.form.get("label_unidade", "Apto").strip() or "Apto"
    usa_agrupamentos = request.form.get("usa_agrupamentos") == "on"
    tem_subsindicos = request.form.get("tem_subsindicos") == "on"
    fluxo_aprovacao_mudanca = (
        request.form.get("fluxo_aprovacao_mudanca", "Dupla").strip() or "Dupla"
    )

    if not nome:
        flash("Informe o nome do condomínio.", "danger")
        return redirect(url_for("superadmin_condominios"))

    if fluxo_aprovacao_mudanca not in ("Simples", "Dupla"):
        flash("Fluxo de aprovação de mudança inválido.", "danger")
        return redirect(url_for("superadmin_condominios"))

    condominio.nome = nome
    condominio.cnpj = cnpj

    cfg = condominio.configuracao
    if cfg is None:
        cfg = ConfiguracaoCondominio(condominio_id=condominio.id)
        db.session.add(cfg)
        db.session.flush()

    cfg.label_agrupamento = label_agrupamento
    cfg.label_unidade = label_unidade
    cfg.usa_agrupamentos = usa_agrupamentos
    cfg.tem_subsindicos = tem_subsindicos
    cfg.fluxo_aprovacao_mudanca = fluxo_aprovacao_mudanca

    db.session.commit()
    flash(f"Condomínio '{condominio.nome}' atualizado com sucesso.", "success")
    return redirect(url_for("superadmin_condominios"))


@superadmin_required
def superadmin_condominio_desativar(condominio_id):
    """Soft delete: marca condomínio como inativo, preservando histórico."""
    condominio = Condominio.query.get_or_404(condominio_id)
    if not condominio.ativo:
        flash(f"O condomínio '{condominio.nome}' já está inativo.", "info")
        return redirect(url_for("superadmin_condominios"))

    condominio.ativo = False
    db.session.commit()
    flash(
        f"Condomínio '{condominio.nome}' desativado. O acesso pela porta /c/{condominio.slug}/ está suspenso.",
        "success",
    )
    return redirect(url_for("superadmin_condominios"))


@superadmin_required
def superadmin_condominio_ativar(condominio_id):
    """Reativa condomínio previamente inativado (soft delete)."""
    condominio = Condominio.query.get_or_404(condominio_id)
    if condominio.ativo:
        flash(f"O condomínio '{condominio.nome}' já está ativo.", "info")
        return redirect(url_for("superadmin_condominios"))

    condominio.ativo = True
    db.session.commit()
    flash(f"Condomínio '{condominio.nome}' reativado com sucesso.", "success")
    return redirect(url_for("superadmin_condominios"))


@superadmin_required
def superadmin_parceiros():
    """Gestão global de parceiros do Clube de Vantagens (plataforma)."""
    usuario = get_current_user()
    parceiros = Parceiro.query.order_by(Parceiro.data_cadastro.desc()).all()
    auditoria_cupons = (
        ResgateCupom.query.join(Cupom)
        .join(Parceiro)
        .join(Unidade)
        .order_by(ResgateCupom.data_resgate.desc())
        .limit(100)
        .all()
    )

    return render_template(
        "superadmin_parceiros.html",
        current_user=usuario,
        parceiros=parceiros,
        auditoria_cupons=auditoria_cupons,
    )


@superadmin_required
def superadmin_parceiros_criar():
    nome_empresa = request.form.get("nome_empresa", "").strip()
    usuario_login = request.form.get("usuario_login", "").strip().lower()
    email = request.form.get("email", "").strip().lower()
    telefone = request.form.get("telefone", "").strip() or None
    categoria = request.form.get("categoria", "").strip()
    endereco = request.form.get("endereco", "").strip() or None
    descricao = html_rico_form("descricao") or None
    link_instagram = link_rede_social(request.form.get("link_instagram"))
    link_facebook = link_rede_social(request.form.get("link_facebook"))

    if not nome_empresa or not usuario_login or not email or not categoria:
        flash("Preencha nome da empresa, usuário de login, e-mail e categoria.", "danger")
        return redirect(url_for("superadmin_parceiros"))

    if " " in usuario_login:
        flash("O usuário de login não pode conter espaços.", "danger")
        return redirect(url_for("superadmin_parceiros"))

    if Parceiro.query.filter_by(usuario_login=usuario_login).first():
        flash("Já existe parceiro cadastrado com este usuário de login.", "warning")
        return redirect(url_for("superadmin_parceiros"))

    if Parceiro.query.filter_by(email=email).first():
        flash("Já existe parceiro cadastrado com este e-mail.", "warning")
        return redirect(url_for("superadmin_parceiros"))

    logo_arquivo, erro_logo = salvar_logo_parceiro(
        request.files.get("logo"), prefixo=usuario_login
    )
    if erro_logo:
        flash(erro_logo, "danger")
        return redirect(url_for("superadmin_parceiros"))

    parceiro = Parceiro(
        nome_empresa=nome_empresa,
        usuario_login=usuario_login,
        email=email,
        senha_hash=generate_password_hash("senha123"),
        telefone=telefone,
        categoria=categoria,
        endereco=endereco,
        descricao=descricao,
        logo_arquivo=logo_arquivo,
        link_instagram=link_instagram,
        link_facebook=link_facebook,
        ativo=True,
        status="Pendente",
    )
    db.session.add(parceiro)
    db.session.commit()
    flash(
        "Parceiro global cadastrado. Status inicial: Pendente. Senha padrão: senha123.",
        "success",
    )
    return redirect(url_for("superadmin_parceiros"))


@superadmin_required
def superadmin_parceiro_editar(parceiro_id):
    parceiro = Parceiro.query.get_or_404(parceiro_id)
    nome_empresa = request.form.get("nome_empresa", "").strip()
    usuario_login = request.form.get("usuario_login", "").strip().lower()
    email = request.form.get("email", "").strip().lower()
    telefone = request.form.get("telefone", "").strip() or None
    categoria = request.form.get("categoria", "").strip()
    endereco = request.form.get("endereco", "").strip() or None
    descricao = html_rico_form("descricao") or None
    link_instagram = link_rede_social(request.form.get("link_instagram"))
    link_facebook = link_rede_social(request.form.get("link_facebook"))

    if not nome_empresa or not usuario_login or not email or not categoria:
        flash("Preencha nome da empresa, usuário de login, e-mail e categoria.", "danger")
        return redirect(url_for("superadmin_parceiros"))

    if " " in usuario_login:
        flash("O usuário de login não pode conter espaços.", "danger")
        return redirect(url_for("superadmin_parceiros"))

    parceiro_login_existente = Parceiro.query.filter(
        Parceiro.usuario_login == usuario_login,
        Parceiro.id != parceiro.id,
    ).first()
    if parceiro_login_existente:
        flash("Já existe outro parceiro cadastrado com este usuário de login.", "warning")
        return redirect(url_for("superadmin_parceiros"))

    parceiro_existente = Parceiro.query.filter(
        Parceiro.email == email,
        Parceiro.id != parceiro.id,
    ).first()
    if parceiro_existente:
        flash("Já existe outro parceiro cadastrado com este e-mail.", "warning")
        return redirect(url_for("superadmin_parceiros"))

    novo_logo, erro_logo = salvar_logo_parceiro(
        request.files.get("logo"), prefixo=usuario_login
    )
    if erro_logo:
        flash(erro_logo, "danger")
        return redirect(url_for("superadmin_parceiros"))

    parceiro.nome_empresa = nome_empresa
    parceiro.usuario_login = usuario_login
    parceiro.email = email
    parceiro.telefone = telefone
    parceiro.categoria = categoria
    parceiro.endereco = endereco
    parceiro.descricao = descricao
    parceiro.link_instagram = link_instagram
    parceiro.link_facebook = link_facebook
    if novo_logo:
        parceiro.logo_arquivo = novo_logo
    db.session.commit()
    flash("Parceiro atualizado com sucesso.", "success")
    return redirect(url_for("superadmin_parceiros"))


@superadmin_required
def superadmin_parceiro_bloquear(parceiro_id):
    from app.routes import _registrar_auditoria

    parceiro = Parceiro.query.get_or_404(parceiro_id)
    usuario = get_current_user()

    parceiro.status = "Bloqueado"
    parceiro.ativo = False
    Cupom.query.filter_by(parceiro_id=parceiro.id).update(
        {"ativo": False},
        synchronize_session=False,
    )
    _registrar_auditoria(
        usuario,
        f"Parceiro global bloqueado: {parceiro.nome_empresa} ({parceiro.email}).",
    )
    db.session.commit()
    flash(
        "Parceiro bloqueado. Os cupons deste parceiro foram removidos da vitrine.",
        "warning",
    )
    return redirect(url_for("superadmin_parceiros"))


@superadmin_required
def superadmin_parceiro_ativar(parceiro_id):
    from app.routes import _registrar_auditoria

    parceiro = Parceiro.query.get_or_404(parceiro_id)
    usuario = get_current_user()

    parceiro.status = "Ativo"
    parceiro.ativo = True
    _registrar_auditoria(
        usuario,
        f"Parceiro global reativado: {parceiro.nome_empresa} ({parceiro.email}).",
    )
    db.session.commit()
    flash("Parceiro reativado com sucesso.", "success")
    return redirect(url_for("superadmin_parceiros"))


@superadmin_required
def superadmin_clube_vantagens():
    """Catálogo global do Clube: categorias, parceiros e condomínios atendidos."""
    categorias = CategoriaParceiro.query.order_by(CategoriaParceiro.nome).all()
    parceiros = Parceiro.query.order_by(Parceiro.nome_empresa).all()
    todos_condominios = (
        Condominio.query.filter_by(ativo=True).order_by(Condominio.nome).all()
    )
    return render_template(
        "superadmin/clube_vantagens.html",
        categorias=categorias,
        parceiros=parceiros,
        todos_condominios=todos_condominios,
        aba=request.args.get("aba") or "parceiros",
    )


@superadmin_required
def superadmin_clube_categoria_salvar():
    nome = (request.form.get("nome") or "").strip()
    categoria_id = request.form.get("categoria_id", type=int)
    if not nome:
        flash("Informe o nome da categoria.", "danger")
        return redirect(url_for("superadmin_clube_vantagens", aba="categorias"))

    existente = CategoriaParceiro.query.filter(
        func.lower(CategoriaParceiro.nome) == nome.lower()
    ).first()

    if categoria_id:
        categoria = CategoriaParceiro.query.get_or_404(categoria_id)
        if existente and existente.id != categoria.id:
            flash("Já existe uma categoria com este nome.", "warning")
            return redirect(url_for("superadmin_clube_vantagens", aba="categorias"))
        categoria.nome = nome
        db.session.commit()
        flash("Categoria atualizada.", "success")
    else:
        if existente:
            flash("Já existe uma categoria com este nome.", "warning")
            return redirect(url_for("superadmin_clube_vantagens", aba="categorias"))
        db.session.add(CategoriaParceiro(nome=nome, ativa=True))
        db.session.commit()
        flash("Categoria criada.", "success")
    return redirect(url_for("superadmin_clube_vantagens", aba="categorias"))


@superadmin_required
def superadmin_clube_categoria_alternar(categoria_id):
    categoria = CategoriaParceiro.query.get_or_404(categoria_id)
    categoria.ativa = not bool(categoria.ativa)
    db.session.commit()
    estado = "ativada" if categoria.ativa else "desativada"
    flash(f"Categoria {estado}.", "success")
    return redirect(url_for("superadmin_clube_vantagens", aba="categorias"))


def _condominios_selecionados():
    ids = []
    for bruto in request.form.getlist("condominios[]"):
        try:
            ids.append(int(bruto))
        except (TypeError, ValueError):
            continue
    if not ids:
        return []
    return (
        Condominio.query.filter(Condominio.id.in_(ids), Condominio.ativo.is_(True))
        .order_by(Condominio.nome)
        .all()
    )


@superadmin_required
def superadmin_clube_parceiro_salvar():
    nome = (request.form.get("nome") or "").strip()
    descricao_vantagem = (request.form.get("descricao_vantagem") or "").strip() or None
    cupom = (request.form.get("cupom") or "").strip() or None
    categoria_id = request.form.get("categoria_id", type=int)
    parceiro_id = request.form.get("parceiro_id", type=int)

    if not nome or not categoria_id:
        flash("Informe o nome e a categoria do parceiro.", "danger")
        return redirect(url_for("superadmin_clube_vantagens"))

    categoria = db.session.get(CategoriaParceiro, categoria_id)
    if categoria is None or not categoria.ativa:
        flash("Selecione uma categoria ativa.", "warning")
        return redirect(url_for("superadmin_clube_vantagens"))

    if parceiro_id:
        parceiro = Parceiro.query.get_or_404(parceiro_id)
    else:
        token = secrets.token_hex(6)
        parceiro = Parceiro(
            nome_empresa=nome,
            email=f"clube-{token}@interno.vizinsync",
            usuario_login=f"clube_{token}",
            categoria=categoria.nome[:50],
            status="Ativo",
            ativo=True,
        )
        parceiro.set_password(secrets.token_urlsafe(16))
        db.session.add(parceiro)
        db.session.flush()

    parceiro.nome_empresa = nome
    parceiro.categoria_id = categoria.id
    parceiro.categoria = categoria.nome[:50]
    parceiro.descricao_vantagem = descricao_vantagem
    parceiro.cupom = cupom

    arquivo = request.files.get("logo")
    if arquivo and arquivo.filename:
        from app.drive_api import upload_logo_parceiro_drive

        nome_arquivo = secure_filename(arquivo.filename) or "logo"
        resultado = upload_logo_parceiro_drive(arquivo, filename=nome_arquivo)
        if not resultado or not resultado.get("id"):
            db.session.rollback()
            flash("Não foi possível enviar o logo para o Drive.", "danger")
            return redirect(url_for("superadmin_clube_vantagens"))
        parceiro.logo_drive_id = resultado.get("id")
        parceiro.logo_url = resultado.get("webViewLink")

    selecionados = _condominios_selecionados()
    parceiro.condominios = []
    db.session.flush()
    for condominio in selecionados:
        parceiro.condominios.append(condominio)

    db.session.commit()
    if selecionados:
        flash("Parceiro salvo. A oferta aparece nos condomínios marcados.", "success")
    else:
        flash(
            "Parceiro salvo. Sem condomínio marcado, a oferta continua visível em todos.",
            "success",
        )
    return redirect(url_for("superadmin_clube_vantagens"))


@superadmin_required
def superadmin_condominio_api_key(condominio_id):
    """Gera ou troca a chave M2M dos equipamentos deste condomínio."""
    from app.models import LogAuditoria

    condominio = Condominio.query.get_or_404(condominio_id)
    condominio.api_key = gerar_api_key()
    db.session.add(
        LogAuditoria(
            usuario_id=get_current_user().id,
            condominio_id=condominio.id,
            mensagem=f"API Key do condomínio {condominio.nome} gerada novamente.",
        )
    )
    db.session.commit()
    flash("Chave de API do condomínio atualizada. Copie o valor no cadastro.", "success")
    return redirect(url_for("superadmin_condominios"))


def register(app):
    """Registra as rotas do Super Admin preservando os endpoints legados."""
    app.add_url_rule(
        "/superadmin/login",
        "superadmin_login",
        superadmin_login,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/superadmin/logout",
        "superadmin_logout",
        superadmin_logout,
        methods=["GET"],
    )
    app.add_url_rule(
        "/superadmin",
        "superadmin_dashboard",
        superadmin_dashboard,
        methods=["GET"],
    )
    app.add_url_rule(
        "/superadmin/condominios",
        "superadmin_condominios",
        superadmin_condominios,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/superadmin/condominios/<int:condominio_id>/primeiro-admin",
        "superadmin_condominio_primeiro_admin",
        superadmin_condominio_primeiro_admin,
        methods=["POST"],
    )
    app.add_url_rule(
        "/superadmin/condominios/<int:condominio_id>/whitelabel",
        "superadmin_condominio_whitelabel",
        superadmin_condominio_whitelabel,
        methods=["POST"],
    )
    app.add_url_rule(
        "/superadmin/condominios/<int:condominio_id>/editar",
        "superadmin_condominio_editar",
        superadmin_condominio_editar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/superadmin/condominios/<int:condominio_id>/desativar",
        "superadmin_condominio_desativar",
        superadmin_condominio_desativar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/superadmin/condominios/<int:condominio_id>/ativar",
        "superadmin_condominio_ativar",
        superadmin_condominio_ativar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/superadmin/parceiros",
        "superadmin_parceiros",
        superadmin_parceiros,
        methods=["GET"],
    )
    app.add_url_rule(
        "/superadmin/parceiros/criar",
        "superadmin_parceiros_criar",
        superadmin_parceiros_criar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/superadmin/parceiros/<int:parceiro_id>/editar",
        "superadmin_parceiro_editar",
        superadmin_parceiro_editar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/superadmin/parceiros/<int:parceiro_id>/bloquear",
        "superadmin_parceiro_bloquear",
        superadmin_parceiro_bloquear,
        methods=["POST"],
    )
    app.add_url_rule(
        "/superadmin/parceiros/<int:parceiro_id>/ativar",
        "superadmin_parceiro_ativar",
        superadmin_parceiro_ativar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/superadmin/clube-vantagens",
        "superadmin_clube_vantagens",
        superadmin_clube_vantagens,
        methods=["GET"],
    )
    app.add_url_rule(
        "/superadmin/clube-vantagens/categoria/salvar",
        "superadmin_clube_categoria_salvar",
        superadmin_clube_categoria_salvar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/superadmin/clube-vantagens/categoria/<int:categoria_id>/alternar",
        "superadmin_clube_categoria_alternar",
        superadmin_clube_categoria_alternar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/superadmin/clube-vantagens/parceiro/salvar",
        "superadmin_clube_parceiro_salvar",
        superadmin_clube_parceiro_salvar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/superadmin/condominios/<int:condominio_id>/api-key",
        "superadmin_condominio_api_key",
        superadmin_condominio_api_key,
        methods=["POST"],
    )
