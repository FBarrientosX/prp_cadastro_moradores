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


def resolver_condominio_id(usuario=None, unidade=None):
    """Resolve condominio_id a partir do ator logado, da unidade ou da sessão."""
    if usuario is not None and getattr(usuario, "condominio_id", None):
        return usuario.condominio_id
    if unidade is not None and getattr(unidade, "condominio_id", None):
        return unidade.condominio_id
    if session.get("cadastro_condominio_id"):
        return session["cadastro_condominio_id"]
    sessao_id = session.get("condominio_id")
    if sessao_id:
        return sessao_id
    return obter_condominio_padrao_id()


def _gravar_tenant_slug_sessao(condominio_id):
    if not condominio_id:
        return
    condominio = Condominio.query.get(condominio_id)
    if condominio and condominio.slug:
        session["tenant_slug"] = condominio.slug


def login_usuario(usuario):
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
    return Usuario.query.get(user_id)


def login_unidade(unidade):
    session["unidade_id"] = unidade.id
    session["unidade_bloco"] = unidade.bloco
    session["unidade_apartamento"] = unidade.apartamento
    session["condominio_id"] = unidade.condominio_id
    _gravar_tenant_slug_sessao(unidade.condominio_id)


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
    return Unidade.query.get(unidade_id)


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
            slug = session.get("tenant_slug") or "prp"
            return redirect(url_for("tenant_login", slug=slug))
        return view(*args, **kwargs)

    return wrapped


def admin_or_assistente_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        usuario = get_current_user()
        if not usuario or usuario.role not in (Role.ADMIN, Role.ASSISTENTE):
            flash("Acesso restrito à administração.", "danger")
            slug = session.get("tenant_slug") or "prp"
            return redirect(url_for("tenant_login", slug=slug))
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
        return view(*args, **kwargs)

    return wrapped


def unidade_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        unidade = get_unidade_logada()
        if not unidade:
            flash("Autentique-se com bloco, apartamento e senha.", "warning")
            slug = session.get("tenant_slug") or "prp"
            return redirect(url_for("tenant_login", slug=slug))
        return view(unidade, *args, **kwargs)

    return wrapped


def portaria_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        usuario = get_current_user()
        if not usuario or usuario.role not in (Role.PORTEIRO, Role.ADMIN):
            flash("Acesso restrito à portaria.", "danger")
            return redirect(url_for("portaria_login"))
        return view(*args, **kwargs)

    return wrapped
