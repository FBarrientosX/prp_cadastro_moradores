"""Portal do Parceiro: login, dashboard, cupons e perfil do parceiro comercial.

Extraído de app/routes.py como primeira fatia isolada do arquivo monolítico.
Isolado por design: sessão própria (`parceiro_id`), sem relação com
condominio_id/tenant. Não usa a classe Blueprint do Flask de propósito —
Blueprint.route() sempre prefixa o endpoint com o nome do blueprint
(ex.: "parceiro_portal.parceiro_login"), o que quebraria todo `url_for(...)`
já espalhado pelos templates. Em vez disso, `register(app)` chama
`app.add_url_rule` diretamente, preservando os nomes de endpoint originais.
"""

import traceback
from datetime import datetime
from functools import wraps

from flask import flash, redirect, render_template, request, session, url_for
from sqlalchemy import case, func
from werkzeug.security import check_password_hash

from app import db
from app.email_service import enviar_email_redefinicao_senha
from app.models import Cupom, Parceiro, ResgateCupom
from app.utils import (
    SALT_RECUPERACAO_PARCEIRO,
    gerar_token_redefinicao,
    html_rico_form,
    link_rede_social,
    salvar_logo_parceiro,
    verificar_token_redefinicao,
)


def parceiro_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("parceiro_id"):
            return view(*args, **kwargs)
        flash("Faça login para acessar o Portal do Parceiro.", "warning")
        return redirect(url_for("parceiro_login"))

    return wrapped


def _buscar_parceiro_logado():
    parceiro_id = session.get("parceiro_id")
    if not parceiro_id:
        return None
    return Parceiro.query.get(parceiro_id)


def _parse_limite_total_form(valor):
    if valor is None:
        return None
    valor = str(valor).strip()
    if not valor:
        return None
    try:
        limite = int(valor)
    except ValueError:
        return None
    return limite if limite > 0 else None


def _parse_limite_por_unidade_form(valor, padrao=1):
    if valor is None or not str(valor).strip():
        return padrao
    try:
        limite = int(str(valor).strip())
    except ValueError:
        return padrao
    return limite if limite > 0 else padrao


def _metricas_resgates_por_cupom(cupom_ids):
    if not cupom_ids:
        return {}
    rows = (
        db.session.query(
            ResgateCupom.cupom_id,
            func.count(ResgateCupom.id).label("total_resgatados"),
            func.sum(
                case((ResgateCupom.status == "Utilizado", 1), else_=0)
            ).label("total_validados"),
        )
        .filter(ResgateCupom.cupom_id.in_(cupom_ids))
        .group_by(ResgateCupom.cupom_id)
        .all()
    )
    metricas = {
        cupom_id: {"total_resgatados": 0, "total_validados": 0} for cupom_id in cupom_ids
    }
    for cupom_id, total_resgatados, total_validados in rows:
        metricas[cupom_id] = {
            "total_resgatados": total_resgatados,
            "total_validados": int(total_validados or 0),
        }
    return metricas


def parceiro_login():
    if request.method == "POST":
        usuario_digitado = request.form.get("usuario_login", "").strip().lower()
        senha = request.form.get("senha", "")

        parceiro = Parceiro.query.filter_by(usuario_login=usuario_digitado).first()
        if not parceiro:
            parceiro = Parceiro.query.filter_by(email=usuario_digitado).first()

        if parceiro and check_password_hash(parceiro.senha_hash, senha):
            if parceiro.status == "Bloqueado":
                flash(
                    "Sua conta foi suspensa pela administração do condomínio. "
                    "Entre em contato para mais detalhes.",
                    "danger",
                )
                return render_template("parceiro_login.html")
            if not parceiro.usuario_login:
                parceiro.usuario_login = parceiro.email
                db.session.commit()
            # Evita sessão mista (admin/síndico/morador + parceiro na mesma aba).
            session.clear()
            session["parceiro_id"] = parceiro.id
            session.permanent = True
            flash("Login realizado com sucesso.", "success")
            return redirect(url_for("parceiro_dashboard"))

        flash("Usuário ou senha inválidos.", "danger")

    return render_template("parceiro_login.html")


def parceiro_esqueci_senha():
    if request.method == "POST":
        email_solicitado = request.form.get("email", "").strip().lower()
        mensagem_generica = (
            "Se o e-mail estiver cadastrado, enviaremos instruções para redefinição de senha."
        )

        parceiro = Parceiro.query.filter_by(email=email_solicitado).first()
        if parceiro and parceiro.status == "Ativo":
            try:
                token = gerar_token_redefinicao(email_solicitado, SALT_RECUPERACAO_PARCEIRO)
                link = url_for("parceiro_redefinir_senha", token=token, _external=True)
                enviar_email_redefinicao_senha(parceiro.email, link, perfil="parceiro")
            except Exception:
                traceback.print_exc()
                flash(
                    "Não foi possível enviar o e-mail. Tente novamente mais tarde.",
                    "danger",
                )
                return render_template("parceiro_esqueci_senha.html")

        flash(mensagem_generica, "info")
        return render_template("parceiro_esqueci_senha.html")

    return render_template("parceiro_esqueci_senha.html")


def parceiro_redefinir_senha(token):
    # verificar_token_redefinicao retorna (email, condominio_id, emitido_em).
    # Parceiro é entidade global (sem tenant): condominio_id do token é
    # ignorado; emitido_em invalida reuso após troca de senha.
    email, _condominio_id_token, emitido_em = verificar_token_redefinicao(
        token, SALT_RECUPERACAO_PARCEIRO
    )
    if not email:
        flash("Link inválido ou expirado. Solicite uma nova redefinição de senha.", "danger")
        return redirect(url_for("parceiro_esqueci_senha"))

    parceiro = Parceiro.query.filter_by(email=email).first()
    if not parceiro or parceiro.status != "Ativo":
        flash("Parceiro não encontrado para este e-mail.", "danger")
        return redirect(url_for("parceiro_esqueci_senha"))

    if parceiro.senha_atualizada_em and emitido_em:
        emitido_em_naive = (
            emitido_em.replace(tzinfo=None) if emitido_em.tzinfo else emitido_em
        )
        if parceiro.senha_atualizada_em >= emitido_em_naive:
            flash(
                "Este link já foi utilizado ou não é mais válido. "
                "Solicite uma nova redefinição de senha.",
                "danger",
            )
            return redirect(url_for("parceiro_esqueci_senha"))

    if request.method == "POST":
        senha = request.form.get("senha", "").strip()
        confirmacao = request.form.get("confirmacao_senha", "").strip()

        if len(senha) < 6:
            flash("A senha deve ter ao menos 6 caracteres.", "danger")
            return render_template("parceiro_redefinir_senha.html", token=token)
        if senha != confirmacao:
            flash("As senhas não coincidem.", "danger")
            return render_template("parceiro_redefinir_senha.html", token=token)

        parceiro.set_password(senha)
        db.session.commit()
        flash("Senha redefinida com sucesso. Faça login com a nova senha.", "success")
        return redirect(url_for("parceiro_login"))

    return render_template("parceiro_redefinir_senha.html", token=token)


def parceiro_logout():
    session.pop("parceiro_id", None)
    flash("Sessão do parceiro encerrada.", "info")
    return redirect(url_for("parceiro_login"))


@parceiro_required
def parceiro_dashboard():
    parceiro = _buscar_parceiro_logado()
    if not parceiro:
        session.pop("parceiro_id", None)
        flash("Sessão inválida. Faça login novamente.", "warning")
        return redirect(url_for("parceiro_login"))

    if parceiro.status == "Pendente":
        return render_template("parceiro_pendente.html", parceiro=parceiro)

    total_cupons_ativos = (
        Cupom.query.filter_by(parceiro_id=parceiro.id, ativo=True).count()
    )
    total_validacoes = (
        ResgateCupom.query.join(Cupom)
        .filter(
            Cupom.parceiro_id == parceiro.id,
            ResgateCupom.status == "Utilizado",
        )
        .count()
    )
    historico_resgates = (
        ResgateCupom.query.join(Cupom)
        .filter(Cupom.parceiro_id == parceiro.id)
        .order_by(ResgateCupom.data_resgate.desc())
        .limit(20)
        .all()
    )
    return render_template(
        "parceiro_dashboard.html",
        parceiro=parceiro,
        total_cupons_ativos=total_cupons_ativos,
        total_validacoes=total_validacoes,
        historico_resgates=historico_resgates,
    )


@parceiro_required
def parceiro_validacao():
    parceiro = _buscar_parceiro_logado()
    if not parceiro:
        session.pop("parceiro_id", None)
        flash("Sessão inválida. Faça login novamente.", "warning")
        return redirect(url_for("parceiro_login"))
    if parceiro.status == "Pendente":
        flash("Ative seu cadastro para validar cupons.", "warning")
        return redirect(url_for("parceiro_dashboard"))
    if parceiro.status != "Ativo":
        flash("Seu acesso está indisponível no momento.", "danger")
        return redirect(url_for("parceiro_dashboard"))

    codigo_url = request.args.get("codigo", "").strip().upper()
    return render_template(
        "parceiro_validacao.html",
        parceiro=parceiro,
        codigo_url=codigo_url,
    )


@parceiro_required
def parceiro_cupons():
    parceiro = _buscar_parceiro_logado()
    if not parceiro:
        session.pop("parceiro_id", None)
        flash("Sessão inválida. Faça login novamente.", "warning")
        return redirect(url_for("parceiro_login"))
    if parceiro.status == "Pendente":
        flash("Ative seu cadastro para gerenciar cupons.", "warning")
        return redirect(url_for("parceiro_dashboard"))
    if parceiro.status != "Ativo":
        flash("Seu acesso está indisponível no momento.", "danger")
        return redirect(url_for("parceiro_dashboard"))

    cupons = Cupom.query.filter_by(parceiro_id=parceiro.id).order_by(Cupom.id.desc()).all()
    metricas_cupons = _metricas_resgates_por_cupom([cupom.id for cupom in cupons])
    return render_template(
        "parceiro_cupons.html",
        parceiro=parceiro,
        cupons=cupons,
        metricas_cupons=metricas_cupons,
    )


@parceiro_required
def parceiro_validar_codigo():
    parceiro = _buscar_parceiro_logado()
    if not parceiro:
        session.pop("parceiro_id", None)
        flash("Sessão inválida. Faça login novamente.", "warning")
        return redirect(url_for("parceiro_login"))
    if parceiro.status != "Ativo":
        flash("Ative seu cadastro para validar cupons.", "warning")
        return redirect(url_for("parceiro_validacao"))

    codigo_unico = request.form.get("codigo_unico", "").strip().upper()
    if not codigo_unico:
        flash("Informe um código para validação.", "danger")
        return redirect(url_for("parceiro_validacao"))

    resgate = ResgateCupom.query.filter_by(codigo_unico=codigo_unico).first()
    if not resgate:
        flash("Código inválido. Verifique e tente novamente.", "danger")
        return redirect(url_for("parceiro_validacao"))

    if resgate.cupom.parceiro_id != parceiro.id:
        flash("Este código pertence a outro parceiro.", "danger")
        return redirect(url_for("parceiro_validacao"))

    if resgate.status != "Ativo":
        flash("Este código já foi utilizado ou está indisponível.", "warning")
        return redirect(url_for("parceiro_validacao"))

    resgate.status = "Utilizado"
    resgate.data_utilizacao = datetime.utcnow()
    db.session.commit()

    unidade_texto = (
        f"Bloco {resgate.unidade.bloco}, Apto {resgate.unidade.apartamento}"
        if resgate.unidade
        else "Unidade não identificada"
    )
    flash(f"Cupom validado! Unidade: {unidade_texto}.", "success")
    return redirect(url_for("parceiro_validacao"))


@parceiro_required
def parceiro_aprovar():
    parceiro = _buscar_parceiro_logado()
    if not parceiro:
        session.pop("parceiro_id", None)
        flash("Sessão inválida. Faça login novamente.", "warning")
        return redirect(url_for("parceiro_login"))

    if parceiro.status == "Bloqueado":
        flash(
            "Sua conta foi suspensa pela administração do condomínio. "
            "Entre em contato para mais detalhes.",
            "danger",
        )
        return redirect(url_for("parceiro_dashboard"))

    parceiro.status = "Ativo"
    parceiro.ativo = True
    db.session.commit()
    flash("Cadastro aprovado e ativado com sucesso!", "success")
    return redirect(url_for("parceiro_dashboard"))


@parceiro_required
def parceiro_cupons_criar():
    parceiro = _buscar_parceiro_logado()
    if not parceiro:
        session.pop("parceiro_id", None)
        flash("Sessão inválida. Faça login novamente.", "warning")
        return redirect(url_for("parceiro_login"))
    if parceiro.status != "Ativo":
        flash("Ative seu cadastro para criar cupons.", "warning")
        return redirect(url_for("parceiro_cupons"))

    titulo = request.form.get("titulo", "").strip()
    descricao = html_rico_form("descricao")
    codigo_prefixo = request.form.get("codigo_prefixo", "").strip().upper()
    data_validade_str = request.form.get("data_validade", "").strip()
    limite_total = _parse_limite_total_form(request.form.get("limite_total"))
    limite_por_unidade = _parse_limite_por_unidade_form(
        request.form.get("limite_por_unidade"), padrao=1
    )

    if not titulo or not descricao or not codigo_prefixo:
        flash("Preencha título, descrição e código prefixo.", "danger")
        return redirect(url_for("parceiro_cupons"))

    data_validade = None
    if data_validade_str:
        try:
            data_validade = datetime.strptime(data_validade_str, "%Y-%m-%d").date()
        except ValueError:
            flash("Data de validade inválida.", "danger")
            return redirect(url_for("parceiro_cupons"))

    db.session.add(
        Cupom(
            parceiro_id=parceiro.id,
            titulo=titulo,
            descricao=descricao,
            codigo_prefixo=codigo_prefixo,
            data_validade=data_validade,
            ativo=True,
            limite_total=limite_total,
            limite_por_unidade=limite_por_unidade,
        )
    )
    db.session.commit()
    flash("Cupom criado com sucesso.", "success")
    return redirect(url_for("parceiro_cupons"))


@parceiro_required
def parceiro_cupons_desativar(cupom_id):
    parceiro = _buscar_parceiro_logado()
    if not parceiro:
        session.pop("parceiro_id", None)
        flash("Sessão inválida. Faça login novamente.", "warning")
        return redirect(url_for("parceiro_login"))
    if parceiro.status != "Ativo":
        flash("Ative seu cadastro para gerenciar cupons.", "warning")
        return redirect(url_for("parceiro_cupons"))

    cupom = Cupom.query.filter_by(id=cupom_id, parceiro_id=parceiro.id).first_or_404()
    if not cupom.ativo:
        flash("Este cupom já está inativo.", "info")
        return redirect(url_for("parceiro_cupons"))

    cupom.ativo = False
    cupom.data_desativacao = datetime.utcnow()
    db.session.commit()
    flash("Cupom desativado permanentemente.", "warning")
    return redirect(url_for("parceiro_cupons"))


@parceiro_required
def parceiro_perfil():
    parceiro = _buscar_parceiro_logado()
    if not parceiro:
        session.pop("parceiro_id", None)
        flash("Sessão inválida. Faça login novamente.", "warning")
        return redirect(url_for("parceiro_login"))
    if parceiro.status == "Bloqueado":
        flash(
            "Sua conta foi suspensa pela administração do condomínio. "
            "Entre em contato para mais detalhes.",
            "danger",
        )
        return redirect(url_for("parceiro_login"))

    return render_template("parceiro_perfil.html", parceiro=parceiro)


@parceiro_required
def parceiro_perfil_editar():
    parceiro = _buscar_parceiro_logado()
    if not parceiro:
        session.pop("parceiro_id", None)
        flash("Sessão inválida. Faça login novamente.", "warning")
        return redirect(url_for("parceiro_login"))
    if parceiro.status == "Bloqueado":
        flash(
            "Sua conta foi suspensa pela administração do condomínio. "
            "Entre em contato para mais detalhes.",
            "danger",
        )
        return redirect(url_for("parceiro_login"))

    nome_empresa = request.form.get("nome_empresa", "").strip()
    email = request.form.get("email", "").strip().lower()
    telefone = request.form.get("telefone", "").strip() or None
    categoria = request.form.get("categoria", "").strip()
    endereco = request.form.get("endereco", "").strip() or None
    descricao = html_rico_form("descricao") or None
    link_instagram = link_rede_social(request.form.get("link_instagram"))
    link_facebook = link_rede_social(request.form.get("link_facebook"))

    if not nome_empresa or not email or not categoria:
        flash("Preencha nome da empresa, e-mail e categoria.", "danger")
        return redirect(url_for("parceiro_perfil"))

    parceiro_existente = Parceiro.query.filter(
        Parceiro.email == email,
        Parceiro.id != parceiro.id,
    ).first()
    if parceiro_existente:
        flash("Já existe outro parceiro cadastrado com este e-mail.", "warning")
        return redirect(url_for("parceiro_perfil"))

    novo_logo, erro_logo = salvar_logo_parceiro(
        request.files.get("logo"),
        prefixo=parceiro.usuario_login or f"parceiro{parceiro.id}",
    )
    if erro_logo:
        flash(erro_logo, "danger")
        return redirect(url_for("parceiro_perfil"))

    parceiro.nome_empresa = nome_empresa
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
    flash("Perfil comercial atualizado com sucesso.", "success")
    return redirect(url_for("parceiro_perfil"))


def register(app):
    """Registra as rotas do Portal do Parceiro preservando os endpoints legados."""
    app.add_url_rule(
        "/parceiro", "parceiro_login", parceiro_login, methods=["GET", "POST"]
    )
    app.add_url_rule(
        "/parceiro/login",
        "parceiro_login_alt",
        parceiro_login,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/parceiro/logout", "parceiro_logout", parceiro_logout, methods=["GET"]
    )
    app.add_url_rule(
        "/parceiro/esqueci_senha",
        "parceiro_esqueci_senha",
        parceiro_esqueci_senha,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/parceiro/redefinir_senha/<token>",
        "parceiro_redefinir_senha",
        parceiro_redefinir_senha,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/parceiro/dashboard",
        "parceiro_dashboard",
        parceiro_dashboard,
        methods=["GET"],
    )
    app.add_url_rule(
        "/parceiro/validacao",
        "parceiro_validacao",
        parceiro_validacao,
        methods=["GET"],
    )
    app.add_url_rule(
        "/parceiro/cupons",
        "parceiro_cupons",
        parceiro_cupons,
        methods=["GET"],
    )
    app.add_url_rule(
        "/parceiro/perfil",
        "parceiro_perfil",
        parceiro_perfil,
        methods=["GET"],
    )
    app.add_url_rule(
        "/parceiro/perfil/editar",
        "parceiro_perfil_editar",
        parceiro_perfil_editar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/parceiro/validar_codigo",
        "parceiro_validar_codigo",
        parceiro_validar_codigo,
        methods=["POST"],
    )
    app.add_url_rule(
        "/parceiro/aprovar",
        "parceiro_aprovar",
        parceiro_aprovar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/parceiro/cupons/criar",
        "parceiro_cupons_criar",
        parceiro_cupons_criar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/parceiro/cupons/<int:cupom_id>/desativar",
        "parceiro_cupons_desativar",
        parceiro_cupons_desativar,
        methods=["POST"],
    )
