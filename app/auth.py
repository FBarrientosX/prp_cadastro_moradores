import secrets
import string
from functools import wraps

from flask import flash, redirect, session, url_for

from app.models import Role, Usuario


def gerar_senha_aleatoria(tamanho=8):
    alfabeto = string.ascii_letters + string.digits
    return "".join(secrets.choice(alfabeto) for _ in range(tamanho))


def login_usuario(usuario):
    session.clear()
    session["user_id"] = usuario.id
    session["role"] = usuario.role
    session.permanent = True


def logout_usuario():
    session.pop("user_id", None)
    session.pop("role", None)


def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return Usuario.query.get(user_id)


def login_unidade(unidade):
    session["unidade_id"] = unidade.id
    session["unidade_bloco"] = unidade.bloco
    session["unidade_apartamento"] = unidade.apartamento


def logout_unidade():
    for chave in (
        "unidade_id",
        "unidade_bloco",
        "unidade_apartamento",
        "cadastro_bloco",
        "cadastro_apartamento",
    ):
        session.pop(chave, None)


def get_unidade_logada():
    from app.models import Unidade

    unidade_id = session.get("unidade_id")
    if not unidade_id:
        return None
    return Unidade.query.get(unidade_id)


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        usuario = get_current_user()
        if not usuario or usuario.role != Role.ADMIN:
            flash("Acesso restrito ao administrador.", "danger")
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)

    return wrapped


def admin_or_assistente_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        usuario = get_current_user()
        if not usuario or usuario.role not in (Role.ADMIN, Role.ASSISTENTE):
            flash("Acesso restrito à administração.", "danger")
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)

    return wrapped


def sindico_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        usuario = get_current_user()
        if not usuario or usuario.role != Role.SINDICO:
            flash("Acesso restrito ao síndico.", "danger")
            return redirect(url_for("sindico_login"))
        return view(*args, **kwargs)

    return wrapped


def unidade_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        unidade = get_unidade_logada()
        if not unidade:
            flash("Autentique-se com bloco, apartamento e senha.", "warning")
            return redirect(url_for("index"))
        return view(unidade, *args, **kwargs)

    return wrapped
