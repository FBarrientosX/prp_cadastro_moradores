import os
from datetime import datetime

from flask import current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename

from app import ALLOWED_EXTENSIONS, db
from app.auth import (
    admin_required,
    gerar_senha_aleatoria,
    get_current_user,
    get_unidade_logada,
    login_unidade,
    login_usuario,
    logout_unidade,
    logout_usuario,
    sindico_required,
    unidade_required,
)
from app.models import (
    Pessoa,
    StatusDocumento,
    StatusUnidade,
    Unidade,
    Usuario,
    Veiculo,
    VinculoPessoa,
)
from app.utils import (
    blocos_equivalentes,
    get_apartamentos_bloco,
    get_condominio_estrutura,
    normalizar_bloco_apartamento,
    normalizar_bloco_codigo,
    validar_unidade,
)


def _contexto_index(**extra):
    base = {
        "condominio_estrutura": get_condominio_estrutura(),
        "bloco": "",
        "apartamento": "",
    }
    base.update(extra)
    return base


def _buscar_unidade(bloco, apartamento):
    return Unidade.query.filter_by(bloco=bloco, apartamento=apartamento).first()


def _parse_data(data_str):
    if not data_str:
        return None
    try:
        return datetime.strptime(data_str, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_pessoas_form(form):
    pessoas = []
    indice = 0
    while True:
        nome = form.get(f"pessoa_{indice}_nome", "").strip()
        if not nome:
            break

        vinculo = form.get(f"pessoa_{indice}_vinculo", "").strip()
        if vinculo not in VinculoPessoa.CHOICES:
            raise ValueError(f"Vínculo inválido para {nome}.")

        pessoas.append(
            {
                "nome_completo": nome,
                "cpf": form.get(f"pessoa_{indice}_cpf", "").strip(),
                "vinculo": vinculo,
                "telefone": form.get(f"pessoa_{indice}_telefone", "").strip(),
                "email": form.get(f"pessoa_{indice}_email", "").strip() or None,
                "parentesco": form.get(f"pessoa_{indice}_parentesco", "").strip()
                or None,
                "data_nascimento": _parse_data(
                    form.get(f"pessoa_{indice}_data_nascimento", "")
                ),
                "is_responsavel": form.get(f"pessoa_{indice}_is_responsavel") == "on",
            }
        )
        indice += 1

    if not pessoas:
        raise ValueError("Informe ao menos uma pessoa.")

    if not any(p["is_responsavel"] for p in pessoas):
        raise ValueError("Marque ao menos uma pessoa como responsável.")

    return pessoas


def _parse_veiculos_form(form):
    veiculos = []
    indice = 0
    while True:
        placa = form.get(f"veiculo_{indice}_placa", "").strip()
        if not placa:
            break

        veiculos.append(
            {
                "placa": placa.upper(),
                "marca": form.get(f"veiculo_{indice}_marca", "").strip(),
                "cor": form.get(f"veiculo_{indice}_cor", "").strip(),
            }
        )
        indice += 1

    return veiculos


def _extensao_permitida(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _salvar_documento(unidade, arquivo):
    if not arquivo or not arquivo.filename:
        return
    if not _extensao_permitida(arquivo.filename):
        raise ValueError("Formato de arquivo inválido. Aceitos: PDF, PNG, JPG, JPEG.")

    extensao = arquivo.filename.rsplit(".", 1)[1].lower()
    nome_arquivo = f"doc_bloco{unidade.bloco}_apto{unidade.apartamento}.{extensao}"
    nome_seguro = secure_filename(nome_arquivo)

    caminho_completo = os.path.join(current_app.config["UPLOAD_FOLDER"], nome_seguro)
    arquivo.save(caminho_completo)

    unidade.documento_path = f"uploads/documentos/{nome_seguro}"
    unidade.documento_status = StatusDocumento.PENDENTE


def _salvar_pessoas_veiculos(unidade, pessoas_data, veiculos_data):
    for pessoa in unidade.pessoas.all():
        db.session.delete(pessoa)
    for veiculo in unidade.veiculos.all():
        db.session.delete(veiculo)

    for dados in pessoas_data:
        db.session.add(Pessoa(unidade_id=unidade.id, **dados))

    for dados in veiculos_data:
        db.session.add(Veiculo(unidade_id=unidade.id, **dados))


def index():
    return render_template("index.html", **_contexto_index())


def verificar_unidade():
    bloco, apartamento = normalizar_bloco_apartamento(
        request.form.get("bloco", ""),
        request.form.get("apartamento", ""),
    )

    if not validar_unidade(bloco, apartamento):
        flash("Combinação de bloco e apartamento inválida.", "danger")
        return render_template(
            "index.html",
            **_contexto_index(bloco=bloco, apartamento=apartamento),
        )

    unidade = _buscar_unidade(bloco, apartamento)

    if not unidade:
        session["cadastro_bloco"] = bloco
        session["cadastro_apartamento"] = apartamento
        return redirect(url_for("cadastro_inicial"))

    if unidade.status == StatusUnidade.REPROVADA:
        db.session.delete(unidade)
        db.session.commit()
        session["cadastro_bloco"] = bloco
        session["cadastro_apartamento"] = apartamento
        return redirect(url_for("cadastro_inicial"))

    senha = request.form.get("senha", "").strip()
    exige_senha = unidade.status in (
        StatusUnidade.PENDENTE,
        StatusUnidade.APROVADA,
        StatusUnidade.REGISTRADA,
    )

    if exige_senha:
        if not senha:
            return render_template(
                "index.html",
                **_contexto_index(
                    exige_senha=True,
                    bloco=bloco,
                    apartamento=apartamento,
                ),
            )

        if not unidade.check_password(senha):
            flash("Senha incorreta.", "danger")
            return render_template(
                "index.html",
                **_contexto_index(
                    exige_senha=True,
                    bloco=bloco,
                    apartamento=apartamento,
                ),
            )

    if unidade.status == StatusUnidade.PENDENTE:
        return render_template(
            "index.html",
            **_contexto_index(
                pendente=True,
                bloco=bloco,
                apartamento=apartamento,
            ),
        )

    login_unidade(unidade)
    return redirect(url_for("atualizar_dados"))


def cadastro_inicial():
    bloco = session.get("cadastro_bloco")
    apartamento = session.get("cadastro_apartamento")

    if not bloco or not apartamento or not validar_unidade(bloco, apartamento):
        flash("Selecione um bloco e apartamento válidos.", "warning")
        return redirect(url_for("index"))

    if _buscar_unidade(bloco, apartamento):
        flash("Esta unidade já possui cadastro.", "warning")
        return redirect(url_for("index"))

    return render_template(
        "cadastro_morador.html",
        bloco=bloco,
        apartamento=apartamento,
        modo="cadastro",
        vinculos=VinculoPessoa.CHOICES,
    )


@unidade_required
def atualizar_dados(unidade):
    if unidade.status not in (StatusUnidade.APROVADA, StatusUnidade.REGISTRADA):
        flash("Esta unidade não pode ser atualizada no momento.", "warning")
        return redirect(url_for("index"))

    pessoas = unidade.pessoas.all()
    veiculos = unidade.veiculos.all()

    return render_template(
        "cadastro_morador.html",
        bloco=unidade.bloco,
        apartamento=unidade.apartamento,
        modo="atualizacao",
        vinculos=VinculoPessoa.CHOICES,
        pessoas=pessoas,
        veiculos=veiculos,
        unidade=unidade,
    )


def salvar_cadastro():
    bloco = session.get("cadastro_bloco")
    apartamento = session.get("cadastro_apartamento")
    unidade_logada = get_unidade_logada()
    modo_atualizacao = unidade_logada is not None

    if modo_atualizacao:
        unidade = unidade_logada
        bloco = unidade.bloco
        apartamento = unidade.apartamento
    else:
        if not bloco or not apartamento:
            flash("Sessão expirada. Selecione bloco e apartamento novamente.", "warning")
            return redirect(url_for("index"))
        unidade = None

    bloco, apartamento = normalizar_bloco_apartamento(bloco, apartamento)

    if not validar_unidade(bloco, apartamento):
        flash("Combinação de bloco e apartamento inválida.", "danger")
        return redirect(url_for("index"))

    senha = request.form.get("senha", "").strip()
    confirmar_senha = request.form.get("confirmar_senha", "").strip()

    try:
        pessoas_data = _parse_pessoas_form(request.form)
        veiculos_data = _parse_veiculos_form(request.form)

        if modo_atualizacao:
            if unidade.status not in (StatusUnidade.APROVADA, StatusUnidade.REGISTRADA):
                raise ValueError("Esta unidade não pode ser atualizada.")

            if senha:
                if senha != confirmar_senha:
                    raise ValueError("As senhas não conferem.")
                if len(senha) < 6:
                    raise ValueError("A senha deve ter ao menos 6 caracteres.")
                unidade.set_password(senha)
        else:
            if _buscar_unidade(bloco, apartamento):
                raise ValueError("Esta unidade já possui cadastro.")

            if not senha or senha != confirmar_senha:
                raise ValueError("Informe e confirme a senha do cadastro.")
            if len(senha) < 6:
                raise ValueError("A senha deve ter ao menos 6 caracteres.")

            unidade = Unidade(
                bloco=bloco,
                apartamento=apartamento,
                status=StatusUnidade.PENDENTE,
            )
            unidade.set_password(senha)
            db.session.add(unidade)
            db.session.flush()

        _salvar_pessoas_veiculos(unidade, pessoas_data, veiculos_data)

        arquivo = request.files.get("documento")
        if arquivo and arquivo.filename:
            _salvar_documento(unidade, arquivo)

        db.session.commit()

        if modo_atualizacao:
            flash("Dados atualizados com sucesso.", "success")
            return redirect(url_for("atualizar_dados"))

        session.pop("cadastro_bloco", None)
        session.pop("cadastro_apartamento", None)
        flash(
            "Cadastro enviado! Aguarde a aprovação do síndico do seu bloco.",
            "success",
        )
        return redirect(url_for("index"))

    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        if modo_atualizacao:
            return redirect(url_for("atualizar_dados"))
        return redirect(url_for("cadastro_inicial"))


def sindico_login():
    if get_current_user() and get_current_user().is_sindico:
        return redirect(url_for("sindico_dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        usuario = Usuario.query.filter_by(username=username, role="sindico").first()
        if usuario and usuario.check_password(password):
            login_usuario(usuario)
            return redirect(url_for("sindico_dashboard"))

        flash("Usuário ou senha inválidos.", "danger")

    return render_template("login.html", titulo="Login do Síndico", action="sindico")


def sindico_logout():
    logout_usuario()
    flash("Sessão encerrada.", "info")
    return redirect(url_for("sindico_login"))


@sindico_required
def sindico_dashboard():
    usuario = get_current_user()
    bloco_codigo = normalizar_bloco_codigo(usuario.bloco_responsavel)
    todos_apartamentos = get_apartamentos_bloco(bloco_codigo)

    unidades_cadastradas = Unidade.query.filter_by(bloco=bloco_codigo).all()
    unidades_por_apto = {u.apartamento: u for u in unidades_cadastradas}

    mapa_bloco = []
    for apto in todos_apartamentos:
        unidade = unidades_por_apto.get(apto)
        mapa_bloco.append(
            {
                "apartamento": apto,
                "unidade": unidade,
                "status": unidade.status if unidade else "Aguardando Morador",
            }
        )

    return render_template(
        "dashboard_sindico.html",
        mapa_bloco=mapa_bloco,
        current_user=usuario,
    )


@sindico_required
def sindico_aprovar(unidade_id):
    usuario = get_current_user()
    unidade = Unidade.query.get_or_404(unidade_id)

    if not blocos_equivalentes(unidade.bloco, usuario.bloco_responsavel):
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
    usuario = get_current_user()
    unidade = Unidade.query.get_or_404(unidade_id)

    if not blocos_equivalentes(unidade.bloco, usuario.bloco_responsavel):
        flash("Você não tem permissão para esta unidade.", "danger")
        return redirect(url_for("sindico_dashboard"))

    if unidade.status != StatusUnidade.PENDENTE:
        flash("Apenas cadastros pendentes podem ser reprovados.", "warning")
        return redirect(url_for("sindico_dashboard"))

    db.session.delete(unidade)
    db.session.commit()
    flash(f"Cadastro da unidade {unidade.identificador} reprovado e removido.", "info")
    return redirect(url_for("sindico_dashboard"))


def admin_login():
    if get_current_user() and get_current_user().is_admin:
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        usuario = Usuario.query.filter_by(username=username, role="admin").first()
        if usuario and usuario.check_password(password):
            login_usuario(usuario)
            return redirect(url_for("admin_dashboard"))

        flash("Usuário ou senha inválidos.", "danger")

    return render_template("login.html", titulo="Login do Administrador", action="admin")


def admin_logout():
    logout_usuario()
    flash("Sessão encerrada.", "info")
    return redirect(url_for("admin_login"))


@admin_required
def admin_dashboard():
    aguardando_registro = (
        Unidade.query.filter_by(status=StatusUnidade.APROVADA)
        .order_by(Unidade.bloco, Unidade.apartamento)
        .all()
    )
    finalizados = (
        Unidade.query.filter_by(status=StatusUnidade.REGISTRADA)
        .order_by(Unidade.bloco, Unidade.apartamento)
        .all()
    )

    return render_template(
        "dashboard_admin.html",
        aguardando_registro=aguardando_registro,
        finalizados=finalizados,
    )


@admin_required
def admin_registrar(unidade_id):
    unidade = Unidade.query.get_or_404(unidade_id)

    if unidade.status != StatusUnidade.APROVADA:
        flash("Apenas unidades aprovadas podem ser registradas.", "warning")
        return redirect(url_for("admin_dashboard"))

    unidade.status = StatusUnidade.REGISTRADA
    db.session.commit()
    flash(f"Unidade {unidade.identificador} marcada como registrada.", "success")
    return redirect(url_for("admin_dashboard"))


@admin_required
def admin_resetar_senha(unidade_id):
    unidade = Unidade.query.get_or_404(unidade_id)
    nova_senha = request.form.get("nova_senha", "").strip()

    if not nova_senha:
        nova_senha = gerar_senha_aleatoria()

    if len(nova_senha) < 6:
        flash("A senha deve ter ao menos 6 caracteres.", "danger")
        return redirect(url_for("admin_dashboard"))

    unidade.set_password(nova_senha)
    db.session.commit()
    flash(
        f"Senha da unidade {unidade.identificador} redefinida: {nova_senha}",
        "success",
    )
    return redirect(url_for("admin_dashboard"))


@admin_required
def admin_validar_documento(unidade_id):
    unidade = Unidade.query.get_or_404(unidade_id)
    unidade.documento_status = StatusDocumento.ENTREGUE
    db.session.commit()
    flash(
        f"Documento da unidade Bloco {unidade.bloco}, Apto {unidade.apartamento} "
        f"marcado como entregue/validado.",
        "success",
    )
    return redirect(url_for("admin_dashboard"))


def init_app(app):
    app.add_url_rule("/", "index", index, methods=["GET"])
    app.add_url_rule(
        "/verificar-unidade", "verificar_unidade", verificar_unidade, methods=["POST"]
    )
    app.add_url_rule(
        "/cadastro-inicial", "cadastro_inicial", cadastro_inicial, methods=["GET"]
    )
    app.add_url_rule(
        "/atualizar-dados", "atualizar_dados", atualizar_dados, methods=["GET"]
    )
    app.add_url_rule(
        "/salvar-cadastro", "salvar_cadastro", salvar_cadastro, methods=["POST"]
    )

    app.add_url_rule(
        "/sindico/login", "sindico_login", sindico_login, methods=["GET", "POST"]
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
        "/admin/login", "admin_login", admin_login, methods=["GET", "POST"]
    )
    app.add_url_rule("/admin/logout", "admin_logout", admin_logout, methods=["GET"])
    app.add_url_rule("/admin", "admin_dashboard", admin_dashboard, methods=["GET"])
    app.add_url_rule(
        "/admin/registrar/<int:unidade_id>",
        "admin_registrar",
        admin_registrar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/resetar-senha/<int:unidade_id>",
        "admin_resetar_senha",
        admin_resetar_senha,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/validar-documento/<int:unidade_id>",
        "admin_validar_documento",
        admin_validar_documento,
        methods=["POST"],
    )
