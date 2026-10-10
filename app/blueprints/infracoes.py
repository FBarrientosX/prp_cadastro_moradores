"""Rito de infrações: registro, defesa do morador e multa no financeiro."""

import json
import os
import re
import secrets
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from flask import (
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

from app import db
from app.auth import (
    admin_or_sindico_required,
    get_current_user,
    unidade_required,
)
from app.models import (
    Condominio,
    EscopoRepasse,
    FundoFinanceiro,
    Infracao,
    PlanoConta,
    Role,
    StatusInfracao,
    TipoInfracao,
    TipoPlanoConta,
    Unidade,
)

_FUSO = ZoneInfo("America/Sao_Paulo")
_EXTENSOES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "pdf": "application/pdf",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mov": "video/quicktime",
}
_MAX_ANEXO = 8 * 1024 * 1024
_MAX_ARQUIVOS = 4
_NOME_ARQUIVO = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,80}$")
_CODIGOS_PLANO = ("1.9.9", "1.9.8", "1.9.7", "1.9.6")


def _hoje():
    return datetime.now(_FUSO).date()


def _parse_data(texto):
    bruto = (texto or "").strip()
    for formato in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(bruto, formato).date()
        except ValueError:
            continue
    return None


def _parse_valor(texto):
    from app.blueprints.financeiro import _parse_valor as parse_financeiro

    return parse_financeiro(texto)


def _dias_defesa(condominio):
    dias = getattr(condominio, "dias_padrao_defesa_multa", None)
    try:
        dias = int(dias)
    except (TypeError, ValueError):
        dias = 15
    return min(365, max(1, dias))


def _anexos(infracao):
    try:
        dados = json.loads(infracao.anexos_json or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(dados, list):
        return []
    return [
        item
        for item in dados
        if isinstance(item, dict) and _NOME_ARQUIVO.match(str(item.get("arquivo") or ""))
    ]


def _assinatura_valida(conteudo, extensao):
    if extensao in ("jpg", "jpeg"):
        return conteudo.startswith(b"\xff\xd8\xff")
    if extensao == "png":
        return conteudo.startswith(b"\x89PNG\r\n\x1a\n")
    if extensao == "webp":
        return (
            len(conteudo) >= 12
            and conteudo[:4] == b"RIFF"
            and conteudo[8:12] == b"WEBP"
        )
    if extensao == "pdf":
        return conteudo.startswith(b"%PDF")
    if extensao == "webm":
        return conteudo.startswith(b"\x1a\x45\xdf\xa3")
    if extensao in ("mp4", "mov"):
        return len(conteudo) >= 12 and conteudo[4:8] == b"ftyp"
    return False


def _ler_uploads(origem):
    """Lê os anexos do formulário. Devolve (lista pronta para gravar, erro)."""
    arquivos = request.files.getlist("anexos")
    escolhidos = [arquivo for arquivo in arquivos if arquivo and arquivo.filename]
    if len(escolhidos) > _MAX_ARQUIVOS:
        return None, f"Envie no máximo {_MAX_ARQUIVOS} anexos."
    gravados = []
    pasta = current_app.config["UPLOAD_INFRACOES_FOLDER"]
    os.makedirs(pasta, exist_ok=True)
    for arquivo in escolhidos:
        nome_seguro = secure_filename(arquivo.filename)
        if not nome_seguro or "." not in nome_seguro:
            _apagar_uploads(gravados)
            return None, "Envie foto, PDF ou vídeo (JPG, PNG, WEBP, PDF, MP4, WEBM ou MOV)."
        extensao = nome_seguro.rsplit(".", 1)[-1].lower()
        if extensao not in _EXTENSOES:
            _apagar_uploads(gravados)
            return None, "Envie foto, PDF ou vídeo (JPG, PNG, WEBP, PDF, MP4, WEBM ou MOV)."
        arquivo.stream.seek(0, os.SEEK_END)
        tamanho = arquivo.stream.tell()
        arquivo.stream.seek(0)
        if tamanho <= 0 or tamanho > _MAX_ANEXO:
            _apagar_uploads(gravados)
            return None, "Cada anexo deve ter no máximo 8 MB."
        conteudo = arquivo.stream.read()
        arquivo.stream.seek(0)
        if not _assinatura_valida(conteudo, extensao):
            _apagar_uploads(gravados)
            return None, "Um dos anexos não corresponde ao tipo de arquivo informado."
        nome_final = f"inf{secrets.token_hex(8)}.{extensao}"
        with open(os.path.join(pasta, nome_final), "wb") as destino:
            destino.write(conteudo)
        gravados.append(
            {
                "arquivo": nome_final,
                "nome": nome_seguro[:80],
                "origem": origem,
            }
        )
    return gravados, None


def _apagar_uploads(itens):
    pasta = current_app.config["UPLOAD_INFRACOES_FOLDER"]
    for item in itens or []:
        nome = str(item.get("arquivo") or "")
        if not _NOME_ARQUIVO.match(nome):
            continue
        caminho = os.path.join(pasta, nome)
        if os.path.isfile(caminho):
            os.remove(caminho)


def _unidades_do_ator(usuario, condominio_id):
    from app.routes import _blocos_codigo_sindico

    consulta = Unidade.query.filter(
        Unidade.condominio_id == condominio_id,
        Unidade.eh_setor_interno.is_(False),
    )
    if usuario.role == Role.SINDICO:
        blocos = _blocos_codigo_sindico(usuario)
        if not blocos:
            return []
        consulta = consulta.filter(Unidade.bloco.in_(blocos))
    return consulta.order_by(Unidade.bloco.asc(), Unidade.apartamento.asc()).all()


def _pode_unidade(usuario, unidade):
    from app.routes import _sindico_gerencia_bloco

    if unidade is None or unidade.eh_setor_interno:
        return False
    if usuario.role == Role.ADMIN:
        return True
    if usuario.role == Role.SINDICO:
        return _sindico_gerencia_bloco(usuario, unidade.bloco)
    return False


def _infracao_do_ator(infracao_id, usuario, condominio_id):
    infracao = (
        Infracao.query.filter_by(id=infracao_id, condominio_id=condominio_id)
        .first()
    )
    if infracao is None or not _pode_unidade(usuario, infracao.unidade):
        return None
    return infracao


def _redirecionar_lista(**extras):
    usuario = get_current_user()
    endpoint = "sindico_infracoes" if usuario and usuario.role == Role.SINDICO else "admin_infracoes"
    return redirect(url_for(endpoint, **extras))


def _notificar_morador(infracao, titulo, mensagem):
    from app.routes import _criar_notificacao
    from app.models import PerfilDestinoNotificacao

    _criar_notificacao(
        infracao.condominio_id,
        PerfilDestinoNotificacao.MORADOR,
        titulo,
        mensagem,
        unidade_id=infracao.unidade_id,
        link_destino=f"/morador/infracoes?foco={infracao.id}",
        tipo="INFRACAO",
    )


def _notificar_gestao(infracao, titulo, mensagem):
    from app.models import PerfilDestinoNotificacao, Usuario
    from app.routes import _criar_notificacao, _sindico_gerencia_bloco

    _criar_notificacao(
        infracao.condominio_id,
        PerfilDestinoNotificacao.ADMIN,
        titulo,
        mensagem,
        link_destino=f"/admin/infracoes?foco={infracao.id}",
        tipo="INFRACAO",
    )
    unidade = infracao.unidade
    if unidade is None:
        return
    sindicos = Usuario.query.filter_by(
        condominio_id=infracao.condominio_id,
        role=Role.SINDICO,
    ).all()
    if not any(_sindico_gerencia_bloco(sindico, unidade.bloco) for sindico in sindicos):
        return
    _criar_notificacao(
        infracao.condominio_id,
        PerfilDestinoNotificacao.SINDICO,
        titulo,
        mensagem,
        unidade_id=unidade.id,
        link_destino=f"/sindico/infracoes?foco={infracao.id}",
        tipo="INFRACAO",
    )


def _plano_multas(condominio_id):
    plano = PlanoConta.query.filter_by(
        condominio_id=condominio_id,
        nome="Multas e Penalidades",
        tipo=TipoPlanoConta.RECEITA,
    ).first()
    if plano is not None:
        return plano, None
    fundos = (
        FundoFinanceiro.query.filter_by(condominio_id=condominio_id)
        .order_by(FundoFinanceiro.id.asc())
        .all()
    )
    fundo = next(
        (item for item in fundos if "CAIXA" in (item.nome or "").upper()),
        fundos[0] if fundos else None,
    )
    if fundo is None:
        return None, "Cadastre um fundo financeiro antes de confirmar a multa."
    codigo = next(
        (
            candidato
            for candidato in _CODIGOS_PLANO
            if PlanoConta.query.filter_by(
                condominio_id=condominio_id, codigo=candidato
            ).first()
            is None
        ),
        None,
    )
    if codigo is None:
        return None, "Não há código livre no plano de contas para a receita de multas."
    plano = PlanoConta(
        condominio_id=condominio_id,
        codigo=codigo,
        nome="Multas e Penalidades",
        tipo=TipoPlanoConta.RECEITA,
        fundo_id=fundo.id,
        escopo_repasse=EscopoRepasse.ADM_GERAL,
    )
    db.session.add(plano)
    db.session.flush()
    return plano, None


def _vencimento_multa(modo, hoje):
    if modo == "proxima":
        ano = hoje.year + (1 if hoje.month == 12 else 0)
        mes = 1 if hoje.month == 12 else hoje.month + 1
        return date(ano, mes, 10)
    return hoje + timedelta(days=5)


def _criar_cobranca_multa(infracao, modo, hoje):
    from app.blueprints.financeiro import _conta_ativa, gerar_cobranca_automatica
    from app.financeiro_fechamento import (
        competencia_esta_fechada,
        mensagem_competencia_fechada,
    )

    valor = round(float(infracao.valor_multa or 0), 2)
    if infracao.tipo != TipoInfracao.MULTA or valor <= 0:
        return None, None
    conta = _conta_ativa(infracao.condominio_id)
    if conta is None:
        return None, "Cadastre uma conta bancária ativa antes de confirmar a multa."
    plano, erro = _plano_multas(infracao.condominio_id)
    if erro:
        return None, erro
    vencimento = _vencimento_multa(modo, hoje)
    competencia = vencimento.strftime("%Y-%m")
    if competencia_esta_fechada(infracao.condominio_id, competencia):
        return None, mensagem_competencia_fechada(competencia)
    observacao = f"Gerada pela confirmação da infração #{infracao.id}."
    if modo == "proxima":
        observacao += " Embutida na próxima taxa."
    cobranca = gerar_cobranca_automatica(
        condominio_id=infracao.condominio_id,
        unidade=infracao.unidade,
        conta=conta,
        plano=plano,
        titulo=f"Multa — infração #{infracao.id}",
        valor=valor,
        vencimento=vencimento,
        hoje=hoje,
        observacoes=observacao,
        descricao="Multas e Penalidades",
    )
    return cobranca, None


@admin_or_sindico_required
def infracoes_lista():
    from app.routes import _blocos_codigo_sindico

    usuario = get_current_user()
    condominio_id = usuario.condominio_id
    condominio = db.session.get(Condominio, condominio_id)
    unidades = _unidades_do_ator(usuario, condominio_id)
    ids_unidades = [unidade.id for unidade in unidades]
    consulta = Infracao.query.filter(Infracao.condominio_id == condominio_id)
    if usuario.role == Role.SINDICO:
        if not ids_unidades:
            consulta = consulta.filter(Infracao.id == 0)
        else:
            consulta = consulta.filter(Infracao.unidade_id.in_(ids_unidades))

    status = (request.args.get("status") or "").strip()
    tipo = (request.args.get("tipo") or "").strip()
    bloco = (request.args.get("bloco") or "").strip()
    unidade_id = request.args.get("unidade", type=int)
    if status in StatusInfracao.CHOICES:
        consulta = consulta.filter(Infracao.status == status)
    else:
        status = ""
    if tipo in TipoInfracao.CHOICES:
        consulta = consulta.filter(Infracao.tipo == tipo)
    else:
        tipo = ""
    if bloco:
        ids_bloco = [unidade.id for unidade in unidades if unidade.bloco == bloco]
        consulta = consulta.filter(Infracao.unidade_id.in_(ids_bloco or [0]))
    if unidade_id:
        if unidade_id not in ids_unidades:
            unidade_id = None
        else:
            consulta = consulta.filter(Infracao.unidade_id == unidade_id)

    infracoes = consulta.order_by(Infracao.criada_em.desc(), Infracao.id.desc()).all()
    blocos = []
    for unidade in unidades:
        if unidade.bloco not in blocos:
            blocos.append(unidade.bloco)
    if usuario.role == Role.SINDICO:
        permitidos = _blocos_codigo_sindico(usuario)
        blocos = [codigo for codigo in blocos if codigo in permitidos]
    return render_template(
        "admin/infracoes.html",
        infracoes=infracoes,
        unidades=unidades,
        blocos=blocos,
        status_filtro=status,
        tipo_filtro=tipo,
        bloco_filtro=bloco,
        unidade_filtro=unidade_id or "",
        status_choices=StatusInfracao.CHOICES,
        tipo_choices=TipoInfracao.CHOICES,
        status_notificada=StatusInfracao.NOTIFICADA,
        status_analise=StatusInfracao.EM_ANALISE,
        status_confirmada=StatusInfracao.CONFIRMADA,
        status_cancelada=StatusInfracao.CANCELADA,
        tipo_multa=TipoInfracao.MULTA,
        hoje=_hoje(),
        prazo_sugerido=_hoje() + timedelta(days=_dias_defesa(condominio)),
        dias_defesa=_dias_defesa(condominio),
        eh_sindico=usuario.role == Role.SINDICO,
        foco=request.args.get("foco", type=int),
        anexos_por_id={item.id: _anexos(item) for item in infracoes},
    )


@admin_or_sindico_required
def infracoes_criar():
    from app.routes import _registrar_auditoria

    usuario = get_current_user()
    condominio = db.session.get(Condominio, usuario.condominio_id)
    hoje = _hoje()
    unidade_id = request.form.get("unidade_id", type=int)
    unidade = Unidade.query.filter_by(
        id=unidade_id, condominio_id=usuario.condominio_id
    ).first()
    if not _pode_unidade(usuario, unidade):
        flash("Selecione uma unidade do seu condomínio e do seu mandato.", "danger")
        return _redirecionar_lista()

    tipo = (request.form.get("tipo") or "").strip()
    if tipo not in TipoInfracao.CHOICES:
        flash("Selecione advertência ou multa.", "danger")
        return _redirecionar_lista()
    valor = 0.0
    if tipo == TipoInfracao.MULTA:
        try:
            valor = _parse_valor(request.form.get("valor_multa"))
        except (TypeError, ValueError):
            flash("Informe o valor da multa.", "danger")
            return _redirecionar_lista()
        if valor <= 0:
            flash("A multa precisa ter valor maior que zero.", "danger")
            return _redirecionar_lista()

    data_ocorrencia = _parse_data(request.form.get("data_ocorrencia"))
    if data_ocorrencia is None:
        flash("Informe a data da ocorrência.", "danger")
        return _redirecionar_lista()
    if data_ocorrencia > hoje:
        flash("A data da ocorrência não pode ser futura.", "danger")
        return _redirecionar_lista()
    prazo = _parse_data(request.form.get("prazo_defesa"))
    if prazo is None:
        prazo = hoje + timedelta(days=_dias_defesa(condominio))
    if prazo < data_ocorrencia:
        flash("O prazo de defesa não pode ser anterior à ocorrência.", "danger")
        return _redirecionar_lista()

    descricao = (request.form.get("descricao") or "").strip()
    if not descricao or len(descricao) > 5000:
        flash("Descreva a infração em até 5000 caracteres.", "danger")
        return _redirecionar_lista()

    anexos, erro = _ler_uploads("gestao")
    if erro:
        flash(erro, "danger")
        return _redirecionar_lista()

    infracao = Infracao(
        condominio_id=usuario.condominio_id,
        unidade_id=unidade.id,
        tipo=tipo,
        valor_multa=valor,
        descricao=descricao,
        data_ocorrencia=data_ocorrencia,
        status=StatusInfracao.NOTIFICADA,
        prazo_defesa=prazo,
        anexos_json=json.dumps(anexos or []),
        criada_por=(usuario.username or "")[:80],
    )
    db.session.add(infracao)
    db.session.flush()
    prazo_texto = prazo.strftime("%d/%m/%Y")
    titulo = (
        f"🚨 Você recebeu uma notificação de infração. Prazo para defesa: {prazo_texto}"
    )
    _notificar_morador(infracao, titulo[:120], titulo)
    _registrar_auditoria(
        usuario,
        f"Registrou infração #{infracao.id} ({tipo}) na unidade {unidade.identificador}.",
    )
    db.session.commit()
    flash("Infração registrada e o morador foi notificado.", "success")
    return _redirecionar_lista(foco=infracao.id)


def _status_confirmavel(infracao, hoje):
    if infracao.status == StatusInfracao.EM_ANALISE:
        return True
    return (
        infracao.status == StatusInfracao.NOTIFICADA
        and infracao.prazo_defesa < hoje
    )


@admin_or_sindico_required
def infracoes_absolver(infracao_id):
    from app.routes import _registrar_auditoria

    usuario = get_current_user()
    infracao = _infracao_do_ator(infracao_id, usuario, usuario.condominio_id)
    if infracao is None:
        abort(404)
    if infracao.status != StatusInfracao.EM_ANALISE:
        flash("Só é possível acatar a defesa enquanto ela está em análise.", "warning")
        return _redirecionar_lista(foco=infracao.id)
    linhas = Infracao.query.filter_by(
        id=infracao.id,
        condominio_id=usuario.condominio_id,
        status=StatusInfracao.EM_ANALISE,
    ).update({Infracao.status: StatusInfracao.CANCELADA}, synchronize_session=False)
    if not linhas:
        db.session.rollback()
        flash("Esta infração já foi julgada.", "warning")
        return _redirecionar_lista(foco=infracao.id)
    db.session.expire(infracao)
    _notificar_morador(
        infracao,
        f"A defesa da infração {infracao.id} foi acatada.",
        "A penalidade foi cancelada.",
    )
    _registrar_auditoria(usuario, f"Acatou a defesa da infração #{infracao.id}.")
    db.session.commit()
    flash("Defesa acatada. A infração foi cancelada.", "success")
    return _redirecionar_lista(foco=infracao.id)


@admin_or_sindico_required
def infracoes_confirmar(infracao_id):
    from app.routes import _registrar_auditoria

    usuario = get_current_user()
    hoje = _hoje()
    infracao = _infracao_do_ator(infracao_id, usuario, usuario.condominio_id)
    if infracao is None:
        abort(404)
    if not _status_confirmavel(infracao, hoje):
        flash(
            "A penalidade só pode ser confirmada depois da defesa ou do fim do prazo.",
            "warning",
        )
        return _redirecionar_lista(foco=infracao.id)
    status_anterior = infracao.status
    modo = (request.form.get("vencimento_modo") or "avulso").strip()
    if modo not in ("avulso", "proxima"):
        modo = "avulso"
    try:
        cobranca, erro = _criar_cobranca_multa(infracao, modo, hoje)
    except IntegrityError:
        db.session.rollback()
        flash("Não foi possível reservar o nosso número. Tente novamente.", "danger")
        return _redirecionar_lista(foco=infracao.id)
    if erro:
        db.session.rollback()
        flash(erro, "danger")
        return _redirecionar_lista(foco=infracao.id)
    valores = {Infracao.status: StatusInfracao.CONFIRMADA}
    if cobranca is not None:
        valores[Infracao.cobranca_id] = cobranca.id
    linhas = Infracao.query.filter(
        Infracao.id == infracao.id,
        Infracao.condominio_id == usuario.condominio_id,
        Infracao.status == status_anterior,
        Infracao.cobranca_id.is_(None),
    ).update(valores, synchronize_session=False)
    if not linhas:
        db.session.rollback()
        flash("Esta infração já foi julgada.", "warning")
        return _redirecionar_lista(foco=infracao.id)
    db.session.expire(infracao)
    if cobranca is not None:
        mensagem = (
            f"A penalidade da infração {infracao.id} foi confirmada "
            "e a cobrança foi gerada no seu painel financeiro."
        )
    else:
        mensagem = f"A penalidade da infração {infracao.id} foi confirmada."
    _notificar_morador(infracao, mensagem[:120], mensagem)
    _registrar_auditoria(usuario, f"Confirmou a penalidade da infração #{infracao.id}.")
    db.session.commit()
    flash("Penalidade confirmada.", "success")
    return _redirecionar_lista(foco=infracao.id)


@admin_or_sindico_required
def infracoes_anexo(infracao_id, arquivo):
    usuario = get_current_user()
    infracao = _infracao_do_ator(infracao_id, usuario, usuario.condominio_id)
    return _enviar_anexo(infracao, arquivo)


@unidade_required
def morador_infracoes(unidade):
    hoje = _hoje()
    infracoes = (
        Infracao.query.filter_by(
            condominio_id=unidade.condominio_id,
            unidade_id=unidade.id,
        )
        .order_by(Infracao.criada_em.desc(), Infracao.id.desc())
        .all()
    )
    return render_template(
        "morador/infracoes.html",
        infracoes=infracoes,
        hoje=hoje,
        status_notificada=StatusInfracao.NOTIFICADA,
        status_analise=StatusInfracao.EM_ANALISE,
        status_confirmada=StatusInfracao.CONFIRMADA,
        status_cancelada=StatusInfracao.CANCELADA,
        tipo_multa=TipoInfracao.MULTA,
        foco=request.args.get("foco", type=int),
        anexos_por_id={item.id: _anexos(item) for item in infracoes},
    )


@unidade_required
def morador_infracoes_defesa(unidade, infracao_id):
    hoje = _hoje()
    infracao = Infracao.query.filter_by(
        id=infracao_id,
        condominio_id=unidade.condominio_id,
        unidade_id=unidade.id,
    ).first()
    if infracao is None:
        abort(404)
    if infracao.status != StatusInfracao.NOTIFICADA or infracao.prazo_defesa < hoje:
        flash("O prazo de defesa desta infração já encerrou.", "warning")
        return redirect(url_for("morador_infracoes", foco=infracao.id))
    texto = (request.form.get("texto_defesa") or "").strip()
    if not texto or len(texto) > 5000:
        flash("Escreva a defesa em até 5000 caracteres.", "danger")
        return redirect(url_for("morador_infracoes", foco=infracao.id))
    novos, erro = _ler_uploads("defesa")
    if erro:
        flash(erro, "danger")
        return redirect(url_for("morador_infracoes", foco=infracao.id))
    lista = _anexos(infracao) + (novos or [])
    linhas = Infracao.query.filter(
        Infracao.id == infracao.id,
        Infracao.unidade_id == unidade.id,
        Infracao.status == StatusInfracao.NOTIFICADA,
        Infracao.prazo_defesa >= hoje,
    ).update(
        {
            Infracao.status: StatusInfracao.EM_ANALISE,
            Infracao.texto_defesa: texto,
            Infracao.anexos_json: json.dumps(lista),
        },
        synchronize_session=False,
    )
    if not linhas:
        _apagar_uploads(novos)
        db.session.rollback()
        flash("O prazo de defesa desta infração já encerrou.", "warning")
        return redirect(url_for("morador_infracoes", foco=infracao.id))
    db.session.expire(infracao)
    aviso = (
        f"⚖️ Defesa enviada pela Unidade {unidade.identificador} "
        f"referente à infração {infracao.id}."
    )
    _notificar_gestao(infracao, aviso[:120], aviso)
    db.session.commit()
    flash("Defesa enviada. A gestão foi notificada.", "success")
    return redirect(url_for("morador_infracoes", foco=infracao.id))


@unidade_required
def morador_infracoes_anexo(unidade, infracao_id, arquivo):
    infracao = Infracao.query.filter_by(
        id=infracao_id,
        condominio_id=unidade.condominio_id,
        unidade_id=unidade.id,
    ).first()
    return _enviar_anexo(infracao, arquivo)


def _enviar_anexo(infracao, arquivo):
    if infracao is None or not _NOME_ARQUIVO.match(arquivo or ""):
        abort(404)
    if not any(item.get("arquivo") == arquivo for item in _anexos(infracao)):
        abort(404)
    pasta = current_app.config["UPLOAD_INFRACOES_FOLDER"]
    caminho = os.path.join(pasta, arquivo)
    if not os.path.isfile(caminho):
        abort(404)
    extensao = arquivo.rsplit(".", 1)[-1].lower()
    return send_from_directory(pasta, arquivo, mimetype=_EXTENSOES.get(extensao))


def register(app):
    app.add_url_rule(
        "/admin/infracoes",
        "admin_infracoes",
        infracoes_lista,
        methods=["GET"],
    )
    app.add_url_rule(
        "/sindico/infracoes",
        "sindico_infracoes",
        infracoes_lista,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/infracoes/nova",
        "admin_infracoes_criar",
        infracoes_criar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/sindico/infracoes/nova",
        "sindico_infracoes_criar",
        infracoes_criar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/infracoes/<int:infracao_id>/absolver",
        "admin_infracoes_absolver",
        infracoes_absolver,
        methods=["POST"],
    )
    app.add_url_rule(
        "/sindico/infracoes/<int:infracao_id>/absolver",
        "sindico_infracoes_absolver",
        infracoes_absolver,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/infracoes/<int:infracao_id>/confirmar",
        "admin_infracoes_confirmar",
        infracoes_confirmar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/sindico/infracoes/<int:infracao_id>/confirmar",
        "sindico_infracoes_confirmar",
        infracoes_confirmar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/infracoes/<int:infracao_id>/anexo/<arquivo>",
        "admin_infracoes_anexo",
        infracoes_anexo,
        methods=["GET"],
    )
    app.add_url_rule(
        "/sindico/infracoes/<int:infracao_id>/anexo/<arquivo>",
        "sindico_infracoes_anexo",
        infracoes_anexo,
        methods=["GET"],
    )
    app.add_url_rule(
        "/morador/infracoes",
        "morador_infracoes",
        morador_infracoes,
        methods=["GET"],
    )
    app.add_url_rule(
        "/morador/infracoes/<int:infracao_id>/defesa",
        "morador_infracoes_defesa",
        morador_infracoes_defesa,
        methods=["POST"],
    )
    app.add_url_rule(
        "/morador/infracoes/<int:infracao_id>/anexo/<arquivo>",
        "morador_infracoes_anexo",
        morador_infracoes_anexo,
        methods=["GET"],
    )
