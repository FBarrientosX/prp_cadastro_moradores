import re
import secrets
import string
from functools import wraps

from flask import flash, redirect, session, url_for

from app.models import Condominio, Role, Usuario

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def gerar_senha_aleatoria(tamanho=8):
    alfabeto = string.ascii_letters + string.digits
    return "".join(secrets.choice(alfabeto) for _ in range(tamanho))


def normalizar_slug(slug):
    return str(slug or "").strip().lower()


def validar_slug(slug):
    """Slug obrigatório: letras minúsculas, números e hifens (sem espaços)."""
    slug_norm = normalizar_slug(slug)
    if not slug_norm or len(slug_norm) > 50:
        return False
    return bool(_SLUG_RE.match(slug_norm))


def obter_condominio_por_slug(slug):
    return Condominio.query.filter_by(slug=normalizar_slug(slug)).first_or_404()


def condominio_esta_ativo(condominio):
    """Soft delete: None/ausente trata como ativo (compatibilidade de migração)."""
    if condominio is None:
        return False
    ativo = getattr(condominio, "ativo", True)
    return ativo is not False and ativo != 0


def obter_condominio_padrao_id():
    """Fallback legado — preferir slug/sessão nas portas de entrada."""
    condominio = Condominio.query.filter_by(slug="prp").first()
    if condominio is None:
        condominio = Condominio.query.filter_by(nome="PRP Condomínio").first()
    if condominio is None:
        condominio = Condominio.query.order_by(Condominio.id).first()
    return condominio.id if condominio is not None else None


def resolver_condominio_id(usuario=None, unidade=None, permitir_fallback=True):
    """
    Resolve condominio_id a partir do ator logado, da unidade ou da sessão.

    Em rotas administrativas locais, preferir condominio_id_obrigatorio() —
    o fallback para o condomínio padrão (PRP) é apenas legado de cadastro público.
    """
    if usuario is not None and getattr(usuario, "condominio_id", None):
        return usuario.condominio_id
    if unidade is not None and getattr(unidade, "condominio_id", None):
        return unidade.condominio_id
    if session.get("cadastro_condominio_id"):
        return session["cadastro_condominio_id"]
    sessao_id = session.get("condominio_id")
    if sessao_id:
        return sessao_id
    if permitir_fallback:
        return obter_condominio_padrao_id()
    return None


def condominio_id_obrigatorio(usuario=None):
    """
    condominio_id do usuário local autenticado (fonte de verdade).
    Retorna None se o usuário não estiver vinculado a um tenant.
    """
    if usuario is None:
        usuario = get_current_user()
    if usuario is None:
        return None
    return getattr(usuario, "condominio_id", None)


def _sincronizar_sessao_tenant(usuario):
    """Garante que a sessão reflita o condominio_id do usuário (evita sessão suja)."""
    if usuario is None or not getattr(usuario, "condominio_id", None):
        return
    session["condominio_id"] = usuario.condominio_id
    _gravar_tenant_slug_sessao(usuario.condominio_id)


def _gravar_tenant_slug_sessao(condominio_id):
    if not condominio_id:
        return
    condominio = Condominio.query.get(condominio_id)
    if condominio and condominio.slug:
        session["tenant_slug"] = condominio.slug


def login_usuario(usuario):
    # Limpa sessão anterior (evita vazamento de tenant entre logins).
    session.clear()
    session["user_id"] = usuario.id
    session["role"] = usuario.role
    session["condominio_id"] = usuario.condominio_id
    _gravar_tenant_slug_sessao(usuario.condominio_id)
    session.permanent = True


def logout_usuario():
    session.pop("user_id", None)
    session.pop("role", None)
    session.pop("condominio_id", None)
    session.pop("tenant_slug", None)


def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    usuario = Usuario.query.get(user_id)
    if usuario is None:
        return None
    if usuario.role == Role.SUPERADMIN:
        return usuario
    sessao_cid = session.get("condominio_id")
    if (
        sessao_cid
        and usuario.condominio_id
        and int(usuario.condominio_id) != int(sessao_cid)
    ):
        return None
    return usuario


def _redirect_login_tenant():
    slug = session.get("tenant_slug") or "prp"
    return redirect(url_for("tenant_login", slug=slug))


def login_unidade(unidade):
    # Limpa sessão anterior — mesma razão de login_usuario(): sem isso, uma
    # sessão de staff (inclusive Super Admin) já autenticada podia logar como
    # unidade por cima e ficar com uma sessão "mista" operando o condomínio
    # errado, ou um morador anterior deixar resíduo de sessão para o próximo.
    session.clear()
    session["unidade_id"] = unidade.id
    session["unidade_bloco"] = unidade.bloco
    session["unidade_apartamento"] = unidade.apartamento
    session["condominio_id"] = unidade.condominio_id
    _gravar_tenant_slug_sessao(unidade.condominio_id)
    session.permanent = True


def logout_unidade():
    for chave in (
        "unidade_id",
        "unidade_bloco",
        "unidade_apartamento",
        "cadastro_bloco",
        "cadastro_apartamento",
    ):
        session.pop(chave, None)
    # Preserva condominio_id se houver usuário administrativo na sessão.
    if not session.get("user_id"):
        session.pop("condominio_id", None)


def get_unidade_logada():
    from app.models import Unidade

    unidade_id = session.get("unidade_id")
    if not unidade_id:
        return None
    unidade = Unidade.query.get(unidade_id)
    if unidade is None:
        return None
    sessao_cid = session.get("condominio_id")
    if (
        sessao_cid
        and unidade.condominio_id
        and int(unidade.condominio_id) != int(sessao_cid)
    ):
        return None
    return unidade


def superadmin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        usuario = get_current_user()
        if not usuario or usuario.role != Role.SUPERADMIN:
            flash("Acesso restrito ao Super Admin da plataforma.", "danger")
            return redirect(url_for("superadmin_login"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        usuario = get_current_user()
        if not usuario or usuario.role != Role.ADMIN:
            flash("Acesso restrito ao administrador.", "danger")
            return _redirect_login_tenant()
        if not usuario.condominio_id:
            flash(
                "Conta administrativa sem condomínio vinculado. "
                "Contate o Super Admin da plataforma.",
                "danger",
            )
            return _redirect_login_tenant()
        _sincronizar_sessao_tenant(usuario)
        return view(*args, **kwargs)

    return wrapped


def admin_or_assistente_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        usuario = get_current_user()
        if not usuario or usuario.role not in (Role.ADMIN, Role.ASSISTENTE):
            flash("Acesso restrito à administração.", "danger")
            return _redirect_login_tenant()
        if not usuario.condominio_id:
            flash(
                "Conta administrativa sem condomínio vinculado. "
                "Contate o Super Admin da plataforma.",
                "danger",
            )
            return _redirect_login_tenant()
        _sincronizar_sessao_tenant(usuario)
        return view(*args, **kwargs)

    return wrapped


def sindico_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        usuario = get_current_user()
        if not usuario or usuario.role != Role.SINDICO:
            flash("Acesso restrito ao síndico.", "danger")
            slug = session.get("tenant_slug") or "prp"
            return redirect(url_for("sindico_login", slug=slug))
        if not usuario.condominio_id:
            flash(
                "Conta de síndico sem condomínio vinculado. "
                "Contate a administração.",
                "danger",
            )
            slug = session.get("tenant_slug") or "prp"
            return redirect(url_for("sindico_login", slug=slug))
        _sincronizar_sessao_tenant(usuario)
        return view(*args, **kwargs)

    return wrapped


def admin_or_sindico_required(view):
    """Acesso ao Kanban de ocorrências: admin ou síndico do mesmo tenant."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        usuario = get_current_user()
        if not usuario or usuario.role not in (Role.ADMIN, Role.SINDICO):
            flash("Acesso restrito à administração e ao síndico.", "danger")
            return _redirect_login_tenant()
        if not usuario.condominio_id:
            flash(
                "Conta sem condomínio vinculado. Contate a administração.",
                "danger",
            )
            return _redirect_login_tenant()
        _sincronizar_sessao_tenant(usuario)
        return view(*args, **kwargs)

    return wrapped


def unidade_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        unidade = get_unidade_logada()
        if not unidade:
            flash("Autentique-se com bloco, apartamento e senha.", "warning")
            return _redirect_login_tenant()
        if unidade.condominio_id:
            session["condominio_id"] = unidade.condominio_id
            _gravar_tenant_slug_sessao(unidade.condominio_id)
        return view(unidade, *args, **kwargs)

    return wrapped


def portaria_required(view):
    """Acesso à portaria: porteiro, admin do tenant ou Super Admin da plataforma."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        usuario = get_current_user()
        if not usuario:
            return _redirect_login_tenant()
        if usuario.role not in (
            Role.PORTEIRO,
            Role.ADMIN,
            Role.SUPERADMIN,
        ):
            flash("Acesso restrito à portaria.", "danger")
            return _redirect_login_tenant()

        # Super Admin opera a plataforma sem tenant obrigatório.
        if usuario.role == Role.SUPERADMIN:
            return view(*args, **kwargs)

        if not usuario.condominio_id:
            flash(
                "Conta de portaria sem condomínio vinculado. "
                "Contate a administração.",
                "danger",
            )
            return _redirect_login_tenant()

        # Isolamento: sessão sempre no condominio_id do próprio usuário.
        _sincronizar_sessao_tenant(usuario)
        return view(*args, **kwargs)

    return wrapped
