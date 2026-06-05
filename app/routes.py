from datetime import datetime
import traceback

from flask import flash, redirect, render_template, request, session, url_for

from app import db
from app.auth import (
    admin_required,
    admin_or_assistente_required,
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
from app.email_service import (
    enviar_email_validacao_parcial,
    enviar_email_validacao_sucesso,
)
from app.models import (
    LogAuditoria,
    Pessoa,
    Role,
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


def _registrar_auditoria(usuario, mensagem):
    db.session.add(
        LogAuditoria(
            usuario_id=usuario.id,
            mensagem=mensagem,
        )
    )


def _adicionar_notificacao_sindico(unidade, nome_morador, motivo):
    nova_linha = (
        f"O cadastro do morador {nome_morador} foi reprovado e removido pelo síndico "
        f"responsável. Motivo informado: {motivo}.\n"
        "Por favor, procure o síndico do seu bloco para maiores orientações e "
        "esclarecimentos antes de tentar cadastrar esta pessoa novamente."
    )
    if unidade.notificacao_sindico:
        unidade.notificacao_sindico = f"{unidade.notificacao_sindico}\n\n{nova_linha}"
    else:
        unidade.notificacao_sindico = nova_linha


def _emails_unicos(pessoas):
    emails = []
    vistos = set()
    for pessoa in pessoas:
        if not pessoa.email:
            continue
        email = pessoa.email.strip()
        if not email:
            continue
        chave = email.lower()
        if chave in vistos:
            continue
        vistos.add(chave)
        emails.append(email)
    return emails


def _parse_data(data_str):
    if not data_str:
        return None
    try:
        return datetime.strptime(data_str, "%Y-%m-%d").date()
    except ValueError:
        return None


def _calcular_idade(data_nascimento):
    if not data_nascimento:
        return None
    hoje = datetime.now().date()
    idade = hoje.year - data_nascimento.year
    if (hoje.month, hoje.day) < (data_nascimento.month, data_nascimento.day):
        idade -= 1
    return idade


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

        data_nascimento = _parse_data(form.get(f"pessoa_{indice}_data_nascimento", ""))
        idade = _calcular_idade(data_nascimento) if data_nascimento else None
        is_menor = idade is not None and idade < 18
        is_responsavel = form.get(f"pessoa_{indice}_is_responsavel") == "on"

        cpf = form.get(f"pessoa_{indice}_cpf", "").strip()
        telefone = form.get(f"pessoa_{indice}_telefone", "").strip()
        email = form.get(f"pessoa_{indice}_email", "").strip()
        autoriza_interfone_raw = (
            form.get(f"pessoa_{indice}_autoriza_interfone", "").strip().lower()
        )
        autoriza_interfone = autoriza_interfone_raw == "true"

        if not is_menor and not cpf:
            raise ValueError(f"CPF é obrigatório para {nome} (maior de idade).")

        if is_responsavel and not is_menor:
            if not telefone:
                raise ValueError(
                    f"Telefone é obrigatório para o responsável {nome} (maior de idade)."
                )
            if not email:
                raise ValueError(
                    f"E-mail é obrigatório para o responsável {nome} (maior de idade)."
                )

        pessoas.append(
            {
                "nome_completo": nome,
                "cpf": cpf or "",
                "vinculo": vinculo,
                "telefone": telefone or "",
                "email": email or None,
                "parentesco": form.get(f"pessoa_{indice}_parentesco", "").strip()
                or None,
                "data_nascimento": data_nascimento,
                "is_responsavel": is_responsavel,
                "autoriza_interfone": autoriza_interfone,
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


def _responsavel_e_locatario(pessoas_data):
    return any(
        p["is_responsavel"] and p["vinculo"] == VinculoPessoa.LOCATARIO
        for p in pessoas_data
    )


def _parse_proprietario_form(form):
    return {
        "proprietario_nome": form.get("proprietario_nome", "").strip() or None,
        "proprietario_telefone": form.get("proprietario_telefone", "").strip() or None,
        "proprietario_email": form.get("proprietario_email", "").strip() or None,
    }


def _salvar_pessoas_veiculos(unidade, pessoas_data, veiculos_data):
    try:
        for pessoa in unidade.pessoas.all():
            db.session.delete(pessoa)
        for veiculo in unidade.veiculos.all():
            db.session.delete(veiculo)

        for dados in pessoas_data:
            db.session.add(Pessoa(unidade_id=unidade.id, **dados))

        for dados in veiculos_data:
            db.session.add(Veiculo(unidade_id=unidade.id, **dados))
    except Exception as exc:
        db.session.rollback()
        raise RuntimeError("Falha ao atualizar moradores e veículos.") from exc


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


@unidade_required
def limpar_notificacao_sindico(unidade):
    unidade.notificacao_sindico = None
    db.session.commit()
    flash("Aviso do síndico removido da sua tela.", "success")
    return redirect(url_for("atualizar_dados"))


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
                documento_status=StatusDocumento.PENDENTE,
            )
            unidade.set_password(senha)
            db.session.add(unidade)
            db.session.flush()

        _salvar_pessoas_veiculos(unidade, pessoas_data, veiculos_data)

        if _responsavel_e_locatario(pessoas_data):
            if not modo_atualizacao:
                unidade.contrato_locacao_status = StatusDocumento.PENDENTE
            elif unidade.contrato_locacao_status == StatusDocumento.NAO_APLICAVEL:
                unidade.contrato_locacao_status = StatusDocumento.PENDENTE

            dados_proprietario = _parse_proprietario_form(request.form)
            unidade.proprietario_nome = dados_proprietario["proprietario_nome"]
            unidade.proprietario_telefone = dados_proprietario["proprietario_telefone"]
            unidade.proprietario_email = dados_proprietario["proprietario_email"]
            if not modo_atualizacao:
                unidade.proprietario_cpf = None
        else:
            unidade.contrato_locacao_drive_id = None
            unidade.contrato_locacao_url = None
            unidade.contrato_locacao_status = StatusDocumento.NAO_APLICAVEL
            unidade.proprietario_nome = None
            unidade.proprietario_cpf = None
            unidade.proprietario_telefone = None
            unidade.proprietario_email = None
            unidade.contrato_locacao_drive_id = None
            unidade.contrato_locacao_url = None

        if modo_atualizacao:
            unidade.status = StatusUnidade.PENDENTE
            unidade.data_alteracao = datetime.utcnow()

        db.session.commit()

        if modo_atualizacao:
            flash(
                "Dados atualizados e cadastro reenviado para nova aprovação do síndico.",
                "success",
            )
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
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        flash(
            "Ocorreu um erro ao salvar o cadastro. Tente novamente em instantes.",
            "danger",
        )
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


@sindico_required
def sindico_reprovar_pessoa(pessoa_id):
    usuario = get_current_user()
    pessoa = Pessoa.query.get_or_404(pessoa_id)
    unidade = pessoa.unidade

    if not blocos_equivalentes(unidade.bloco, usuario.bloco_responsavel):
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
    usuario = get_current_user()
    unidade = Unidade.query.get_or_404(unidade_id)

    if not blocos_equivalentes(unidade.bloco, usuario.bloco_responsavel):
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

    if moradores_aprovados:
        unidade.status = StatusUnidade.APROVADA
        _registrar_auditoria(
            usuario,
            f"O síndico {usuario.username} finalizou a validação da unidade "
            f"'{unidade_identificador}' com {len(moradores_aprovados)} morador(es) aprovado(s).",
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
    else:
        flash(
            f"Todos os moradores da unidade {unidade_identificador} foram reprovados. "
            "A unidade voltou para Aguardando Morador.",
            "info",
        )

    return redirect(url_for("sindico_dashboard"))


def admin_login():
    usuario_logado = get_current_user()
    if usuario_logado and usuario_logado.role in (Role.ADMIN, Role.ASSISTENTE):
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        usuario = Usuario.query.filter(
            Usuario.username == username,
            Usuario.role.in_([Role.ADMIN, Role.ASSISTENTE]),
        ).first()
        if usuario and usuario.check_password(password):
            login_usuario(usuario)
            return redirect(url_for("admin_dashboard"))

        flash("Usuário ou senha inválidos.", "danger")

    return render_template("login.html", titulo="Login do Administrador", action="admin")


def admin_logout():
    logout_usuario()
    flash("Sessão encerrada.", "info")
    return redirect(url_for("admin_login"))


@admin_or_assistente_required
def admin_dashboard():
    usuario = get_current_user()
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
    sindicos = (
        Usuario.query.filter_by(role="sindico")
        .order_by(Usuario.bloco_responsavel, Usuario.username)
        .all()
    )
    equipe_acessos = (
        Usuario.query.filter(Usuario.role.in_([Role.ASSISTENTE, Role.SINDICO]))
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


@admin_or_assistente_required
def admin_registrar(unidade_id):
    unidade = Unidade.query.get_or_404(unidade_id)

    if unidade.status != StatusUnidade.APROVADA:
        flash("Apenas unidades aprovadas podem ser registradas.", "warning")
        return redirect(url_for("admin_dashboard"))

    unidade.status = StatusUnidade.REGISTRADA
    db.session.commit()
    flash(f"Unidade {unidade.identificador} marcada como registrada.", "success")
    return redirect(url_for("admin_dashboard"))


@admin_or_assistente_required
def admin_resetar_senha(unidade_id):
    usuario = get_current_user()
    if usuario.role != Role.ADMIN:
        flash("Acesso negado.", "danger")
        return redirect(url_for("admin_dashboard"))

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


@admin_or_assistente_required
def admin_excluir_unidade(unidade_id):
    usuario = get_current_user()
    if usuario.role != Role.ADMIN:
        flash("Acesso negado.", "danger")
        return redirect(url_for("admin_dashboard"))

    unidade = Unidade.query.get_or_404(unidade_id)

    db.session.delete(unidade)
    db.session.commit()

    flash(
        "Cadastro da unidade apagado com sucesso. Ela está livre para novo registro.",
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


@admin_required
def admin_validar_contrato_locacao(unidade_id):
    unidade = Unidade.query.get_or_404(unidade_id)

    if unidade.contrato_locacao_status == StatusDocumento.NAO_APLICAVEL:
        flash(
            f"Contrato de locação não se aplica à unidade Bloco {unidade.bloco}, "
            f"Apto {unidade.apartamento}.",
            "warning",
        )
        return redirect(url_for("admin_dashboard"))

    unidade.contrato_locacao_status = StatusDocumento.ENTREGUE
    db.session.commit()
    flash(
        f"Contrato de locação da unidade Bloco {unidade.bloco}, "
        f"Apto {unidade.apartamento} marcado como entregue/validado.",
        "success",
    )
    return redirect(url_for("admin_dashboard"))


@admin_required
def admin_validar_documentos(unidade_id):
    unidade = Unidade.query.get_or_404(unidade_id)
    unidade.documento_status = StatusDocumento.ENTREGUE
    if unidade.contrato_locacao_status != StatusDocumento.NAO_APLICAVEL:
        unidade.contrato_locacao_status = StatusDocumento.ENTREGUE

    db.session.commit()
    flash(
        f"Documentos da unidade Bloco {unidade.bloco}, Apto {unidade.apartamento} "
        f"marcados como entregues/validados.",
        "success",
    )
    return redirect(url_for("admin_dashboard"))


@admin_required
def admin_atualizar_status_documentos(unidade_id):
    unidade = Unidade.query.get_or_404(unidade_id)
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
    return redirect(url_for("admin_dashboard"))


@admin_required
def admin_alterar_senha_sindico():
    username = request.form.get("username", "").strip()
    nova_senha = request.form.get("nova_senha", "").strip()

    if not username or not nova_senha:
        flash("Informe o síndico e a nova senha.", "danger")
        return redirect(url_for("admin_dashboard"))

    if len(nova_senha) < 6:
        flash("A nova senha deve ter ao menos 6 caracteres.", "danger")
        return redirect(url_for("admin_dashboard"))

    sindico = Usuario.query.filter_by(username=username, role="sindico").first()
    if not sindico:
        flash("Síndico não encontrado.", "danger")
        return redirect(url_for("admin_dashboard"))

    sindico.set_password(nova_senha)
    db.session.commit()
    flash(
        f"Senha do síndico do {sindico.bloco_responsavel} atualizada com sucesso.",
        "success",
    )
    return redirect(url_for("admin_dashboard"))


@admin_required
def admin_salvar_proprietario(unidade_id):
    unidade = Unidade.query.get_or_404(unidade_id)
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
    return redirect(url_for("admin_dashboard"))


@admin_required
def admin_criar_usuario():
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
            bloco_responsavel=bloco_responsavel if role == Role.SINDICO else None,
        )
        novo_usuario.set_password(senha)
        db.session.add(novo_usuario)
        db.session.commit()

        flash("Usuário criado com sucesso.", "success")
        return redirect(url_for("admin_dashboard"))

    return render_template("criar_usuario.html", blocos=blocos)


@admin_required
def admin_excluir_usuario(usuario_id):
    usuario_logado = get_current_user()
    usuario_alvo = Usuario.query.get_or_404(usuario_id)

    if usuario_alvo.id == usuario_logado.id:
        flash("Você não pode excluir o próprio acesso.", "danger")
        return redirect(url_for("admin_dashboard"))

    if usuario_alvo.role not in (Role.ASSISTENTE, Role.SINDICO):
        flash("Apenas acessos de assistente ou síndico podem ser revogados aqui.", "warning")
        return redirect(url_for("admin_dashboard"))

    username_alvo = usuario_alvo.username
    role_alvo = usuario_alvo.role
    db.session.delete(usuario_alvo)
    _registrar_auditoria(
        usuario_logado,
        f"Acesso do {role_alvo} '{username_alvo}' foi revogado por "
        f"'{usuario_logado.username}'.",
    )
    db.session.commit()

    flash(f"Acesso de '{username_alvo}' revogado com sucesso.", "success")
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
        "/limpar-notificacao-sindico",
        "limpar_notificacao_sindico",
        limpar_notificacao_sindico,
        methods=["POST"],
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
        "/admin/login", "admin_login", admin_login, methods=["GET", "POST"]
    )
    app.add_url_rule("/admin/logout", "admin_logout", admin_logout, methods=["GET"])
    app.add_url_rule("/admin", "admin_dashboard", admin_dashboard, methods=["GET"])
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
        "/admin/resetar-senha/<int:unidade_id>",
        "admin_resetar_senha",
        admin_resetar_senha,
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
