from datetime import datetime, timedelta
from functools import wraps
import os
import random
import string
import traceback

from flask import flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import and_, case, func, or_
from werkzeug.security import check_password_hash, generate_password_hash

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
    enviar_email_nova_reserva,
    enviar_email_redefinicao_senha,
    enviar_email_reprovacao,
    enviar_email_resposta_reserva,
    enviar_email_validacao_parcial,
    enviar_email_validacao_sucesso,
)
from app.models import (
    Cupom,
    EspacoComum,
    LogAuditoria,
    Parceiro,
    Pessoa,
    Reserva,
    ResgateCupom,
    Role,
    StatusDocumento,
    StatusUnidade,
    Unidade,
    Usuario,
    Veiculo,
    VinculoPessoa,
)
from app.utils import (
    SALT_RECUPERACAO_MORADOR,
    SALT_RECUPERACAO_PARCEIRO,
    blocos_equivalentes,
    gerar_token_redefinicao,
    get_apartamentos_bloco,
    get_condominio_estrutura,
    normalizar_bloco_apartamento,
    normalizar_bloco_codigo,
    validar_unidade,
    verificar_token_redefinicao,
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


def _buscar_unidade_e_email_login(email):
    """Localiza unidade e o e-mail cadastrado que corresponde ao login informado."""
    email_normalizado = email.strip().lower()
    if not email_normalizado:
        return None, None

    unidades = (
        db.session.query(Unidade)
        .outerjoin(
            Pessoa,
            and_(
                Pessoa.unidade_id == Unidade.id,
                Pessoa.is_responsavel.is_(True),
            ),
        )
        .filter(
            or_(
                func.lower(Unidade.proprietario_email) == email_normalizado,
                func.lower(Pessoa.email) == email_normalizado,
            )
        )
        .all()
    )
    if not unidades:
        return None, None

    for unidade in unidades:
        responsavel = unidade.pessoas.filter_by(is_responsavel=True).first()
        if (
            responsavel
            and responsavel.email
            and responsavel.email.strip().lower() == email_normalizado
        ):
            return unidade, responsavel.email.strip()

    for unidade in unidades:
        if (
            unidade.proprietario_email
            and unidade.proprietario_email.strip().lower() == email_normalizado
        ):
            return unidade, unidade.proprietario_email.strip()

    return None, None


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


def _montar_analytics_clube():
    total_resgates = db.session.query(func.count(ResgateCupom.id)).scalar() or 0
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

    resgates_por_bloco_rows = (
        db.session.query(
            Unidade.bloco,
            func.count(ResgateCupom.id).label("total"),
        )
        .join(ResgateCupom, ResgateCupom.unidade_id == Unidade.id)
        .group_by(Unidade.bloco)
        .order_by(func.count(ResgateCupom.id).desc())
        .all()
    )

    top_unidades_rows = (
        db.session.query(
            Unidade.bloco,
            Unidade.apartamento,
            func.count(ResgateCupom.id).label("total"),
        )
        .join(ResgateCupom, ResgateCupom.unidade_id == Unidade.id)
        .group_by(Unidade.id, Unidade.bloco, Unidade.apartamento)
        .order_by(func.count(ResgateCupom.id).desc())
        .limit(10)
        .all()
    )

    status_rows = (
        db.session.query(ResgateCupom.status, func.count(ResgateCupom.id))
        .group_by(ResgateCupom.status)
        .all()
    )
    status_map = {status: quantidade for status, quantidade in status_rows}
    resgates_ativos = status_map.get("Ativo", 0)
    resgates_utilizados = status_map.get("Utilizado", 0)
    taxa_conversao = (
        round((resgates_utilizados / total_resgates) * 100, 1) if total_resgates else 0.0
    )

    evolucao_rows = (
        db.session.query(
            func.date(ResgateCupom.data_resgate).label("data"),
            func.count(ResgateCupom.id).label("total"),
        )
        .group_by(func.date(ResgateCupom.data_resgate))
        .order_by(func.date(ResgateCupom.data_resgate))
        .all()
    )

    parceiro_popular_row = (
        db.session.query(
            Parceiro.nome_empresa,
            func.count(ResgateCupom.id).label("total"),
        )
        .join(Cupom, Cupom.parceiro_id == Parceiro.id)
        .join(ResgateCupom, ResgateCupom.cupom_id == Cupom.id)
        .group_by(Parceiro.id, Parceiro.nome_empresa)
        .order_by(func.count(ResgateCupom.id).desc())
        .first()
    )

    cupons_conversao_rows = (
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
        .group_by(Cupom.id, Cupom.titulo, Parceiro.nome_empresa)
        .all()
    )

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

        pessoa_id = None
        pessoa_id_raw = form.get(f"pessoa_{indice}_id", "").strip()
        if pessoa_id_raw:
            try:
                pessoa_id = int(pessoa_id_raw)
            except ValueError:
                raise ValueError(f"Identificador inválido para o morador {nome}.")

        pessoas.append(
            {
                "id": pessoa_id,
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


def _normalizar_texto_comparacao(valor):
    if not valor:
        return ""
    return " ".join(str(valor).strip().lower().split())


def _normalizar_placa(placa):
    return "".join(ch for ch in str(placa or "").upper() if ch.isalnum())


def _somente_digitos(valor):
    return "".join(ch for ch in str(valor or "") if ch.isdigit())


def _responsavel_dados_pessoas(pessoas_data):
    return next((p for p in pessoas_data if p["is_responsavel"]), None)


def _responsavel_pessoa_unidade(unidade):
    return next((p for p in unidade.pessoas.all() if p.is_responsavel), None)


def _validar_ids_pessoas_unidade(unidade, pessoas_data):
    ids_validos = {pessoa.id for pessoa in unidade.pessoas.all()}
    for pessoa in pessoas_data:
        pessoa_id = pessoa.get("id")
        if pessoa_id is not None and pessoa_id not in ids_validos:
            raise ValueError("Morador inválido informado no formulário.")


def _encontrar_par_pessoa_morador(pessoa_atual, candidatos):
    cpf_atual = _somente_digitos(pessoa_atual.cpf)
    if cpf_atual:
        for candidato in candidatos:
            if _somente_digitos(candidato.get("cpf", "")) == cpf_atual:
                return candidato

    nome_atual = _normalizar_texto_comparacao(pessoa_atual.nome_completo)
    nascimento_atual = pessoa_atual.data_nascimento
    for candidato in candidatos:
        if (
            _normalizar_texto_comparacao(candidato["nome_completo"]) == nome_atual
            and candidato.get("data_nascimento") == nascimento_atual
        ):
            return candidato
    return None


def _houve_add_remove_pessoas(unidade, pessoas_data):
    pessoas_atuais = unidade.pessoas.all()
    ids_informados = {p["id"] for p in pessoas_data if p.get("id") is not None}

    if ids_informados:
        ids_atuais = {pessoa.id for pessoa in pessoas_atuais}
        if ids_informados != ids_atuais:
            return True
        if any(p.get("id") is None for p in pessoas_data):
            return True
        return False

    if len(pessoas_atuais) != len(pessoas_data):
        return True

    candidatos = list(pessoas_data)
    for pessoa_atual in pessoas_atuais:
        par = _encontrar_par_pessoa_morador(pessoa_atual, candidatos)
        if not par:
            return True
        candidatos.remove(par)
    return False


def _houve_add_remove_veiculos(unidade, veiculos_data):
    placas_atuais = {_normalizar_placa(veiculo.placa) for veiculo in unidade.veiculos.all()}
    placas_novas = {_normalizar_placa(veiculo["placa"]) for veiculo in veiculos_data}
    return placas_atuais != placas_novas


def _houve_mudanca_proprietario_ou_responsavel(unidade, pessoas_data, dados_proprietario):
    responsavel_atual = _responsavel_pessoa_unidade(unidade)
    responsavel_novo = _responsavel_dados_pessoas(pessoas_data)

    era_locatario = _responsavel_e_locatario(
        [{"is_responsavel": True, "vinculo": responsavel_atual.vinculo}]
        if responsavel_atual
        else []
    )
    sera_locatario = _responsavel_e_locatario(pessoas_data)

    if era_locatario != sera_locatario:
        return True

    if responsavel_atual and responsavel_novo:
        if responsavel_novo.get("id") != responsavel_atual.id:
            return True
        if responsavel_novo["vinculo"] != responsavel_atual.vinculo:
            return True
    elif bool(responsavel_atual) != bool(responsavel_novo):
        return True

    if sera_locatario:
        nome_atual = _normalizar_texto_comparacao(unidade.proprietario_nome)
        nome_novo = _normalizar_texto_comparacao(dados_proprietario.get("proprietario_nome"))
        if nome_atual != nome_novo:
            return True

    return False


def _requer_nova_aprovacao_sindico(unidade, pessoas_data, veiculos_data, dados_proprietario):
    if _houve_add_remove_pessoas(unidade, pessoas_data):
        return True
    if _houve_add_remove_veiculos(unidade, veiculos_data):
        return True
    if _houve_mudanca_proprietario_ou_responsavel(
        unidade, pessoas_data, dados_proprietario
    ):
        return True
    return False


def acesso_reservas_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if get_current_user() or get_unidade_logada():
            return view(*args, **kwargs)
        flash("Faça login para acessar o módulo de reservas.", "warning")
        return redirect(url_for("index"))

    return wrapped


DIAS_FUNCIONAMENTO_VALIDOS = ("seg", "ter", "qua", "qui", "sex", "sab", "dom")


def gestao_espacos_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        usuario = get_current_user()
        if usuario and usuario.role in (Role.ADMIN, Role.ASSISTENTE, Role.SINDICO):
            return view(*args, **kwargs)
        flash("Acesso restrito para gestão de espaços.", "danger")
        return redirect(url_for("reservas"))

    return wrapped


def parceiro_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("parceiro_id"):
            return view(*args, **kwargs)
        flash("Faça login para acessar o Portal do Parceiro.", "warning")
        return redirect(url_for("parceiro_login"))

    return wrapped


def _usuario_pode_gerenciar_espaco(usuario, espaco):
    if not usuario:
        return False
    if usuario.role == Role.SINDICO:
        return espaco.bloco_vinculado == usuario.bloco_responsavel
    if usuario.role in (Role.ADMIN, Role.ASSISTENTE):
        return espaco.gerenciado_por == "admin"
    return False


def _reservas_pendentes_por_jurisdicao(usuario):
    if not usuario:
        return []
    query = Reserva.query.join(Reserva.espaco).filter(Reserva.status == "Pendente")
    if usuario.role == Role.SINDICO:
        query = query.filter(EspacoComum.bloco_vinculado == usuario.bloco_responsavel)
    elif usuario.role in (Role.ADMIN, Role.ASSISTENTE):
        query = query.filter(EspacoComum.gerenciado_por == "admin")
    else:
        return []
    return query.order_by(Reserva.data_solicitacao.desc()).all()


def _salvar_pessoas_veiculos(unidade, pessoas_data, veiculos_data):
    try:
        for pessoa in unidade.pessoas.all():
            db.session.delete(pessoa)
        for veiculo in unidade.veiculos.all():
            db.session.delete(veiculo)

        for dados in pessoas_data:
            campos_pessoa = {k: v for k, v in dados.items() if k != "id"}
            db.session.add(Pessoa(unidade_id=unidade.id, **campos_pessoa))

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


def esqueci_senha():
    if request.method == "POST":
        email_solicitado = request.form.get("email", "").strip().lower()
        mensagem_generica = (
            "Se o e-mail estiver cadastrado, enviaremos instruções para redefinição de senha."
        )

        unidade, email_destino = _buscar_unidade_e_email_login(email_solicitado)
        if unidade and email_destino:
            try:
                token = gerar_token_redefinicao(email_solicitado, SALT_RECUPERACAO_MORADOR)
                link = url_for("redefinir_senha", token=token, _external=True)
                enviar_email_redefinicao_senha(email_destino, link, perfil="morador")
            except Exception:
                traceback.print_exc()
                flash(
                    "Não foi possível enviar o e-mail. Tente novamente mais tarde.",
                    "danger",
                )
                return redirect(url_for("esqueci_senha"))

        flash(mensagem_generica, "info")
        return redirect(url_for("index"))

    return render_template("esqueci_senha.html")


def redefinir_senha(token):
    email = verificar_token_redefinicao(token, SALT_RECUPERACAO_MORADOR)
    if not email:
        flash("Link inválido ou expirado. Solicite uma nova redefinição de senha.", "danger")
        return redirect(url_for("esqueci_senha"))

    unidade, _ = _buscar_unidade_e_email_login(email)
    if not unidade:
        flash("Unidade não encontrada para este e-mail.", "danger")
        return redirect(url_for("esqueci_senha"))

    if request.method == "POST":
        senha = request.form.get("senha", "").strip()
        confirmacao = request.form.get("confirmacao_senha", "").strip()

        if len(senha) < 6:
            flash("A senha deve ter ao menos 6 caracteres.", "danger")
            return render_template("redefinir_senha.html", token=token)
        if senha != confirmacao:
            flash("As senhas não coincidem.", "danger")
            return render_template("redefinir_senha.html", token=token)

        unidade.set_password(senha)
        db.session.commit()
        flash(
            "Senha redefinida com sucesso. Acesse com bloco, apartamento e a nova senha.",
            "success",
        )
        return redirect(url_for("index"))

    return render_template("redefinir_senha.html", token=token)


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


def _contagem_resgates_por_cupom(cupom_ids):
    if not cupom_ids:
        return {}
    rows = (
        db.session.query(ResgateCupom.cupom_id, func.count(ResgateCupom.id))
        .filter(ResgateCupom.cupom_id.in_(cupom_ids))
        .group_by(ResgateCupom.cupom_id)
        .all()
    )
    return {cupom_id: total for cupom_id, total in rows}


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


@unidade_required
def clube_vantagens(unidade):
    data_atual = datetime.utcnow().date()
    cupons_ativos = (
        Cupom.query.join(Parceiro)
        .filter(
            Parceiro.status == "Ativo",
            Cupom.ativo.is_(True),
            or_(Cupom.data_validade.is_(None), Cupom.data_validade >= data_atual),
        )
        .order_by(Parceiro.nome_empresa, Cupom.titulo)
        .all()
    )
    cupom_ids = [cupom.id for cupom in cupons_ativos]
    resgates_por_cupom = _contagem_resgates_por_cupom(cupom_ids)

    resgates_unidade_rows = (
        db.session.query(ResgateCupom.cupom_id, func.count(ResgateCupom.id))
        .filter(ResgateCupom.unidade_id == unidade.id)
        .group_by(ResgateCupom.cupom_id)
        .all()
    )
    resgates_unidade_por_cupom = {
        cupom_id: total for cupom_id, total in resgates_unidade_rows
    }

    cupons_disponiveis = []
    for cupom in cupons_ativos:
        total_resgates = resgates_por_cupom.get(cupom.id, 0)
        if cupom.limite_total is not None and total_resgates >= cupom.limite_total:
            continue
        resgates_unidade = resgates_unidade_por_cupom.get(cupom.id, 0)
        if resgates_unidade >= cupom.limite_por_unidade:
            continue
        cupons_disponiveis.append(cupom)

    resgates_ativos = (
        ResgateCupom.query.join(Cupom)
        .join(Parceiro)
        .filter(ResgateCupom.unidade_id == unidade.id, ResgateCupom.status == "Ativo")
        .order_by(ResgateCupom.data_resgate.desc())
        .all()
    )
    resgates_utilizados = (
        ResgateCupom.query.join(Cupom)
        .join(Parceiro)
        .filter(ResgateCupom.unidade_id == unidade.id, ResgateCupom.status == "Utilizado")
        .order_by(ResgateCupom.data_utilizacao.desc())
        .all()
    )

    parceiros_ativos = (
        Parceiro.query.filter_by(status="Ativo")
        .order_by(Parceiro.nome_empresa)
        .all()
    )
    parceiros_com_cupons_ativos = {
        parceiro_id
        for (parceiro_id,) in db.session.query(Cupom.parceiro_id)
        .join(Parceiro)
        .filter(
            Parceiro.status == "Ativo",
            Cupom.ativo.is_(True),
            or_(Cupom.data_validade.is_(None), Cupom.data_validade >= data_atual),
        )
        .distinct()
        .all()
    }

    return render_template(
        "clube_vantagens.html",
        cupons_disponiveis=cupons_disponiveis,
        resgates_ativos=resgates_ativos,
        resgates_utilizados=resgates_utilizados,
        parceiros_ativos=parceiros_ativos,
        parceiros_com_cupons_ativos=parceiros_com_cupons_ativos,
    )


@unidade_required
def clube_vantagens_resgatar(unidade, cupom_id):
    cupom = Cupom.query.get_or_404(cupom_id)

    if not cupom.ativo or not cupom.parceiro.ativo:
        flash("Este cupom não está disponível no momento.", "warning")
        return redirect(url_for("clube_vantagens"))

    if cupom.data_validade and cupom.data_validade < datetime.utcnow().date():
        flash("Este cupom expirou.", "warning")
        return redirect(url_for("clube_vantagens"))

    total_resgates = ResgateCupom.query.filter_by(cupom_id=cupom.id).count()
    if cupom.limite_total is not None and total_resgates >= cupom.limite_total:
        flash("Oferta esgotada.", "danger")
        return redirect(url_for("clube_vantagens"))

    resgates_unidade = ResgateCupom.query.filter_by(
        cupom_id=cupom.id,
        unidade_id=unidade.id,
    ).count()
    if resgates_unidade >= cupom.limite_por_unidade:
        flash("Você atingiu o limite de resgates para esta oferta.", "danger")
        return redirect(url_for("clube_vantagens"))

    bloco = "".join(ch for ch in str(unidade.bloco or "") if ch.isalnum()).upper()
    apartamento = "".join(ch for ch in str(unidade.apartamento or "") if ch.isalnum()).upper()
    prefixo = "".join(ch for ch in (cupom.codigo_prefixo or "") if ch.isalnum()).upper()
    sufixo_chars = string.ascii_uppercase + string.digits

    codigo_unico = None
    for _ in range(20):
        sufixo = "".join(random.choices(sufixo_chars, k=4))
        candidato = f"PRP-{bloco}{apartamento}-{prefixo}-{sufixo}"
        if not ResgateCupom.query.filter_by(codigo_unico=candidato).first():
            codigo_unico = candidato
            break
    if not codigo_unico:
        flash("Não foi possível gerar um código único. Tente novamente.", "danger")
        return redirect(url_for("clube_vantagens"))

    db.session.add(
        ResgateCupom(
            cupom_id=cupom.id,
            unidade_id=unidade.id,
            codigo_unico=codigo_unico,
            status="Ativo",
        )
    )
    db.session.commit()
    flash(f"Cupom resgatado com sucesso! Código: {codigo_unico}", "success")
    return redirect(url_for("clube_vantagens"))


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
            session["parceiro_id"] = parceiro.id
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
    email = verificar_token_redefinicao(token, SALT_RECUPERACAO_PARCEIRO)
    if not email:
        flash("Link inválido ou expirado. Solicite uma nova redefinição de senha.", "danger")
        return redirect(url_for("parceiro_esqueci_senha"))

    parceiro = Parceiro.query.filter_by(email=email).first()
    if not parceiro or parceiro.status != "Ativo":
        flash("Parceiro não encontrado para este e-mail.", "danger")
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

        parceiro.senha_hash = generate_password_hash(senha)
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
    descricao = request.form.get("descricao", "").strip()
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
    descricao = request.form.get("descricao", "").strip() or None

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

    parceiro.nome_empresa = nome_empresa
    parceiro.email = email
    parceiro.telefone = telefone
    parceiro.categoria = categoria
    parceiro.endereco = endereco
    parceiro.descricao = descricao
    db.session.commit()
    flash("Perfil comercial atualizado com sucesso.", "success")
    return redirect(url_for("parceiro_perfil"))


@acesso_reservas_required
def reservas():
    usuario = get_current_user()
    unidade = get_unidade_logada()
    espacos = []
    unidades_gestao = []
    reservas_pendentes = []
    reservas_historico = []
    espacos_disponiveis = []
    minhas_reservas = []

    if usuario:
        if usuario.role == Role.SINDICO:
            espacos = (
                EspacoComum.query.filter_by(bloco_vinculado=usuario.bloco_responsavel)
                .order_by(EspacoComum.nome)
                .all()
            )
        elif usuario.role in (Role.ADMIN, Role.ASSISTENTE):
            espacos = (
                EspacoComum.query.filter_by(gerenciado_por="admin")
                .order_by(EspacoComum.nome)
                .all()
            )
            unidades_gestao = Unidade.query.order_by(Unidade.bloco, Unidade.apartamento).all()
        query_pendentes = Reserva.query.join(EspacoComum).filter(Reserva.status == "Pendente")
        query_historico = Reserva.query.join(EspacoComum).filter(
            Reserva.status != "Pendente"
        )

        if usuario.role == Role.SINDICO:
            filtro_jurisdicao = EspacoComum.bloco_vinculado == usuario.bloco_responsavel
            unidades_gestao = [
                unidade
                for unidade in Unidade.query.order_by(Unidade.bloco, Unidade.apartamento).all()
                if blocos_equivalentes(unidade.bloco, usuario.bloco_responsavel)
            ]
        else:
            filtro_jurisdicao = EspacoComum.gerenciado_por == "admin"

        reservas_pendentes = (
            query_pendentes.filter(filtro_jurisdicao)
            .order_by(Reserva.data_solicitacao.desc())
            .all()
        )
        reservas_historico = (
            query_historico.filter(filtro_jurisdicao)
            .order_by(Reserva.data_reserva.desc())
            .all()
        )

    if unidade:
        espacos_disponiveis = (
            EspacoComum.query.filter(
                or_(
                    EspacoComum.apenas_moradores_bloco.is_(False),
                    EspacoComum.bloco_vinculado == unidade.bloco,
                )
            )
            .order_by(EspacoComum.nome)
            .all()
        )

        minhas_reservas = (
            Reserva.query.filter_by(unidade_id=unidade.id)
            .order_by(Reserva.data_reserva.desc())
            .all()
        )

    return render_template(
        "reservas.html",
        current_user=usuario,
        current_unidade=unidade,
        espacos=espacos,
        unidades_gestao=unidades_gestao,
        reservas_pendentes=reservas_pendentes,
        reservas_historico=reservas_historico,
        espacos_disponiveis=espacos_disponiveis,
        minhas_reservas=minhas_reservas,
    )


@unidade_required
def solicitar_reserva(unidade):
    espaco_id = request.form.get("espaco_id", "").strip()
    data_reserva_str = request.form.get("data_reserva", "").strip()

    if not espaco_id or not data_reserva_str:
        flash("Informe o espaço e a data desejada para reserva.", "danger")
        return redirect(url_for("reservas"))

    try:
        espaco = EspacoComum.query.get_or_404(int(espaco_id))
        data_reserva = datetime.strptime(data_reserva_str, "%Y-%m-%d").date()
    except ValueError:
        flash("Data de reserva inválida.", "danger")
        return redirect(url_for("reservas"))

    if espaco.apenas_moradores_bloco and espaco.bloco_vinculado != unidade.bloco:
        flash("Este espaço aceita reservas apenas de moradores do bloco vinculado.", "danger")
        return redirect(url_for("reservas"))

    if Reserva.query.filter_by(espaco_id=espaco.id, data_reserva=data_reserva).filter(
        Reserva.status.in_(["Pendente", "Aprovada"])
    ).first():
        flash("Já existe uma reserva pendente/aprovada para este espaço nesta data.", "warning")
        return redirect(url_for("reservas"))

    reserva = Reserva(
        espaco_id=espaco.id,
        unidade_id=unidade.id,
        data_reserva=data_reserva,
        status="Pendente",
    )
    db.session.add(reserva)
    db.session.commit()

    email_sistema = os.environ.get("MAIL_USERNAME")
    if email_sistema:
        try:
            enviar_email_nova_reserva(
                email_destino=email_sistema,
                nome_espaco=espaco.nome,
                bloco=unidade.bloco,
                apartamento=unidade.apartamento,
                data_reserva=data_reserva.strftime("%d/%m/%Y"),
            )
        except Exception:
            traceback.print_exc()
            flash(
                "Reserva enviada, mas não foi possível notificar a administração por e-mail.",
                "warning",
            )

    flash("Solicitação de reserva enviada com sucesso.", "success")
    return redirect(url_for("reservas"))


@gestao_espacos_required
def criar_reserva_gestao():
    usuario = get_current_user()
    espaco_id = request.form.get("espaco_id", "").strip()
    data_reserva_str = request.form.get("data_reserva", "").strip()
    unidade_id = request.form.get("unidade_id", "").strip()
    motivo_reserva = request.form.get("motivo_reserva", "").strip() or None

    if not espaco_id or not data_reserva_str:
        flash("Informe o espaço e a data para criar a reserva.", "danger")
        return redirect(url_for("reservas"))

    try:
        espaco = EspacoComum.query.get_or_404(int(espaco_id))
        data_reserva = datetime.strptime(data_reserva_str, "%d/%m/%Y").date()
    except ValueError:
        flash("Dados inválidos para criação da reserva.", "danger")
        return redirect(url_for("reservas"))

    if not _usuario_pode_gerenciar_espaco(usuario, espaco):
        flash("Você não tem permissão para criar reserva neste espaço.", "danger")
        return redirect(url_for("reservas"))

    conflito = Reserva.query.filter_by(espaco_id=espaco.id, data_reserva=data_reserva).filter(
        Reserva.status.in_(["Pendente", "Aprovada"])
    ).first()
    if conflito:
        flash("Já existe uma reserva pendente/aprovada para este espaço nesta data.", "warning")
        return redirect(url_for("reservas"))

    unidade = None
    if unidade_id:
        try:
            unidade = Unidade.query.get_or_404(int(unidade_id))
        except ValueError:
            flash("Unidade inválida para vinculação da reserva.", "danger")
            return redirect(url_for("reservas"))

        if usuario.role == Role.SINDICO and not blocos_equivalentes(
            unidade.bloco, usuario.bloco_responsavel
        ):
            flash("Você só pode vincular reservas a unidades do seu bloco.", "danger")
            return redirect(url_for("reservas"))

    reserva = Reserva(
        espaco_id=espaco.id,
        unidade_id=unidade.id if unidade else None,
        data_reserva=data_reserva,
        status="Aprovada",
        valor_pago=0.0 if unidade else espaco.valor_reserva,
        motivo_reserva=motivo_reserva,
    )
    db.session.add(reserva)
    db.session.commit()

    flash("Reserva criada com sucesso.", "success")
    return redirect(url_for("reservas"))


@gestao_espacos_required
def responder_reserva(reserva_id):
    usuario = get_current_user()
    reserva = Reserva.query.get_or_404(reserva_id)
    acao = request.form.get("acao", "").strip().lower()

    if not _usuario_pode_gerenciar_espaco(usuario, reserva.espaco):
        flash("Você não tem permissão para responder esta reserva.", "danger")
        return redirect(url_for("reservas"))

    if reserva.status != "Pendente":
        flash("Esta reserva já foi respondida.", "warning")
        return redirect(url_for("reservas"))

    if acao == "aprovar":
        reserva.status = "Aprovada"
    elif acao == "recusar":
        reserva.status = "Recusada"
    else:
        flash("Ação inválida para resposta da reserva.", "danger")
        return redirect(url_for("reservas"))

    db.session.commit()

    if reserva.unidade:
        emails_moradores = _emails_unicos(reserva.unidade.pessoas.all())
        for email in emails_moradores:
            try:
                enviar_email_resposta_reserva(
                    email_destino=email,
                    nome_espaco=reserva.espaco.nome,
                    data_reserva=reserva.data_reserva.strftime("%d/%m/%Y"),
                    status=reserva.status,
                )
            except Exception:
                traceback.print_exc()
                flash(
                    f"Reserva atualizada, mas houve falha ao notificar {email}.",
                    "warning",
                )

    flash(f"Reserva {reserva.status.lower()} com sucesso.", "success")
    return redirect(url_for("reservas"))


@gestao_espacos_required
def api_reservas_eventos():
    usuario = get_current_user()
    if usuario.role == Role.SINDICO:
        query = Reserva.query.join(EspacoComum).filter(
            EspacoComum.bloco_vinculado == usuario.bloco_responsavel,
            Reserva.status.in_(["Pendente", "Aprovada"]),
        )
    elif usuario.role in (Role.ADMIN, Role.ASSISTENTE):
        query = Reserva.query.join(EspacoComum).filter(
            or_(
                and_(
                    EspacoComum.gerenciado_por == "admin",
                    Reserva.status.in_(["Pendente", "Aprovada"]),
                ),
                and_(
                    EspacoComum.gerenciado_por == "sindico",
                    Reserva.status == "Aprovada",
                ),
            )
        )
    else:
        return jsonify([])

    reservas = query.order_by(Reserva.data_reserva.asc()).all()
    eventos = []
    for reserva in reservas:
        pode_gerenciar = _usuario_pode_gerenciar_espaco(usuario, reserva.espaco)
        if reserva.unidade:
            titulo_base = (
                f"{reserva.unidade.bloco} - {reserva.unidade.apartamento} "
                f"({reserva.espaco.nome})"
            )
            titulo = (
                f"{titulo_base} [Pago: R$ {reserva.valor_pago:.2f}]"
                if pode_gerenciar
                else titulo_base
            )
        else:
            motivo = reserva.motivo_reserva or "Evento interno"
            titulo = f"[CONDOMÍNIO] {reserva.espaco.nome} - {motivo}"
        eventos.append(
            {
                "title": titulo,
                "start": reserva.data_reserva.isoformat(),
                "color": "#198754" if reserva.status == "Aprovada" else "#ffc107",
            }
        )
    return jsonify(eventos)


@gestao_espacos_required
def atualizar_pagamento_reserva(reserva_id):
    usuario = get_current_user()
    reserva = Reserva.query.get_or_404(reserva_id)

    if not _usuario_pode_gerenciar_espaco(usuario, reserva.espaco):
        flash("Você não tem permissão para atualizar este pagamento.", "danger")
        return redirect(url_for("reservas"))

    valor_pago_raw = request.form.get("valor_pago", "").strip()
    try:
        valor_pago = round(float(valor_pago_raw), 2)
    except ValueError:
        flash("Valor pago inválido.", "danger")
        return redirect(url_for("reservas"))

    if valor_pago < 0:
        flash("O valor pago não pode ser negativo.", "danger")
        return redirect(url_for("reservas"))

    reserva.valor_pago = valor_pago
    if reserva.valor_pago >= reserva.espaco.valor_reserva:
        reserva.status = "Aprovada"

    db.session.commit()
    flash("Pagamento da reserva atualizado com sucesso.", "success")
    return redirect(url_for("reservas"))


@gestao_espacos_required
def cancelar_reserva(reserva_id):
    usuario = get_current_user()
    reserva = Reserva.query.get_or_404(reserva_id)

    if not _usuario_pode_gerenciar_espaco(usuario, reserva.espaco):
        flash("Você não tem permissão para cancelar esta reserva.", "danger")
        return redirect(url_for("reservas"))

    if reserva.status == "Cancelada":
        flash("Esta reserva já está cancelada.", "warning")
        return redirect(url_for("reservas"))

    reserva.status = "Cancelada"
    db.session.commit()

    if reserva.unidade:
        emails_moradores = _emails_unicos(reserva.unidade.pessoas.all())
        for email in emails_moradores:
            try:
                enviar_email_resposta_reserva(
                    email_destino=email,
                    nome_espaco=reserva.espaco.nome,
                    data_reserva=reserva.data_reserva.strftime("%d/%m/%Y"),
                    status="Cancelada",
                )
            except Exception:
                traceback.print_exc()
                flash(
                    f"Reserva cancelada, mas houve falha ao notificar {email}.",
                    "warning",
                )

    flash("Reserva cancelada com sucesso.", "success")
    return redirect(url_for("reservas"))


@gestao_espacos_required
def salvar_espaco_reserva():
    usuario = get_current_user()
    espaco_id = request.form.get("espaco_id", "").strip()
    nome = request.form.get("nome", "").strip()
    apenas_moradores_bloco = request.form.get("apenas_moradores_bloco") == "on"
    valor_reserva_raw = request.form.get("valor_reserva", "").strip()
    dias_selecionados = [
        dia
        for dia in request.form.getlist("dias_funcionamento")
        if dia in DIAS_FUNCIONAMENTO_VALIDOS
    ]

    if not nome:
        flash("Informe o nome do espaço.", "danger")
        return redirect(url_for("reservas"))
    if not dias_selecionados:
        flash("Selecione ao menos um dia de funcionamento.", "danger")
        return redirect(url_for("reservas"))

    try:
        valor_reserva = float(valor_reserva_raw or 0)
    except ValueError:
        flash("Valor de reserva inválido.", "danger")
        return redirect(url_for("reservas"))

    if valor_reserva < 0:
        flash("O valor da reserva não pode ser negativo.", "danger")
        return redirect(url_for("reservas"))

    if espaco_id:
        espaco = EspacoComum.query.get_or_404(int(espaco_id))
        if usuario.role == Role.SINDICO:
            if espaco.bloco_vinculado != usuario.bloco_responsavel:
                flash("Você não tem permissão para editar este espaço.", "danger")
                return redirect(url_for("reservas"))
        elif usuario.role in (Role.ADMIN, Role.ASSISTENTE):
            if espaco.gerenciado_por != "admin":
                flash("Você só pode editar espaços gerenciados pela administração.", "danger")
                return redirect(url_for("reservas"))
    else:
        espaco = EspacoComum(tipo="SALAO_FESTAS")
        db.session.add(espaco)

    espaco.nome = nome
    espaco.valor_reserva = valor_reserva
    espaco.dias_funcionamento = ",".join(dias_selecionados)

    if usuario.role == Role.SINDICO:
        espaco.gerenciado_por = "sindico"
        espaco.bloco_vinculado = usuario.bloco_responsavel
        espaco.apenas_moradores_bloco = apenas_moradores_bloco
    else:
        espaco.gerenciado_por = "admin"
        espaco.bloco_vinculado = None
        espaco.apenas_moradores_bloco = False

    db.session.commit()
    flash("Espaço salvo com sucesso.", "success")
    return redirect(url_for("reservas"))


def sair():
    logout_unidade()
    logout_usuario()
    flash("Sessão encerrada.", "info")
    return redirect(url_for("index"))


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

            _validar_ids_pessoas_unidade(unidade, pessoas_data)

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

        dados_proprietario = _parse_proprietario_form(request.form)
        requer_nova_aprovacao = False
        if modo_atualizacao:
            requer_nova_aprovacao = _requer_nova_aprovacao_sindico(
                unidade, pessoas_data, veiculos_data, dados_proprietario
            )

        _salvar_pessoas_veiculos(unidade, pessoas_data, veiculos_data)

        if _responsavel_e_locatario(pessoas_data):
            if not modo_atualizacao:
                unidade.contrato_locacao_status = StatusDocumento.PENDENTE
            elif unidade.contrato_locacao_status == StatusDocumento.NAO_APLICAVEL:
                unidade.contrato_locacao_status = StatusDocumento.PENDENTE

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
            unidade.data_alteracao = datetime.utcnow()
            if requer_nova_aprovacao:
                unidade.status = StatusUnidade.PENDENTE

        db.session.commit()

        if modo_atualizacao:
            if requer_nova_aprovacao:
                flash(
                    "Dados atualizados e cadastro reenviado para nova aprovação do síndico.",
                    "success",
                )
            else:
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


def _redirect_pos_login_admin(usuario):
    if usuario.role == Role.ADMIN:
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("admin_index"))


def admin_login():
    usuario_logado = get_current_user()
    if usuario_logado and usuario_logado.role in (Role.ADMIN, Role.ASSISTENTE):
        return _redirect_pos_login_admin(usuario_logado)

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        usuario = Usuario.query.filter(
            Usuario.username == username,
            Usuario.role.in_([Role.ADMIN, Role.ASSISTENTE]),
        ).first()
        if usuario and usuario.check_password(password):
            login_usuario(usuario)
            return _redirect_pos_login_admin(usuario)

        flash("Usuário ou senha inválidos.", "danger")

    return render_template("login.html", titulo="Login do Administrador", action="admin")


def admin_logout():
    logout_usuario()
    flash("Sessão encerrada.", "info")
    return redirect(url_for("admin_login"))


@admin_required
def admin_dashboard():
    usuario = get_current_user()
    inicio_janela = datetime.utcnow() - timedelta(days=30)

    total_aprovados = Unidade.query.filter_by(
        status=StatusUnidade.REGISTRADA
    ).count()

    aguardando_registro = Unidade.query.filter_by(
        status=StatusUnidade.APROVADA
    ).count()

    documentos_pendentes = Unidade.query.filter(
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
            Unidade.status.in_([StatusUnidade.APROVADA, StatusUnidade.REGISTRADA])
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
        .filter(Unidade.data_criacao >= inicio_janela)
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


@admin_required
def admin_clube_vantagens():
    usuario = get_current_user()
    parceiros_clube = Parceiro.query.order_by(Parceiro.data_cadastro.desc()).all()
    auditoria_cupons = (
        ResgateCupom.query.join(Cupom).join(Parceiro).join(Unidade)
        .order_by(ResgateCupom.data_resgate.desc())
        .all()
    )

    return render_template(
        "admin_clube_vantagens.html",
        parceiros_clube=parceiros_clube,
        auditoria_cupons=auditoria_cupons,
        current_user=usuario,
        active_tab="gestao",
    )


@admin_required
def admin_clube_vantagens_analytics():
    usuario = get_current_user()
    analytics = _montar_analytics_clube()

    return render_template(
        "admin_clube_vantagens.html",
        current_user=usuario,
        active_tab="analytics",
        analytics=analytics,
    )


@admin_or_assistente_required
def admin_parceiros_criar():
    nome_empresa = request.form.get("nome_empresa", "").strip()
    usuario_login = request.form.get("usuario_login", "").strip().lower()
    email = request.form.get("email", "").strip().lower()
    telefone = request.form.get("telefone", "").strip() or None
    categoria = request.form.get("categoria", "").strip()
    endereco = request.form.get("endereco", "").strip() or None
    descricao = request.form.get("descricao", "").strip() or None

    if not nome_empresa or not usuario_login or not email or not categoria:
        flash("Preencha nome da empresa, usuário de login, e-mail e categoria.", "danger")
        return redirect(url_for("admin_clube_vantagens"))

    if " " in usuario_login:
        flash("O usuário de login não pode conter espaços.", "danger")
        return redirect(url_for("admin_clube_vantagens"))

    if Parceiro.query.filter_by(usuario_login=usuario_login).first():
        flash("Já existe parceiro cadastrado com este usuário de login.", "warning")
        return redirect(url_for("admin_clube_vantagens"))

    if Parceiro.query.filter_by(email=email).first():
        flash("Já existe parceiro cadastrado com este e-mail.", "warning")
        return redirect(url_for("admin_clube_vantagens"))

    parceiro = Parceiro(
        nome_empresa=nome_empresa,
        usuario_login=usuario_login,
        email=email,
        senha_hash=generate_password_hash("senha123"),
        telefone=telefone,
        categoria=categoria,
        endereco=endereco,
        descricao=descricao,
        ativo=True,
        status="Pendente",
    )
    db.session.add(parceiro)
    db.session.commit()
    flash("Parceiro cadastrado com sucesso. Status inicial: Pendente.", "success")
    return redirect(url_for("admin_clube_vantagens"))


@admin_required
def admin_parceiro_editar(parceiro_id):
    parceiro = Parceiro.query.get_or_404(parceiro_id)
    nome_empresa = request.form.get("nome_empresa", "").strip()
    usuario_login = request.form.get("usuario_login", "").strip().lower()
    email = request.form.get("email", "").strip().lower()
    telefone = request.form.get("telefone", "").strip() or None
    categoria = request.form.get("categoria", "").strip()
    endereco = request.form.get("endereco", "").strip() or None
    descricao = request.form.get("descricao", "").strip() or None

    if not nome_empresa or not usuario_login or not email or not categoria:
        flash("Preencha nome da empresa, usuário de login, e-mail e categoria.", "danger")
        return redirect(url_for("admin_clube_vantagens"))

    if " " in usuario_login:
        flash("O usuário de login não pode conter espaços.", "danger")
        return redirect(url_for("admin_clube_vantagens"))

    parceiro_login_existente = Parceiro.query.filter(
        Parceiro.usuario_login == usuario_login,
        Parceiro.id != parceiro.id,
    ).first()
    if parceiro_login_existente:
        flash("Já existe outro parceiro cadastrado com este usuário de login.", "warning")
        return redirect(url_for("admin_clube_vantagens"))

    parceiro_existente = Parceiro.query.filter(
        Parceiro.email == email,
        Parceiro.id != parceiro.id,
    ).first()
    if parceiro_existente:
        flash("Já existe outro parceiro cadastrado com este e-mail.", "warning")
        return redirect(url_for("admin_clube_vantagens"))

    parceiro.nome_empresa = nome_empresa
    parceiro.usuario_login = usuario_login
    parceiro.email = email
    parceiro.telefone = telefone
    parceiro.categoria = categoria
    parceiro.endereco = endereco
    parceiro.descricao = descricao
    db.session.commit()
    flash("Parceiro atualizado com sucesso.", "success")
    return redirect(url_for("admin_clube_vantagens"))


@admin_required
def admin_parceiro_bloquear(parceiro_id):
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
        f"Parceiro bloqueado: {parceiro.nome_empresa} ({parceiro.email}).",
    )
    db.session.commit()
    flash(
        "Parceiro bloqueado. Os cupons deste parceiro foram removidos da vitrine.",
        "warning",
    )
    return redirect(url_for("admin_clube_vantagens"))


@admin_required
def admin_parceiro_ativar(parceiro_id):
    parceiro = Parceiro.query.get_or_404(parceiro_id)
    usuario = get_current_user()

    parceiro.status = "Ativo"
    parceiro.ativo = True
    _registrar_auditoria(
        usuario,
        f"Parceiro reativado: {parceiro.nome_empresa} ({parceiro.email}).",
    )
    db.session.commit()
    flash("Parceiro reativado com sucesso.", "success")
    return redirect(url_for("admin_clube_vantagens"))


@admin_or_assistente_required
def admin_registrar(unidade_id):
    unidade = Unidade.query.get_or_404(unidade_id)

    if unidade.status != StatusUnidade.APROVADA:
        flash("Apenas unidades aprovadas podem ser registradas.", "warning")
        return redirect(url_for("admin_index"))

    unidade.status = StatusUnidade.REGISTRADA
    db.session.commit()
    flash(f"Unidade {unidade.identificador} marcada como registrada.", "success")
    return redirect(url_for("admin_index"))


@admin_or_assistente_required
def admin_unidade_alterar_senha(unidade_id):
    unidade = Unidade.query.get_or_404(unidade_id)
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
    usuario = get_current_user()
    if usuario.role != Role.ADMIN:
        flash("Acesso negado.", "danger")
        return redirect(url_for("admin_index"))

    unidade = Unidade.query.get_or_404(unidade_id)

    db.session.delete(unidade)
    db.session.commit()

    flash(
        "Cadastro da unidade apagado com sucesso. Ela está livre para novo registro.",
        "success",
    )
    return redirect(url_for("admin_index"))


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
    return redirect(url_for("admin_index"))


@admin_required
def admin_validar_contrato_locacao(unidade_id):
    unidade = Unidade.query.get_or_404(unidade_id)

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
    return redirect(url_for("admin_index"))


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
    return redirect(url_for("admin_index"))


@admin_required
def admin_alterar_senha_sindico():
    username = request.form.get("username", "").strip()
    nova_senha = request.form.get("nova_senha", "").strip()

    if not username or not nova_senha:
        flash("Informe o síndico e a nova senha.", "danger")
        return redirect(url_for("admin_index"))

    if len(nova_senha) < 6:
        flash("A nova senha deve ter ao menos 6 caracteres.", "danger")
        return redirect(url_for("admin_index"))

    sindico = Usuario.query.filter_by(username=username, role="sindico").first()
    if not sindico:
        flash("Síndico não encontrado.", "danger")
        return redirect(url_for("admin_index"))

    sindico.set_password(nova_senha)
    db.session.commit()
    flash(
        f"Senha do síndico do {sindico.bloco_responsavel} atualizada com sucesso.",
        "success",
    )
    return redirect(url_for("admin_index"))


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
    return redirect(url_for("admin_index"))


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
        return redirect(url_for("admin_index"))

    return render_template("criar_usuario.html", blocos=blocos)


@admin_required
def admin_excluir_usuario(usuario_id):
    usuario_logado = get_current_user()
    usuario_alvo = Usuario.query.get_or_404(usuario_id)

    if usuario_alvo.id == usuario_logado.id:
        flash("Você não pode excluir o próprio acesso.", "danger")
        return redirect(url_for("admin_index"))

    if usuario_alvo.role not in (Role.ASSISTENTE, Role.SINDICO):
        flash("Apenas acessos de assistente ou síndico podem ser revogados aqui.", "warning")
        return redirect(url_for("admin_index"))

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
    return redirect(url_for("admin_index"))


def init_app(app):
    app.add_url_rule("/", "index", index, methods=["GET"])
    app.add_url_rule(
        "/verificar-unidade", "verificar_unidade", verificar_unidade, methods=["POST"]
    )
    app.add_url_rule(
        "/esqueci_senha", "esqueci_senha", esqueci_senha, methods=["GET", "POST"]
    )
    app.add_url_rule(
        "/redefinir_senha/<token>",
        "redefinir_senha",
        redefinir_senha,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/cadastro-inicial", "cadastro_inicial", cadastro_inicial, methods=["GET"]
    )
    app.add_url_rule(
        "/atualizar-dados", "atualizar_dados", atualizar_dados, methods=["GET"]
    )
    app.add_url_rule(
        "/parceiro",
        "parceiro_login",
        parceiro_login,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/parceiro/login",
        "parceiro_login_alt",
        parceiro_login,
        methods=["GET", "POST"],
    )
    app.add_url_rule("/parceiro/logout", "parceiro_logout", parceiro_logout, methods=["GET"])
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
    app.add_url_rule(
        "/clube_vantagens",
        "clube_vantagens",
        clube_vantagens,
        methods=["GET"],
    )
    app.add_url_rule(
        "/clube_vantagens/resgatar/<int:cupom_id>",
        "clube_vantagens_resgatar",
        clube_vantagens_resgatar,
        methods=["POST"],
    )
    app.add_url_rule("/reservas", "reservas", reservas, methods=["GET"])
    app.add_url_rule(
        "/reservas/solicitar",
        "solicitar_reserva",
        solicitar_reserva,
        methods=["POST"],
    )
    app.add_url_rule(
        "/reservas/gestao/criar",
        "criar_reserva_gestao",
        criar_reserva_gestao,
        methods=["POST"],
    )
    app.add_url_rule(
        "/reservas/<int:reserva_id>/responder",
        "responder_reserva",
        responder_reserva,
        methods=["POST"],
    )
    app.add_url_rule(
        "/api/reservas/eventos",
        "api_reservas_eventos",
        api_reservas_eventos,
        methods=["GET"],
    )
    app.add_url_rule(
        "/reservas/<int:reserva_id>/atualizar_pagamento",
        "atualizar_pagamento_reserva",
        atualizar_pagamento_reserva,
        methods=["POST"],
    )
    app.add_url_rule(
        "/reservas/<int:reserva_id>/cancelar",
        "cancelar_reserva",
        cancelar_reserva,
        methods=["POST"],
    )
    app.add_url_rule(
        "/reservas/espacos/salvar",
        "salvar_espaco_reserva",
        salvar_espaco_reserva,
        methods=["POST"],
    )
    app.add_url_rule("/sair", "sair", sair, methods=["GET"])
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
        "/admin/parceiros/criar",
        "admin_parceiros_criar",
        admin_parceiros_criar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/parceiros/<int:parceiro_id>/editar",
        "admin_parceiro_editar",
        admin_parceiro_editar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/parceiros/<int:parceiro_id>/bloquear",
        "admin_parceiro_bloquear",
        admin_parceiro_bloquear,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/parceiros/<int:parceiro_id>/ativar",
        "admin_parceiro_ativar",
        admin_parceiro_ativar,
        methods=["POST"],
    )
