from datetime import date, datetime, timedelta
from functools import wraps
from html import escape
from html.parser import HTMLParser
import os
import random
import re
import string
import traceback

from flask import (
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import and_, func, or_, text
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

from app import db
from app.auth import (
    condominio_esta_ativo,
    condominio_id_obrigatorio,
    get_current_user,
    get_unidade_logada,
    login_unidade,
    login_usuario,
    logout_unidade,
    logout_usuario,
    normalizar_slug,
    obter_condominio_por_slug,
    resolver_condominio_id,
    unidade_required,
    _redirect_login_tenant,
)
from app.email_service import (
    enviar_email_nova_reserva,
    enviar_email_redefinicao_senha,
    enviar_email_resposta_reserva,
)
from app.models import (
    AgendamentoMudanca,
    AutorizacaoAcesso,
    CategoriaOcorrencia,
    Condominio,
    Cupom,
    EspacoComum,
    LogAuditoria,
    Notificacao,
    Ocorrencia,
    Parceiro,
    PerfilDestinoNotificacao,
    Pessoa,
    Reserva,
    ResgateCupom,
    Role,
    StatusAgendamentoMudanca,
    StatusAutorizacaoAcesso,
    StatusDocumento,
    StatusOcorrencia,
    StatusUnidade,
    TipoVisitante,
    Unidade,
    Usuario,
    Veiculo,
    VinculoPessoa,
)
from app.utils import (
    PARCEIRO_LOGO_EXTENSOES,
    PARCEIRO_LOGO_MAX_BYTES,
    SALT_RECUPERACAO_MORADOR,
    gerar_token_redefinicao,
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


def _slug_sessao_ou_prp():
    return session.get("tenant_slug") or session.get("cadastro_slug") or "prp"


def _slug_logout():
    """Slug do tenant atual, capturado antes de limpar a sessão."""
    usuario = get_current_user()
    condominio = getattr(usuario, "condominio", None) if usuario else None
    if condominio and condominio.slug:
        return condominio.slug
    return _slug_sessao_ou_prp()


def _salvar_imagem_upload(arquivo, pasta, prefixo="foto"):
    """
    Blindagem de imagem (mesma regra do Clube de Vantagens / Ocorrências):
    PNG/JPG/JPEG/WEBP, máximo 2 MB. Salva em `pasta`.
    Retorna (filename, erro).
    """
    if not arquivo or not arquivo.filename:
        return None, None

    nome_seguro = secure_filename(arquivo.filename)
    if not nome_seguro or "." not in nome_seguro:
        return None, "Envie uma foto em PNG, JPG, JPEG ou WEBP."

    extensao = nome_seguro.rsplit(".", 1)[-1].lower()
    if extensao not in PARCEIRO_LOGO_EXTENSOES:
        return None, "Envie uma foto em PNG, JPG, JPEG ou WEBP."

    tamanho_header = getattr(arquivo, "content_length", None)
    if tamanho_header and tamanho_header > PARCEIRO_LOGO_MAX_BYTES:
        return None, "A foto deve ter no máximo 2 MB."

    arquivo.stream.seek(0, os.SEEK_END)
    tamanho = arquivo.stream.tell()
    arquivo.stream.seek(0)
    if tamanho > PARCEIRO_LOGO_MAX_BYTES:
        return None, "A foto deve ter no máximo 2 MB."

    os.makedirs(pasta, exist_ok=True)
    token = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    nome_final = f"{secure_filename(prefixo)}_{token}.{extensao}"
    arquivo.save(os.path.join(pasta, nome_final))
    return nome_final, None


def _salvar_foto_ocorrencia(arquivo, prefixo="ocorrencia"):
    """Salva foto do chamado em static/uploads/ocorrencias/."""
    pasta = current_app.config.get("UPLOAD_OCORRENCIAS_FOLDER") or os.path.join(
        current_app.root_path, "static", "uploads", "ocorrencias"
    )
    return _salvar_imagem_upload(arquivo, pasta, prefixo=prefixo)


def _buscar_unidade(bloco, apartamento, condominio_id=None):
    """Busca unidade apenas dentro do tenant. Sem condominio_id, não consulta."""
    if not condominio_id:
        return None
    return Unidade.query.filter_by(
        bloco=bloco,
        apartamento=apartamento,
        condominio_id=condominio_id,
    ).first()


def _unidade_do_tenant(unidade_id, condominio_id):
    """Carrega unidade garantindo isolamento multi-tenant (anti-IDOR)."""
    return Unidade.query.filter_by(
        id=unidade_id, condominio_id=condominio_id
    ).first_or_404()


def _usuario_do_tenant(usuario_id, condominio_id):
    """Carrega usuário local do mesmo condomínio (anti-IDOR)."""
    return Usuario.query.filter_by(
        id=usuario_id, condominio_id=condominio_id
    ).first_or_404()


def _agendamento_do_tenant(agendamento_id, condominio_id):
    """Carrega agendamento de mudança do mesmo condomínio (anti-IDOR)."""
    return AgendamentoMudanca.query.filter_by(
        id=agendamento_id, condominio_id=condominio_id
    ).first_or_404()


def _ocorrencia_do_tenant(ocorrencia_id, condominio_id):
    """Carrega ocorrência do mesmo condomínio (anti-IDOR)."""
    return Ocorrencia.query.filter_by(
        id=ocorrencia_id, condominio_id=condominio_id
    ).first()


def _condominio_id_portaria(usuario=None):
    """Tenant da sessão de portaria; Super Admin só opera com condomínio na sessão."""
    usuario = usuario or get_current_user()
    condominio_id = condominio_id_obrigatorio(usuario)
    if condominio_id:
        return condominio_id
    return session.get("condominio_id")


def _espaco_do_tenant(espaco_id, condominio_id):
    """Carrega área comum garantindo isolamento multi-tenant (anti-IDOR)."""
    return EspacoComum.query.filter_by(
        id=espaco_id, condominio_id=condominio_id
    ).first_or_404()


def _reserva_do_tenant(reserva_id, condominio_id):
    """Carrega reserva cujo espaço pertence ao condomínio logado (anti-IDOR)."""
    return (
        Reserva.query.join(EspacoComum)
        .filter(
            Reserva.id == reserva_id,
            EspacoComum.condominio_id == condominio_id,
        )
        .first_or_404()
    )


def _pessoa_do_tenant(pessoa_id, condominio_id):
    """Carrega morador (Pessoa) apenas se a unidade for do tenant (anti-IDOR)."""
    return (
        Pessoa.query.join(Unidade)
        .filter(Pessoa.id == pessoa_id, Unidade.condominio_id == condominio_id)
        .first_or_404()
    )


def _condominio_id_da_sessao():
    """Tenant da sessão atual — sem fallback para o PRP (evita vazamento LGPD)."""
    cid = session.get("condominio_id") or session.get("cadastro_condominio_id")
    if cid:
        return cid
    slug = session.get("tenant_slug") or session.get("cadastro_slug")
    if not slug:
        return None
    condominio = Condominio.query.filter_by(slug=normalizar_slug(slug)).first()
    return condominio.id if condominio else None


def _buscar_unidade_e_email_login(email, condominio_id=None):
    """Localiza unidade e o e-mail cadastrado, estritamente no condomínio informado."""
    email_normalizado = email.strip().lower()
    if not email_normalizado or not condominio_id:
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
            Unidade.condominio_id == condominio_id,
            or_(
                func.lower(Unidade.proprietario_email) == email_normalizado,
                func.lower(Pessoa.email) == email_normalizado,
            ),
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


def _agrupamentos_sindico(usuario):
    """Lista os nomes de agrupamento (blocos) sob jurisdição do síndico."""
    if not usuario:
        return []
    query = usuario.agrupamentos
    if usuario.condominio_id:
        query = query.filter_by(condominio_id=usuario.condominio_id)
    return [agrup.nome_agrupamento for agrup in query]


def _blocos_codigo_sindico(usuario):
    """Códigos normalizados dos agrupamentos do síndico (ex.: '1', '6')."""
    return [
        normalizar_bloco_codigo(nome) for nome in _agrupamentos_sindico(usuario)
    ]


def _chaves_agrupamento_sindico(usuario):
    """Valores possíveis para filtros SQL em EspacoComum.bloco_vinculado."""
    chaves = set()
    for nome in _agrupamentos_sindico(usuario):
        chaves.add(nome)
        codigo = normalizar_bloco_codigo(nome)
        chaves.add(codigo)
        chaves.add(f"Bloco {codigo}")
    return list(chaves)


def _sindico_gerencia_bloco(usuario, bloco):
    if not bloco:
        return False
    bloco_norm = normalizar_bloco_codigo(bloco)
    return bloco_norm in _blocos_codigo_sindico(usuario)


def _label_agrupamentos_sindico(usuario):
    nomes = _agrupamentos_sindico(usuario)
    return ", ".join(nomes) if nomes else "—"


def _registrar_auditoria(usuario, mensagem):
    db.session.add(
        LogAuditoria(
            usuario_id=usuario.id,
            condominio_id=resolver_condominio_id(usuario=usuario),
            mensagem=mensagem,
        )
    )


def _criar_notificacao(
    condominio_id, perfil_destino, titulo, mensagem, unidade_id=None
):
    """Enfileira notificação na sessão atual (commit fica a cargo do chamador)."""
    if not condominio_id or perfil_destino not in PerfilDestinoNotificacao.CHOICES:
        return
    if perfil_destino == PerfilDestinoNotificacao.MORADOR and not unidade_id:
        return
    db.session.add(
        Notificacao(
            condominio_id=condominio_id,
            unidade_id=unidade_id,
            perfil_destino=perfil_destino,
            titulo=titulo,
            mensagem=mensagem,
            lida=False,
        )
    )


def _destinatario_notificacoes():
    """Escopo do usuário atual: (perfil, condominio_id, unidade_id) ou Nones."""
    unidade = get_unidade_logada()
    usuario = get_current_user()
    if unidade and not usuario:
        return (
            PerfilDestinoNotificacao.MORADOR,
            unidade.condominio_id,
            unidade.id,
        )
    if usuario and usuario.role in (Role.PORTEIRO, Role.ADMIN, Role.SUPERADMIN):
        condominio_id = _condominio_id_portaria(usuario)
        if condominio_id:
            return (PerfilDestinoNotificacao.PORTARIA, condominio_id, None)
    return None, None, None


def _query_notificacoes(perfil, condominio_id, unidade_id):
    query = Notificacao.query.filter_by(
        condominio_id=condominio_id,
        perfil_destino=perfil,
    )
    if perfil == PerfilDestinoNotificacao.MORADOR:
        query = query.filter_by(unidade_id=unidade_id)
    else:
        query = query.filter(Notificacao.unidade_id.is_(None))
    return query.order_by(Notificacao.lida.asc(), Notificacao.created_at.desc())


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
        usuario = get_current_user()
        if usuario and usuario.role == Role.PORTEIRO:
            flash("Acesso restrito à portaria.", "danger")
            return redirect(url_for("portaria_dashboard"))
        if usuario or get_unidade_logada():
            return view(*args, **kwargs)
        flash("Faça login para acessar o módulo de reservas.", "warning")
        return redirect(url_for("tenant_login", slug=_slug_sessao_ou_prp()))

    return wrapped


DIAS_FUNCIONAMENTO_VALIDOS = ("seg", "ter", "qua", "qui", "sex", "sab", "dom")


def gestao_espacos_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        usuario = get_current_user()
        if usuario and usuario.role in (Role.ADMIN, Role.ASSISTENTE, Role.SINDICO):
            if not usuario.condominio_id:
                flash(
                    "Conta sem condomínio vinculado. Contate a administração.",
                    "danger",
                )
                return redirect(url_for("reservas"))
            return view(*args, **kwargs)
        flash("Acesso restrito para gestão de espaços.", "danger")
        return redirect(url_for("reservas"))

    return wrapped


def _usuario_pode_gerenciar_espaco(usuario, espaco):
    if not usuario or not espaco:
        return False
    if not usuario.condominio_id or espaco.condominio_id != usuario.condominio_id:
        return False
    if usuario.role == Role.SINDICO:
        return _sindico_gerencia_bloco(usuario, espaco.bloco_vinculado)
    if usuario.role in (Role.ADMIN, Role.ASSISTENTE):
        return espaco.gerenciado_por == "admin"
    return False


def _reservas_pendentes_por_jurisdicao(usuario):
    if not usuario or not usuario.condominio_id:
        return []
    query = (
        Reserva.query.join(Reserva.espaco)
        .filter(
            Reserva.status == "Pendente",
            EspacoComum.condominio_id == usuario.condominio_id,
        )
    )
    if usuario.role == Role.SINDICO:
        chaves = _chaves_agrupamento_sindico(usuario)
        if not chaves:
            return []
        query = query.filter(EspacoComum.bloco_vinculado.in_(chaves))
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
    """Porta genérica da plataforma — redireciona ao tenant legado PRP."""
    return redirect(url_for("tenant_login", slug="prp"))


def _render_tenant_login(condominio, **extra):
    return render_template(
        "tenant_login.html",
        **_contexto_index(condominio=condominio, slug=condominio.slug, **extra),
    )


def _resposta_condominio_inativo(condominio):
    """Bloqueia portas /c/<slug>/ de clientes com soft delete (ativo=False)."""
    return (
        render_template("condominio_suspenso.html", condominio=condominio),
        403,
    )


def _carregar_condominio_entrada(slug):
    """Carrega tenant por slug e bloqueia se inativo."""
    condominio = obter_condominio_por_slug(slug)
    if not condominio_esta_ativo(condominio):
        return condominio, _resposta_condominio_inativo(condominio)
    return condominio, None


def _redirect_pos_login_equipe(usuario):
    if usuario.role == Role.SINDICO:
        return redirect(url_for("sindico_dashboard"))
    if usuario.role == Role.PORTEIRO:
        return redirect(url_for("portaria_dashboard"))
    return _redirect_pos_login_admin(usuario)


def tenant_login(slug):
    """Login unificado: Morador (aba) e Equipe (admin/síndico/porteiro)."""
    condominio, bloqueio = _carregar_condominio_entrada(slug)
    if bloqueio is not None:
        return bloqueio
    session["tenant_slug"] = condominio.slug

    usuario_logado = get_current_user()
    if usuario_logado and usuario_logado.condominio_id == condominio.id:
        if usuario_logado.role in (Role.ADMIN, Role.ASSISTENTE, Role.SINDICO, Role.PORTEIRO):
            return _redirect_pos_login_equipe(usuario_logado)

    if request.method == "POST" and request.form.get("perfil") in ("admin", "equipe"):
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        usuario = Usuario.query.filter(
            Usuario.username == username,
            Usuario.role.in_(
                [Role.ADMIN, Role.ASSISTENTE, Role.SINDICO, Role.PORTEIRO]
            ),
            Usuario.condominio_id == condominio.id,
        ).first()
        if usuario and usuario.check_password(password):
            login_usuario(usuario)
            return _redirect_pos_login_equipe(usuario)
        flash("Usuário ou senha inválidos.", "danger")
        return _render_tenant_login(condominio, active_tab="equipe")

    aba_equipe = request.args.get("tab") == "equipe"
    return _render_tenant_login(
        condominio, active_tab="equipe" if aba_equipe else "morador"
    )


def verificar_unidade(slug):
    condominio, bloqueio = _carregar_condominio_entrada(slug)
    if bloqueio is not None:
        return bloqueio
    session["tenant_slug"] = condominio.slug

    bloco, apartamento = normalizar_bloco_apartamento(
        request.form.get("bloco", ""),
        request.form.get("apartamento", ""),
    )

    if not validar_unidade(bloco, apartamento):
        flash("Combinação de bloco e apartamento inválida.", "danger")
        return _render_tenant_login(
            condominio,
            active_tab="morador",
            bloco=bloco,
            apartamento=apartamento,
        )

    unidade = _buscar_unidade(bloco, apartamento, condominio_id=condominio.id)

    if not unidade:
        session["cadastro_bloco"] = bloco
        session["cadastro_apartamento"] = apartamento
        session["cadastro_condominio_id"] = condominio.id
        session["cadastro_slug"] = condominio.slug
        return redirect(url_for("cadastro_inicial", slug=condominio.slug))

    if unidade.status == StatusUnidade.REPROVADA:
        db.session.delete(unidade)
        db.session.commit()
        session["cadastro_bloco"] = bloco
        session["cadastro_apartamento"] = apartamento
        session["cadastro_condominio_id"] = condominio.id
        session["cadastro_slug"] = condominio.slug
        return redirect(url_for("cadastro_inicial", slug=condominio.slug))

    senha = request.form.get("senha", "").strip()
    exige_senha = unidade.status in (
        StatusUnidade.PENDENTE,
        StatusUnidade.APROVADA,
        StatusUnidade.REGISTRADA,
    )

    if exige_senha:
        if not senha:
            return _render_tenant_login(
                condominio,
                active_tab="morador",
                exige_senha=True,
                bloco=bloco,
                apartamento=apartamento,
            )

        if not unidade.check_password(senha):
            flash("Senha incorreta.", "danger")
            return _render_tenant_login(
                condominio,
                active_tab="morador",
                exige_senha=True,
                bloco=bloco,
                apartamento=apartamento,
            )

    if unidade.status == StatusUnidade.PENDENTE:
        return _render_tenant_login(
            condominio,
            active_tab="morador",
            pendente=True,
            bloco=bloco,
            apartamento=apartamento,
        )

    login_unidade(unidade)
    return redirect(url_for("atualizar_dados"))


def esqueci_senha():
    if request.method == "POST":
        email_solicitado = request.form.get("email", "").strip().lower()
        mensagem_generica = (
            "Se o e-mail estiver cadastrado, enviaremos instruções para redefinição de senha."
        )

        condominio_id_solicitacao = _condominio_id_da_sessao()
        unidade, email_destino = _buscar_unidade_e_email_login(
            email_solicitado, condominio_id=condominio_id_solicitacao
        )
        if unidade and email_destino:
            try:
                token = gerar_token_redefinicao(
                    email_solicitado,
                    SALT_RECUPERACAO_MORADOR,
                    condominio_id=condominio_id_solicitacao,
                )
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
        return redirect(url_for("tenant_login", slug=_slug_sessao_ou_prp()))

    return render_template(
        "esqueci_senha.html",
        slug=_slug_sessao_ou_prp(),
    )


def redefinir_senha(token):
    email, condominio_id_token, emitido_em = verificar_token_redefinicao(
        token, SALT_RECUPERACAO_MORADOR
    )
    # Sem condominio_id no token: ou é um link antigo (formato anterior a esta
    # correção) ou foi solicitado fora de qualquer tenant — em ambos os casos
    # não é seguro resolver a unidade pelo estado da sessão atual (poderia
    # pertencer a outro condomínio). Pede para solicitar um link novo.
    if not email or not condominio_id_token:
        flash("Link inválido ou expirado. Solicite uma nova redefinição de senha.", "danger")
        return redirect(url_for("esqueci_senha"))

    unidade, _ = _buscar_unidade_e_email_login(
        email, condominio_id=condominio_id_token
    )
    if not unidade:
        flash("Unidade não encontrada para este e-mail.", "danger")
        return redirect(url_for("esqueci_senha"))

    if unidade.senha_atualizada_em and emitido_em:
        emitido_em_naive = (
            emitido_em.replace(tzinfo=None) if emitido_em.tzinfo else emitido_em
        )
        if unidade.senha_atualizada_em >= emitido_em_naive:
            flash(
                "Este link já foi utilizado ou não é mais válido. "
                "Solicite uma nova redefinição de senha.",
                "danger",
            )
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
        slug = "prp"
        if unidade.condominio and unidade.condominio.slug:
            slug = unidade.condominio.slug
        return redirect(url_for("tenant_login", slug=slug))

    return render_template("redefinir_senha.html", token=token)


def cadastro_inicial(slug):
    condominio, bloqueio = _carregar_condominio_entrada(slug)
    if bloqueio is not None:
        return bloqueio
    session["tenant_slug"] = condominio.slug
    session["cadastro_condominio_id"] = condominio.id
    session["cadastro_slug"] = condominio.slug

    if request.method == "POST":
        return salvar_cadastro()

    bloco = session.get("cadastro_bloco")
    apartamento = session.get("cadastro_apartamento")

    if not bloco or not apartamento or not validar_unidade(bloco, apartamento):
        flash("Selecione um bloco e apartamento válidos.", "warning")
        return redirect(url_for("tenant_login", slug=condominio.slug))

    if _buscar_unidade(bloco, apartamento, condominio_id=condominio.id):
        flash("Esta unidade já possui cadastro.", "warning")
        return redirect(url_for("tenant_login", slug=condominio.slug))

    return render_template(
        "cadastro_morador.html",
        bloco=bloco,
        apartamento=apartamento,
        modo="cadastro",
        vinculos=VinculoPessoa.CHOICES,
        condominio=condominio,
        slug=condominio.slug,
    )


@unidade_required
def atualizar_dados(unidade):
    if unidade.status not in (StatusUnidade.APROVADA, StatusUnidade.REGISTRADA):
        flash("Esta unidade não pode ser atualizada no momento.", "warning")
        return redirect(url_for("tenant_login", slug=_slug_sessao_ou_prp()))

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
        slug=_slug_sessao_ou_prp(),
    )


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

    resgates_unidade = ResgateCupom.query.filter_by(
        cupom_id=cupom.id,
        unidade_id=unidade.id,
    ).count()
    if resgates_unidade >= cupom.limite_por_unidade:
        flash("Você atingiu o limite de resgates para esta oferta.", "danger")
        return redirect(url_for("clube_vantagens"))

    # Reserva atomicamente uma "vaga" no limite total do cupom: um único
    # UPDATE é indivisível mesmo sob concorrência — diferente de um
    # COUNT() seguido de INSERT em requisições separadas, que permitiria
    # duas requisições simultâneas passarem pela checagem ao mesmo tempo.
    resultado = db.session.execute(
        text(
            "UPDATE cupom SET total_resgatado = total_resgatado + 1 "
            "WHERE id = :id AND (limite_total IS NULL OR total_resgatado < limite_total)"
        ),
        {"id": cupom.id},
    )
    if resultado.rowcount == 0:
        db.session.rollback()
        flash("Oferta esgotada.", "danger")
        return redirect(url_for("clube_vantagens"))

    # Revalida o limite por unidade dentro da mesma transação: o UPDATE acima
    # já tomou o lock de escrita do SQLite para este cupom, então nenhuma
    # outra requisição concorrente consegue avançar até este commit/rollback.
    resgates_unidade_atual = ResgateCupom.query.filter_by(
        cupom_id=cupom.id,
        unidade_id=unidade.id,
    ).count()
    if resgates_unidade_atual >= cupom.limite_por_unidade:
        db.session.rollback()
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
        # Libera a vaga reservada no UPDATE atômico acima — sem isso, a falha
        # em gerar o código consumiria um resgate do limite sem criar o
        # ResgateCupom correspondente.
        db.session.rollback()
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
        condominio_id = condominio_id_obrigatorio(usuario)
        if usuario.role == Role.SINDICO:
            chaves_agrupamento = _chaves_agrupamento_sindico(usuario)
            espacos = (
                EspacoComum.query.filter(
                    EspacoComum.condominio_id == condominio_id,
                    EspacoComum.bloco_vinculado.in_(chaves_agrupamento),
                )
                .order_by(EspacoComum.nome)
                .all()
                if chaves_agrupamento
                else []
            )
        elif usuario.role in (Role.ADMIN, Role.ASSISTENTE):
            espacos = (
                EspacoComum.query.filter_by(
                    condominio_id=condominio_id, gerenciado_por="admin"
                )
                .order_by(EspacoComum.nome)
                .all()
            )
            unidades_gestao = (
                Unidade.query.filter_by(condominio_id=condominio_id)
                .order_by(Unidade.bloco, Unidade.apartamento)
                .all()
            )

        query_pendentes = Reserva.query.join(EspacoComum).filter(
            Reserva.status == "Pendente",
            EspacoComum.condominio_id == condominio_id,
        )
        query_historico = Reserva.query.join(EspacoComum).filter(
            Reserva.status != "Pendente",
            EspacoComum.condominio_id == condominio_id,
        )

        if usuario.role == Role.SINDICO:
            chaves_agrupamento = _chaves_agrupamento_sindico(usuario)
            filtro_jurisdicao = EspacoComum.bloco_vinculado.in_(chaves_agrupamento or [""])
            blocos_sindico = _blocos_codigo_sindico(usuario)
            unidades_gestao = (
                Unidade.query.filter(
                    Unidade.condominio_id == condominio_id,
                    Unidade.bloco.in_(blocos_sindico),
                )
                .order_by(Unidade.bloco, Unidade.apartamento)
                .all()
                if blocos_sindico
                else []
            )
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
        condominio_id = unidade.condominio_id
        espacos_disponiveis = (
            EspacoComum.query.filter(
                EspacoComum.condominio_id == condominio_id,
                or_(
                    EspacoComum.apenas_moradores_bloco.is_(False),
                    EspacoComum.bloco_vinculado == unidade.bloco,
                ),
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
        espaco = _espaco_do_tenant(int(espaco_id), unidade.condominio_id)
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
    try:
        db.session.commit()
    except IntegrityError:
        # Fecha a janela de corrida: duas requisições podem passar pela
        # checagem acima antes de qualquer uma commitar; o índice único no
        # banco (ux_reserva_espaco_data_ativa) é quem garante a exclusão.
        db.session.rollback()
        flash("Já existe uma reserva pendente/aprovada para este espaço nesta data.", "warning")
        return redirect(url_for("reservas"))

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
        espaco = _espaco_do_tenant(
            int(espaco_id), condominio_id_obrigatorio(usuario)
        )
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
            unidade = _unidade_do_tenant(
                int(unidade_id), condominio_id_obrigatorio(usuario)
            )
        except ValueError:
            flash("Unidade inválida para vinculação da reserva.", "danger")
            return redirect(url_for("reservas"))

        if usuario.role == Role.SINDICO and not _sindico_gerencia_bloco(
            usuario, unidade.bloco
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
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Já existe uma reserva pendente/aprovada para este espaço nesta data.", "warning")
        return redirect(url_for("reservas"))

    flash("Reserva criada com sucesso.", "success")
    return redirect(url_for("reservas"))


@gestao_espacos_required
def responder_reserva(reserva_id):
    usuario = get_current_user()
    reserva = _reserva_do_tenant(reserva_id, condominio_id_obrigatorio(usuario))
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
    condominio_id = condominio_id_obrigatorio(usuario)
    if usuario.role == Role.SINDICO:
        chaves = _chaves_agrupamento_sindico(usuario)
        query = Reserva.query.join(EspacoComum).filter(
            EspacoComum.condominio_id == condominio_id,
            EspacoComum.bloco_vinculado.in_(chaves or [""]),
            Reserva.status.in_(["Pendente", "Aprovada"]),
        )
    elif usuario.role in (Role.ADMIN, Role.ASSISTENTE):
        query = Reserva.query.join(EspacoComum).filter(
            EspacoComum.condominio_id == condominio_id,
            or_(
                and_(
                    EspacoComum.gerenciado_por == "admin",
                    Reserva.status.in_(["Pendente", "Aprovada"]),
                ),
                and_(
                    EspacoComum.gerenciado_por == "sindico",
                    Reserva.status == "Aprovada",
                ),
            ),
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
    reserva = _reserva_do_tenant(reserva_id, condominio_id_obrigatorio(usuario))

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
    reserva = _reserva_do_tenant(reserva_id, condominio_id_obrigatorio(usuario))

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
        espaco = _espaco_do_tenant(int(espaco_id), condominio_id_obrigatorio(usuario))
        if usuario.role == Role.SINDICO:
            if not _sindico_gerencia_bloco(usuario, espaco.bloco_vinculado):
                flash("Você não tem permissão para editar este espaço.", "danger")
                return redirect(url_for("reservas"))
        elif usuario.role in (Role.ADMIN, Role.ASSISTENTE):
            if espaco.gerenciado_por != "admin":
                flash("Você só pode editar espaços gerenciados pela administração.", "danger")
                return redirect(url_for("reservas"))
    else:
        espaco = EspacoComum(
            tipo="SALAO_FESTAS",
            condominio_id=condominio_id_obrigatorio(usuario),
        )
        db.session.add(espaco)

    espaco.nome = nome
    espaco.valor_reserva = valor_reserva
    espaco.dias_funcionamento = ",".join(dias_selecionados)

    if usuario.role == Role.SINDICO:
        agrupamentos = _agrupamentos_sindico(usuario)
        if not agrupamentos:
            flash("Síndico sem agrupamento vinculado. Contate a administração.", "danger")
            return redirect(url_for("reservas"))
        espaco.gerenciado_por = "sindico"
        # Espaço continua vinculado a um agrupamento; usa o primeiro até haver seletor.
        espaco.bloco_vinculado = agrupamentos[0]
        espaco.apenas_moradores_bloco = apenas_moradores_bloco
    else:
        espaco.gerenciado_por = "admin"
        espaco.bloco_vinculado = None
        espaco.apenas_moradores_bloco = False

    db.session.commit()
    flash("Espaço salvo com sucesso.", "success")
    return redirect(url_for("reservas"))


def sair():
    slug = _slug_logout()
    logout_unidade()
    logout_usuario()
    flash("Sessão encerrada.", "info")
    return redirect(url_for("tenant_login", slug=slug))


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
    slug_retorno = _slug_sessao_ou_prp()

    if modo_atualizacao:
        unidade = unidade_logada
        bloco = unidade.bloco
        apartamento = unidade.apartamento
        condominio_id = unidade.condominio_id
    else:
        if not bloco or not apartamento:
            flash("Sessão expirada. Selecione bloco e apartamento novamente.", "warning")
            return redirect(url_for("tenant_login", slug=slug_retorno))
        condominio_id = session.get("cadastro_condominio_id")
        if not condominio_id:
            flash(
                "Sessão do condomínio expirada. Acesse pelo link do seu condomínio.",
                "warning",
            )
            return redirect(url_for("tenant_login", slug=slug_retorno))
        unidade = None

    bloco, apartamento = normalizar_bloco_apartamento(bloco, apartamento)

    if not validar_unidade(bloco, apartamento):
        flash("Combinação de bloco e apartamento inválida.", "danger")
        return redirect(url_for("tenant_login", slug=slug_retorno))

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
            if _buscar_unidade(bloco, apartamento, condominio_id=condominio_id):
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
                condominio_id=condominio_id,
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
        return redirect(url_for("tenant_login", slug=slug_retorno))

    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        if modo_atualizacao:
            return redirect(url_for("atualizar_dados"))
        return redirect(url_for("cadastro_inicial", slug=slug_retorno))
    except Exception:
        db.session.rollback()
        traceback.print_exc()
        flash(
            "Ocorreu um erro ao salvar o cadastro. Tente novamente em instantes.",
            "danger",
        )
        if modo_atualizacao:
            return redirect(url_for("atualizar_dados"))
        return redirect(url_for("cadastro_inicial", slug=slug_retorno))


def _redirect_pos_login_admin(usuario):
    if usuario.role == Role.ADMIN:
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("admin_index"))



def verificar_unidade_legacy():
    return redirect(url_for("verificar_unidade", slug="prp"), code=307)


def cadastro_inicial_legacy():
    return redirect(url_for("cadastro_inicial", slug="prp"))


def _validar_data_mudanca(data_mudanca):
    """Valida antecedência mínima de 3 dias e proibição de domingo."""
    hoje = date.today()
    data_minima = hoje + timedelta(days=3)
    if data_mudanca < data_minima:
        return (
            "A data da mudança deve ter, no mínimo, 3 dias de antecedência."
        )
    if data_mudanca.weekday() == 6:
        return "Mudanças não são permitidas aos domingos."
    return None


def _nome_responsavel_unidade(unidade):
    responsavel = unidade.pessoas.filter_by(is_responsavel=True).first()
    if responsavel:
        return responsavel.nome_completo
    return "Não informado"


@unidade_required
def mudancas_morador(unidade):
    if unidade.status not in (StatusUnidade.APROVADA, StatusUnidade.REGISTRADA):
        flash(
            "Apenas unidades aprovadas ou registradas podem agendar mudanças.",
            "warning",
        )
        return redirect(url_for("atualizar_dados"))

    if request.method == "POST":
        acao = request.form.get("acao", "solicitar").strip()

        if acao == "cancelar":
            agendamento_id = request.form.get("agendamento_id", type=int)
            if not agendamento_id:
                flash("Solicitação não encontrada.", "danger")
                return redirect(url_for("mudancas_morador"))
            agendamento = _agendamento_do_tenant(
                agendamento_id, unidade.condominio_id
            )
            if agendamento.unidade_id != unidade.id:
                flash("Solicitação não encontrada.", "danger")
            elif agendamento.status not in StatusAgendamentoMudanca.PENDENTES:
                flash(
                    "Somente solicitações pendentes podem ser canceladas.",
                    "warning",
                )
            else:
                agendamento.status = StatusAgendamentoMudanca.CANCELADA
                db.session.commit()
                flash("Solicitação de mudança cancelada.", "success")
            return redirect(url_for("mudancas_morador"))

        tipo = request.form.get("tipo", "").strip()
        data_str = request.form.get("data_mudanca", "").strip()
        observacoes = request.form.get("observacoes", "").strip() or None

        if tipo not in StatusAgendamentoMudanca.TIPOS:
            flash("Selecione o tipo da mudança (Entrada ou Saída).", "danger")
            return redirect(url_for("mudancas_morador"))

        try:
            data_mudanca = datetime.strptime(data_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            flash("Informe uma data válida para a mudança.", "danger")
            return redirect(url_for("mudancas_morador"))

        erro_data = _validar_data_mudanca(data_mudanca)
        if erro_data:
            flash(erro_data, "danger")
            return redirect(url_for("mudancas_morador"))

        agendamento = AgendamentoMudanca(
            unidade_id=unidade.id,
            tipo=tipo,
            data_mudanca=data_mudanca,
            status=StatusAgendamentoMudanca.PENDENTE_SINDICO,
            observacoes=observacoes,
            condominio_id=unidade.condominio_id,
        )
        db.session.add(agendamento)
        db.session.commit()
        flash(
            "Solicitação de mudança enviada. Aguarde a aprovação do síndico.",
            "success",
        )
        return redirect(url_for("mudancas_morador"))

    historico = (
        AgendamentoMudanca.query.filter_by(
            unidade_id=unidade.id, condominio_id=unidade.condominio_id
        )
        .order_by(
            AgendamentoMudanca.data_mudanca.desc(),
            AgendamentoMudanca.data_solicitacao.desc(),
        )
        .all()
    )
    data_minima = (date.today() + timedelta(days=3)).isoformat()
    return render_template(
        "mudancas_morador.html",
        unidade=unidade,
        historico=historico,
        data_minima=data_minima,
        status_pendentes=StatusAgendamentoMudanca.PENDENTES,
    )


@unidade_required
def morador_autorizacoes(unidade):
    """Painel do morador: autorizações prévias de visitantes/prestadores."""
    if unidade.status not in (StatusUnidade.APROVADA, StatusUnidade.REGISTRADA):
        flash(
            "Apenas unidades aprovadas ou registradas podem criar autorizações.",
            "warning",
        )
        return redirect(url_for("atualizar_dados"))

    if not unidade.condominio_id:
        flash("Unidade sem condomínio vinculado. Contate a administração.", "danger")
        return redirect(url_for("atualizar_dados"))

    if request.method == "POST":
        nome_visitante = request.form.get("nome_visitante", "").strip()
        documento = request.form.get("documento", "").strip() or None
        data_str = request.form.get("data_prevista", "").strip()
        tipo = request.form.get("tipo", "").strip()

        if not nome_visitante:
            flash("Informe o nome do visitante ou prestador.", "danger")
            return redirect(url_for("morador_autorizacoes"))

        if tipo not in TipoVisitante.CHOICES:
            flash("Selecione o tipo (Visitante ou Prestador).", "danger")
            return redirect(url_for("morador_autorizacoes"))

        try:
            data_prevista = datetime.strptime(data_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            flash("Informe uma data prevista válida.", "danger")
            return redirect(url_for("morador_autorizacoes"))

        if data_prevista < date.today():
            flash("A data prevista não pode ser anterior a hoje.", "danger")
            return redirect(url_for("morador_autorizacoes"))

        autorizacao = AutorizacaoAcesso(
            condominio_id=unidade.condominio_id,
            unidade_id=unidade.id,
            nome_visitante=nome_visitante,
            documento=documento,
            data_prevista=data_prevista,
            tipo=tipo,
            status=StatusAutorizacaoAcesso.PENDENTE,
        )
        db.session.add(autorizacao)
        _criar_notificacao(
            condominio_id=unidade.condominio_id,
            perfil_destino=PerfilDestinoNotificacao.PORTARIA,
            titulo="Nova Autorização",
            mensagem=(
                f"A unidade {unidade.identificador} autorizou a entrada de "
                f"{nome_visitante}."
            ),
            unidade_id=None,
        )
        db.session.commit()
        flash("Autorização criada com sucesso.", "success")
        return redirect(url_for("morador_autorizacoes"))

    hoje = date.today()
    registros = (
        AutorizacaoAcesso.query.filter_by(
            unidade_id=unidade.id,
            condominio_id=unidade.condominio_id,
        )
        .order_by(
            AutorizacaoAcesso.data_prevista.desc(),
            AutorizacaoAcesso.created_at.desc(),
        )
        .all()
    )

    autorizacoes_ativas = [
        item
        for item in registros
        if item.status == StatusAutorizacaoAcesso.PENDENTE
        and item.data_prevista >= hoje
    ]
    autorizacoes_historico = [
        item
        for item in registros
        if item.status != StatusAutorizacaoAcesso.PENDENTE
        or item.data_prevista < hoje
    ]

    return render_template(
        "autorizacoes_morador.html",
        unidade=unidade,
        autorizacoes_ativas=autorizacoes_ativas,
        autorizacoes_historico=autorizacoes_historico,
        tipos_autorizacao=TipoVisitante.CHOICES,
        data_minima=hoje.isoformat(),
    )


@unidade_required
def morador_autorizacoes_cancelar(unidade, autorizacao_id):
    """Cancela autorização prévia com proteção Anti-IDOR por unidade."""
    autorizacao = AutorizacaoAcesso.query.filter_by(
        id=autorizacao_id,
        condominio_id=unidade.condominio_id,
    ).first()

    # Anti-IDOR: só cancela se pertencer à unidade logada.
    if not autorizacao or autorizacao.unidade_id != unidade.id:
        flash("Autorização não encontrada.", "danger")
        return redirect(url_for("morador_autorizacoes"))

    if autorizacao.status != StatusAutorizacaoAcesso.PENDENTE:
        flash("Somente autorizações pendentes podem ser canceladas.", "warning")
        return redirect(url_for("morador_autorizacoes"))

    autorizacao.status = StatusAutorizacaoAcesso.CANCELADA
    db.session.commit()
    flash("Autorização cancelada.", "success")
    return redirect(url_for("morador_autorizacoes"))


def _unidade_pode_abrir_ocorrencia(unidade):
    if unidade.status not in (StatusUnidade.APROVADA, StatusUnidade.REGISTRADA):
        flash(
            "Apenas unidades aprovadas ou registradas podem abrir ocorrências.",
            "warning",
        )
        return False
    if not unidade.condominio_id:
        flash("Unidade sem condomínio vinculado. Contate a administração.", "danger")
        return False
    return True


@unidade_required
def morador_ocorrencias(unidade):
    """Lista os chamados da unidade logada."""
    if not _unidade_pode_abrir_ocorrencia(unidade):
        return redirect(url_for("atualizar_dados"))

    ocorrencias = (
        Ocorrencia.query.filter_by(
            unidade_id=unidade.id,
            condominio_id=unidade.condominio_id,
        )
        .order_by(Ocorrencia.created_at.desc())
        .all()
    )
    return render_template(
        "morador/ocorrencias.html",
        unidade=unidade,
        ocorrencias=ocorrencias,
        categorias=CategoriaOcorrencia.CHOICES,
        status_ocorrencia=StatusOcorrencia,
    )


@unidade_required
def morador_ocorrencias_nova(unidade):
    """Abre um novo chamado com foto opcional (blindagem de imagem)."""
    if not _unidade_pode_abrir_ocorrencia(unidade):
        return redirect(url_for("atualizar_dados"))

    titulo = (request.form.get("titulo") or "").strip()
    descricao = (request.form.get("descricao") or "").strip()
    categoria = (request.form.get("categoria") or "").strip()

    if not titulo:
        flash("Informe o título da ocorrência.", "danger")
        return redirect(url_for("morador_ocorrencias"))
    if not descricao:
        flash("Descreva a ocorrência.", "danger")
        return redirect(url_for("morador_ocorrencias"))
    if categoria not in CategoriaOcorrencia.CHOICES:
        flash("Selecione uma categoria válida.", "danger")
        return redirect(url_for("morador_ocorrencias"))

    foto_arquivo, erro_foto = _salvar_foto_ocorrencia(
        request.files.get("foto"),
        prefixo=f"un{unidade.id}",
    )
    if erro_foto:
        flash(erro_foto, "danger")
        return redirect(url_for("morador_ocorrencias"))

    ocorrencia = Ocorrencia(
        condominio_id=unidade.condominio_id,
        unidade_id=unidade.id,
        titulo=titulo[:200],
        descricao=descricao,
        categoria=categoria,
        status=StatusOcorrencia.ABERTO,
        foto_arquivo=foto_arquivo,
    )
    db.session.add(ocorrencia)
    db.session.commit()
    flash("Ocorrência registrada com sucesso.", "success")
    return redirect(url_for("morador_ocorrencias"))


def _layout_notificacoes(perfil):
    usuario = get_current_user()
    if perfil == PerfilDestinoNotificacao.PORTARIA and usuario and usuario.is_porteiro:
        return "portaria_base.html"
    return "base.html"


def listar_notificacoes():
    perfil, condominio_id, unidade_id = _destinatario_notificacoes()
    if not perfil or not condominio_id:
        flash("Faça login para ver as notificações.", "warning")
        return _redirect_login_tenant()

    notificacoes = _query_notificacoes(perfil, condominio_id, unidade_id).all()
    return render_template(
        "notificacoes.html",
        layout=_layout_notificacoes(perfil),
        notificacoes=notificacoes,
        current_user=get_current_user(),
    )


def notificacoes_ler(notificacao_id):
    perfil, condominio_id, unidade_id = _destinatario_notificacoes()
    if not perfil or not condominio_id:
        flash("Faça login para gerenciar notificações.", "warning")
        return _redirect_login_tenant()

    query = Notificacao.query.filter_by(
        id=notificacao_id,
        condominio_id=condominio_id,
        perfil_destino=perfil,
    )
    if perfil == PerfilDestinoNotificacao.MORADOR:
        query = query.filter_by(unidade_id=unidade_id)
    else:
        query = query.filter(Notificacao.unidade_id.is_(None))

    notificacao = query.first()
    if not notificacao:
        flash("Notificação não encontrada.", "danger")
        return redirect(url_for("listar_notificacoes"))

    notificacao.lida = True
    db.session.commit()
    return redirect(url_for("listar_notificacoes"))


def init_app(app):
    from app.blueprints import admin as admin_routes
    from app.blueprints import parceiro as parceiro_routes
    from app.blueprints import portaria as portaria_routes
    from app.blueprints import sindico as sindico_routes
    from app.blueprints import superadmin as superadmin_routes

    parceiro_routes.register(app)
    superadmin_routes.register(app)
    sindico_routes.register(app)
    admin_routes.register(app)
    portaria_routes.register(app)

    app.add_url_rule("/", "index", index, methods=["GET"])
    app.add_url_rule(
        "/c/<slug>/login",
        "tenant_login",
        tenant_login,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/c/<slug>/verificar-unidade",
        "verificar_unidade",
        verificar_unidade,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/c/<slug>/cadastro-inicial",
        "cadastro_inicial",
        cadastro_inicial,
        methods=["GET", "POST"],
    )
    # Legacy bypass — não quebra bookmarks antigos.
    app.add_url_rule(
        "/verificar-unidade",
        "verificar_unidade_legacy",
        verificar_unidade_legacy,
        methods=["POST"],
    )
    app.add_url_rule(
        "/cadastro-inicial",
        "cadastro_inicial_legacy",
        cadastro_inicial_legacy,
        methods=["GET"],
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
        "/atualizar-dados", "atualizar_dados", atualizar_dados, methods=["GET"]
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
        "/mudancas",
        "mudancas_morador",
        mudancas_morador,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/morador/autorizacoes",
        "morador_autorizacoes",
        morador_autorizacoes,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/morador/autorizacoes/cancelar/<int:autorizacao_id>",
        "morador_autorizacoes_cancelar",
        morador_autorizacoes_cancelar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/morador/ocorrencias",
        "morador_ocorrencias",
        morador_ocorrencias,
        methods=["GET"],
    )
    app.add_url_rule(
        "/morador/ocorrencias/nova",
        "morador_ocorrencias_nova",
        morador_ocorrencias_nova,
        methods=["POST"],
    )
    app.add_url_rule(
        "/notificacoes",
        "listar_notificacoes",
        listar_notificacoes,
        methods=["GET"],
    )
    app.add_url_rule(
        "/notificacoes/ler/<int:notificacao_id>",
        "notificacoes_ler",
        notificacoes_ler,
        methods=["POST"],
    )
