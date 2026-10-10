from datetime import date, datetime, timedelta
from html import escape
from html.parser import HTMLParser
from zoneinfo import ZoneInfo
import base64
import io
import os
import random
import re
import string
import traceback
from types import SimpleNamespace

from flask import (
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from sqlalchemy import and_, func, or_, text
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

from app import ALLOWED_EXTENSIONS, db
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
from app.drive_api import upload_file_stream
from app.email_service import enviar_email_redefinicao_senha
from app.models import (
    AgendamentoMudanca,
    AutorizacaoAcesso,
    CategoriaOcorrencia,
    CategoriaParceiro,
    Condominio,
    CredencialAcesso,
    Cupom,
    Encomenda,
    LogAuditoria,
    Notificacao,
    Ocorrencia,
    Parceiro,
    PerfilDestinoNotificacao,
    Pessoa,
    ResgateCupom,
    Role,
    StatusAgendamentoMudanca,
    StatusAutorizacaoAcesso,
    StatusDocumento,
    StatusEncomenda,
    StatusOcorrencia,
    StatusPessoa,
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
    get_blocos,
    get_condominio_estrutura,
    normalizar_bloco_apartamento,
    nome_foto_facial_seguro,
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


def _arquivo_documento_valido(arquivo):
    """Valida presença e extensão de documento de cadastro (pdf/png/jpg/jpeg)."""
    if not arquivo or not arquivo.filename:
        return False, None
    nome_seguro = secure_filename(arquivo.filename)
    if not nome_seguro or "." not in nome_seguro:
        return False, "Envie o documento em PDF, PNG, JPG ou JPEG."
    extensao = nome_seguro.rsplit(".", 1)[-1].lower()
    if extensao not in ALLOWED_EXTENSIONS:
        return False, "Envie o documento em PDF, PNG, JPG ou JPEG."
    return True, nome_seguro


def _nome_arquivo_drive(bloco, apartamento, rotulo, filename):
    """Nome auditável no Drive: bloco-apto_rotulo_arquivo.ext."""
    original = os.path.basename(filename or "arquivo")
    return f"{bloco}-{apartamento}_{rotulo}_{original}"


def _slug_drive_cadastro(unidade):
    slug = session.get("cadastro_slug") or session.get("tenant_slug")
    if not slug and unidade is not None and getattr(unidade, "condominio", None):
        slug = unidade.condominio.slug
    return slug or _slug_sessao_ou_prp()


def _upload_documento_drive(arquivo, filename, tenant_slug):
    """
    Envia FileStorage ao Drive. Retorna (resultado_dict|None, erro_validacao|None).
    Falha da API devolve (None, None) para não abortar o cadastro.
    """
    ok, nome_ou_erro = _arquivo_documento_valido(arquivo)
    if not ok:
        if nome_ou_erro:
            return None, nome_ou_erro
        return None, None

    resultado = upload_file_stream(
        arquivo, filename=filename, tenant_slug=tenant_slug
    )
    return resultado, None


def _salvar_foto_ocorrencia(arquivo, prefixo="ocorrencia"):
    """Salva foto do chamado em static/uploads/ocorrencias/."""
    pasta = current_app.config.get("UPLOAD_OCORRENCIAS_FOLDER") or os.path.join(
        current_app.root_path, "static", "uploads", "ocorrencias"
    )
    return _salvar_imagem_upload(arquivo, pasta, prefixo=prefixo)


_FOTO_FACIAL_LADO = 600
_FOTO_FACIAL_MAX_BYTES = 8 * 1024 * 1024


def _agora_foto_facial():
    return datetime.now(ZoneInfo("America/Sao_Paulo")).replace(tzinfo=None)


def _pasta_faciais():
    pasta = current_app.config.get("UPLOAD_FACIAIS_FOLDER") or os.path.join(
        current_app.root_path, "static", "uploads", "faciais"
    )
    os.makedirs(pasta, exist_ok=True)
    return pasta


def _apagar_arquivo_facial(nome):
    base = nome_foto_facial_seguro(nome)
    if not base:
        return
    caminho = os.path.join(_pasta_faciais(), base)
    if os.path.isfile(caminho):
        os.remove(caminho)


def _bytes_para_jpg_facial(conteudo):
    """Converte a imagem para JPG RGB de no máximo 600x600."""
    if not conteudo:
        return None, "Envie uma foto."
    if len(conteudo) > _FOTO_FACIAL_MAX_BYTES:
        return None, "A foto deve ter no máximo 8 MB."
    try:
        from PIL import Image, ImageOps
    except ImportError:
        if conteudo[:3] == b"\xff\xd8\xff":
            return conteudo, None
        return None, "Envie uma imagem JPG, PNG ou WEBP."
    try:
        imagem = ImageOps.exif_transpose(Image.open(io.BytesIO(conteudo)))
        if imagem.mode != "RGB":
            imagem = imagem.convert("RGB")
        imagem.thumbnail((_FOTO_FACIAL_LADO, _FOTO_FACIAL_LADO), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        imagem.save(buffer, format="JPEG", quality=85, optimize=True)
        return buffer.getvalue(), None
    except Exception:
        return None, "Não foi possível ler a imagem. Envie uma foto JPG, PNG ou WEBP."


def _gravar_jpg_facial(conteudo):
    jpg, erro = _bytes_para_jpg_facial(conteudo)
    if erro or not jpg:
        return None, erro or "Foto inválida."
    token = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    nome = f"facial_{token}.jpg"
    with open(os.path.join(_pasta_faciais(), nome), "wb") as arquivo:
        arquivo.write(jpg)
    return nome, None


def _ler_foto_facial_requisicao(arquivo=None, base64_texto=None):
    if arquivo is not None and getattr(arquivo, "filename", None):
        arquivo.stream.seek(0)
        return _gravar_jpg_facial(arquivo.read())
    texto = (base64_texto or "").strip()
    if not texto:
        return None, None
    if "," in texto and texto.lower().startswith("data:"):
        texto = texto.split(",", 1)[1]
    try:
        conteudo = base64.b64decode(texto, validate=False)
    except Exception:
        return None, "A foto capturada é inválida."
    return _gravar_jpg_facial(conteudo)


def sincronizar_credencial_facial(pessoa):
    """Cria ou atualiza a credencial Facial de um morador aprovado com foto."""
    if pessoa is None or not pessoa.eh_morador or not pessoa.id:
        return None
    if pessoa.status != StatusPessoa.APROVADO:
        return None
    foto = nome_foto_facial_seguro(pessoa.foto_facial)
    if not foto:
        return None
    unidade = pessoa.unidade
    condominio_id = unidade.condominio_id if unidade is not None else None
    if not condominio_id:
        return None
    codigo = f"facial-{pessoa.id}-{foto}"[:120]
    existente = (
        CredencialAcesso.query.filter(
            CredencialAcesso.condominio_id == condominio_id,
            CredencialAcesso.morador_id == pessoa.id,
            CredencialAcesso.ativa.is_(True),
            CredencialAcesso.tipo.in_(CredencialAcesso.TIPOS_FACIAL),
        )
        .order_by(CredencialAcesso.id.asc())
        .first()
    )
    if existente is None:
        credencial = CredencialAcesso(
            tipo="Facial",
            codigo_identificador=codigo,
            morador_id=pessoa.id,
            condominio_id=condominio_id,
            ativa=True,
        )
        db.session.add(credencial)
        return credencial
    if existente.codigo_identificador != codigo or existente.tipo != "Facial":
        existente.codigo_identificador = codigo
        existente.tipo = "Facial"
    return existente


def _snapshot_acesso_pessoa(pessoa):
    credenciais = []
    for credencial in list(pessoa.credenciais):
        credenciais.append(
            {
                "tipo": credencial.tipo,
                "codigo_identificador": credencial.codigo_identificador,
                "ativa": credencial.ativa,
                "condominio_id": credencial.condominio_id,
                "data_emissao": credencial.data_emissao,
            }
        )
    return {
        "foto_perfil": pessoa.foto_perfil,
        "foto_facial": pessoa.foto_facial,
        "foto_atualizada_em": pessoa.foto_atualizada_em,
        "credenciais": credenciais,
    }


def _aplicar_fotos_faciais_moradores(pessoas, fotos_novas):
    """Grava fotos enviadas no formulário e sincroniza a credencial se já aprovado."""
    substituidos = []
    for indice, pessoa in enumerate(pessoas):
        if not getattr(pessoa, "eh_morador", False):
            continue
        arquivo = request.files.get(f"morador_{indice}_foto")
        base64_texto = request.form.get(f"morador_{indice}_foto_base64", "")
        tem_envio = bool(arquivo and arquivo.filename) or bool((base64_texto or "").strip())
        if not tem_envio:
            if pessoa.foto_facial and pessoa.status == StatusPessoa.APROVADO:
                sincronizar_credencial_facial(pessoa)
            continue
        nome, erro = _ler_foto_facial_requisicao(arquivo, base64_texto)
        if erro:
            raise ValueError(erro)
        if not nome:
            continue
        fotos_novas.append(nome)
        antigo = pessoa.foto_facial
        pessoa.foto_facial = nome
        pessoa.foto_atualizada_em = _agora_foto_facial()
        sincronizar_credencial_facial(pessoa)
        if antigo and antigo != nome:
            substituidos.append(antigo)
    return substituidos


def _buscar_unidade(bloco, apartamento, condominio_id=None):
    """Busca unidade apenas dentro do tenant. Sem condominio_id, não consulta."""
    if not condominio_id:
        return None
    return Unidade.query.filter_by(
        bloco=bloco,
        apartamento=apartamento,
        condominio_id=condominio_id,
    ).first()


def _unidade_exige_senha(unidade):
    """Conta já reivindicada pede senha. Pré-cadastro segue para o 1º acesso."""
    if not unidade or unidade.eh_setor_interno:
        return False
    if not unidade.conta_reivindicada:
        return False
    if unidade.status == StatusUnidade.REPROVADA:
        return False
    return True


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


def _identificacao_recuperacao(valor):
    """Separa e-mail ou CPF digitados no mesmo campo de confirmação."""
    texto = str(valor or "").strip()
    if "@" in texto:
        return texto.lower(), ""
    digitos = _somente_digitos(texto)
    if len(digitos) == 11:
        return "", digitos
    return "", ""


def _email_destino_recuperacao_unidade(unidade, identificacao):
    """
    E-mail de destino somente se o CPF ou e-mail pertencer à unidade já
    localizada. Não consulta outras unidades.
    """
    if unidade is None:
        return None
    email_informado, cpf_informado = _identificacao_recuperacao(identificacao)
    if not email_informado and not cpf_informado:
        return None

    pessoas = Pessoa.query.filter(Pessoa.unidade_id == unidade.id).all()
    for pessoa in pessoas:
        email_pessoa = (pessoa.email or "").strip()
        if email_informado and email_pessoa.lower() == email_informado:
            return email_pessoa
        if (
            cpf_informado
            and email_pessoa
            and _somente_digitos(pessoa.cpf) == cpf_informado
        ):
            return email_pessoa

    email_dono = (unidade.proprietario_email or "").strip()
    if email_informado and email_dono.lower() == email_informado:
        return email_dono
    if (
        cpf_informado
        and email_dono
        and _somente_digitos(unidade.proprietario_cpf) == cpf_informado
    ):
        return email_dono
    return None


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
    """Códigos de bloco sob jurisdição do síndico (ex.: '1', '2')."""
    return _blocos_codigo_sindico(usuario)


def _blocos_codigo_sindico(usuario):
    """Códigos normalizados dos blocos do síndico (ex.: '1', '6').

    Síndico geral (`blocos_escopo == '*'`) recebe todos os blocos residenciais.
    """
    if not usuario or usuario.role != Role.SINDICO:
        return []
    permitidos = usuario.get_blocos_permitidos()
    if permitidos is None:
        return list(get_blocos())
    return list(permitidos)


def _sindico_gerencia_bloco(usuario, bloco):
    if not usuario or usuario.role != Role.SINDICO or not bloco:
        return False
    return normalizar_bloco_codigo(bloco) in _blocos_codigo_sindico(usuario)


def _label_agrupamentos_sindico(usuario):
    if not usuario or usuario.role != Role.SINDICO:
        return "—"
    permitidos = usuario.get_blocos_permitidos()
    if permitidos is None:
        return "Todos os blocos"
    if not permitidos:
        return "—"
    return ", ".join(f"Bloco {codigo}" for codigo in permitidos)


def _recorte_blocos_consulta(usuario):
    """(opções do filtro, blocos da query, código selecionado ou '')."""
    opcoes = _blocos_codigo_sindico(usuario)
    pedido = normalizar_bloco_codigo((request.args.get("bloco") or "").strip())
    if pedido and pedido in opcoes:
        return opcoes, [pedido], pedido
    return opcoes, list(opcoes), ""


def _registrar_auditoria(usuario, mensagem):
    db.session.add(
        LogAuditoria(
            usuario_id=usuario.id,
            condominio_id=resolver_condominio_id(usuario=usuario),
            mensagem=mensagem,
        )
    )


def _criar_notificacao(
    condominio_id,
    perfil_destino,
    titulo,
    mensagem,
    unidade_id=None,
    link_destino=None,
    tipo="GERAL",
):
    """Enfileira notificação na sessão atual (commit fica a cargo do chamador)."""
    if not condominio_id or perfil_destino not in PerfilDestinoNotificacao.CHOICES:
        return
    if perfil_destino == PerfilDestinoNotificacao.MORADOR and not unidade_id:
        return
    destino = (link_destino or "").strip() or None
    if destino and (not destino.startswith("/") or destino.startswith("//")):
        destino = None
    db.session.add(
        Notificacao(
            condominio_id=condominio_id,
            unidade_id=unidade_id,
            perfil_destino=perfil_destino,
            titulo=(titulo or "")[:120],
            mensagem=mensagem or "",
            lida=False,
            link_destino=destino,
            tipo=(tipo or "GERAL")[:30],
        )
    )


def _rotulo_unidade_ocorrencia(unidade):
    if unidade is None:
        return "Unidade"
    return f"{unidade.bloco} - {unidade.apartamento}"


def _sinalizar_ocorrencia_para_gestao(ocorrencia, nova=True):
    """Avisa admin e os síndicos do bloco. O commit fica com quem chamou."""
    unidade = ocorrencia.unidade
    rotulo = _rotulo_unidade_ocorrencia(unidade)
    if nova:
        titulo = f"🆕 Nova Ocorrência [Unid. {rotulo}]: {ocorrencia.titulo}"
        mensagem = (ocorrencia.descricao or "")[:300]
    else:
        titulo = f"💬 Nova mensagem da Unid. {rotulo} na Ocorrência: {ocorrencia.titulo}"
        mensagem = "O morador enviou uma nova mensagem neste chamado."
    _criar_notificacao(
        ocorrencia.condominio_id,
        PerfilDestinoNotificacao.ADMIN,
        titulo,
        mensagem,
        link_destino=f"/admin/ocorrencias?foco={ocorrencia.id}",
        tipo="OCORRENCIA",
    )
    if unidade is None:
        return
    from app.models import Role, Usuario

    sindicos = Usuario.query.filter_by(
        condominio_id=ocorrencia.condominio_id,
        role=Role.SINDICO,
    ).all()
    bloco_tem_sindico = any(
        _sindico_gerencia_bloco(sindico, unidade.bloco) for sindico in sindicos
    )
    if ocorrencia.competencia == "geral" and not bloco_tem_sindico:
        return
    _criar_notificacao(
        ocorrencia.condominio_id,
        PerfilDestinoNotificacao.SINDICO,
        titulo,
        mensagem,
        unidade_id=unidade.id,
        link_destino=f"/sindico/ocorrencias?foco={ocorrencia.id}",
        tipo="OCORRENCIA",
    )


def _sinalizar_ocorrencia_para_morador(ocorrencia, mensagem):
    _criar_notificacao(
        ocorrencia.condominio_id,
        PerfilDestinoNotificacao.MORADOR,
        "Atualização da sua ocorrência",
        mensagem,
        unidade_id=ocorrencia.unidade_id,
        link_destino="/morador/ocorrencias",
        tipo="OCORRENCIA",
    )


def _marcar_ocorrencia_gestao(ocorrencia):
    ocorrencia.lida_pela_gestao = True
    ocorrencia.ultima_interacao_por = "GESTAO"
    ocorrencia.ultima_interacao_em = datetime.utcnow()


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
    if usuario and usuario.role == Role.SINDICO and usuario.condominio_id:
        return (PerfilDestinoNotificacao.SINDICO, usuario.condominio_id, None)
    if usuario and usuario.role == Role.ADMIN and usuario.condominio_id:
        return (PerfilDestinoNotificacao.ADMIN, usuario.condominio_id, None)
    if usuario and usuario.role in (Role.PORTEIRO, Role.SUPERADMIN):
        condominio_id = _condominio_id_portaria(usuario)
        if condominio_id:
            return (PerfilDestinoNotificacao.PORTARIA, condominio_id, None)
    return None, None, None


def _query_notificacoes(perfil, condominio_id, unidade_id, usuario=None):
    if perfil == PerfilDestinoNotificacao.ADMIN:
        query = Notificacao.query.filter(
            Notificacao.condominio_id == condominio_id,
            Notificacao.unidade_id.is_(None),
            Notificacao.perfil_destino.in_(
                (PerfilDestinoNotificacao.ADMIN, PerfilDestinoNotificacao.PORTARIA)
            ),
        )
    elif perfil == PerfilDestinoNotificacao.SINDICO:
        blocos = _blocos_codigo_sindico(usuario) if usuario is not None else []
        query = Notificacao.query.join(Unidade, Notificacao.unidade_id == Unidade.id).filter(
            Notificacao.condominio_id == condominio_id,
            Notificacao.perfil_destino == PerfilDestinoNotificacao.SINDICO,
            Unidade.condominio_id == condominio_id,
            Unidade.bloco.in_(blocos or [""]),
        )
    else:
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


_ALIASES_VINCULO_MORADOR = {
    "proprietário": VinculoPessoa.PROPRIETARIO,
    "proprietario": VinculoPessoa.PROPRIETARIO,
    "inquilino": VinculoPessoa.LOCATARIO,
    "locatário": VinculoPessoa.LOCATARIO,
    "locatario": VinculoPessoa.LOCATARIO,
    "familiar/morador": VinculoPessoa.MORADOR,
    "familiar": VinculoPessoa.MORADOR,
    "morador": VinculoPessoa.MORADOR,
}


def _canonicalizar_vinculo_morador(valor):
    texto = (valor or "").strip()
    if texto in VinculoPessoa.CHOICES:
        return texto
    return _ALIASES_VINCULO_MORADOR.get(texto.lower())


def _parse_pessoas_form(form, prefixo="pessoa", aceitar_aliases_vinculo=False):
    pessoas = []
    indice = 0
    while True:
        nome = form.get(f"{prefixo}_{indice}_nome", "").strip()
        if not nome:
            break

        vinculo_bruto = form.get(f"{prefixo}_{indice}_vinculo", "").strip()
        if aceitar_aliases_vinculo:
            vinculo = _canonicalizar_vinculo_morador(vinculo_bruto)
        else:
            vinculo = vinculo_bruto if vinculo_bruto in VinculoPessoa.CHOICES else None
        if not vinculo:
            raise ValueError(f"Vínculo inválido para {nome}.")

        data_nascimento = _parse_data(
            form.get(f"{prefixo}_{indice}_data_nascimento", "")
        )
        idade = _calcular_idade(data_nascimento) if data_nascimento else None
        is_menor = idade is not None and idade < 18
        is_responsavel = form.get(f"{prefixo}_{indice}_is_responsavel") == "on"

        cpf = form.get(f"{prefixo}_{indice}_cpf", "").strip()
        telefone = form.get(f"{prefixo}_{indice}_telefone", "").strip()
        email = form.get(f"{prefixo}_{indice}_email", "").strip()
        autoriza_interfone_raw = (
            form.get(f"{prefixo}_{indice}_autoriza_interfone", "")
            or form.get(f"{prefixo}_{indice}_lgpd_interfone", "")
        ).strip().lower()
        autoriza_interfone = autoriza_interfone_raw in ("true", "on", "1", "sim")

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
        pessoa_id_raw = form.get(f"{prefixo}_{indice}_id", "").strip()
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
                "parentesco": form.get(f"{prefixo}_{indice}_parentesco", "").strip()
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


def _parse_moradores_form(form):
    return _parse_pessoas_form(
        form, prefixo="morador", aceitar_aliases_vinculo=True
    )


def _parse_proprietarios_lista_form(form):
    """Donos legais enviados pelo formulário novo (proprietario_0_nome, ...)."""
    proprietarios = []
    indice = 0
    while True:
        nome = form.get(f"proprietario_{indice}_nome", "").strip()
        if not nome:
            break

        cpf = form.get(f"proprietario_{indice}_cpf", "").strip()
        if not _somente_digitos(cpf):
            raise ValueError(f"CPF é obrigatório para o proprietário {nome}.")

        pessoa_id = None
        pessoa_id_raw = form.get(f"proprietario_{indice}_id", "").strip()
        if pessoa_id_raw:
            try:
                pessoa_id = int(pessoa_id_raw)
            except ValueError:
                raise ValueError(f"Identificador inválido para o proprietário {nome}.")

        email = form.get(f"proprietario_{indice}_email", "").strip()
        proprietarios.append(
            {
                "id": pessoa_id,
                "nome_completo": nome,
                "cpf": cpf,
                "telefone": form.get(f"proprietario_{indice}_telefone", "").strip(),
                "email": email or None,
                "data_nascimento": _parse_data(
                    form.get(f"proprietario_{indice}_data_nascimento", "")
                ),
            }
        )
        indice += 1

    return proprietarios


_RE_LISTAS_SEPARADAS = re.compile(r"^(proprietario|morador)_\d+_nome$")


def _formulario_usa_listas_separadas(form):
    return any(_RE_LISTAS_SEPARADAS.match(chave) for chave in form.keys())


def _preencher_dados_basicos(destino, origem):
    for campo in ("nome_completo", "telefone", "email", "data_nascimento"):
        valor = origem.get(campo)
        if valor not in (None, ""):
            destino[campo] = valor


def _sincronizar_dados_basicos_por_cpf(proprietarios, moradores):
    """
    Quando o mesmo CPF está nas duas listas da unidade, nome, telefone,
    e-mail e data de nascimento ficam iguais nos dois lados.
    O lado do morador prevalece se os dois trouxerem valor.
    """
    por_cpf = {}
    for item in proprietarios:
        cpf = _somente_digitos(item.get("cpf"))
        if not cpf:
            continue
        por_cpf.setdefault(cpf, {"proprietarios": [], "moradores": []})
        por_cpf[cpf]["proprietarios"].append(item)
    for item in moradores:
        cpf = _somente_digitos(item.get("cpf"))
        if not cpf:
            continue
        por_cpf.setdefault(cpf, {"proprietarios": [], "moradores": []})
        por_cpf[cpf]["moradores"].append(item)

    for grupos in por_cpf.values():
        if not grupos["proprietarios"] or not grupos["moradores"]:
            continue
        base = {
            "nome_completo": "",
            "telefone": "",
            "email": None,
            "data_nascimento": None,
        }
        for item in grupos["proprietarios"]:
            _preencher_dados_basicos(base, item)
        for item in grupos["moradores"]:
            _preencher_dados_basicos(base, item)
        for item in grupos["proprietarios"] + grupos["moradores"]:
            item["nome_completo"] = base["nome_completo"]
            item["telefone"] = base["telefone"] or ""
            item["email"] = base["email"]
            item["data_nascimento"] = base["data_nascimento"]


def _cpfs_proprietarios_informados(proprietarios):
    return {
        _somente_digitos(item.get("cpf"))
        for item in proprietarios
        if _somente_digitos(item.get("cpf"))
    }


def _ajustar_vinculo_morador_sem_dono(proprietarios, moradores):
    """
    Vínculo Proprietário no morador só permanece se o CPF dele foi
    enviado na lista de proprietários desta mesma submissão.
    """
    cpfs_donos = _cpfs_proprietarios_informados(proprietarios)
    for morador in moradores:
        if morador.get("vinculo") != VinculoPessoa.PROPRIETARIO:
            continue
        cpf = _somente_digitos(morador.get("cpf"))
        if cpf not in cpfs_donos:
            morador["vinculo"] = VinculoPessoa.MORADOR


def _unir_proprietarios_e_moradores(proprietarios, moradores):
    """Uma linha de Pessoa por CPF, com as flags dos papéis preenchidos."""
    _ajustar_vinculo_morador_sem_dono(proprietarios, moradores)
    _sincronizar_dados_basicos_por_cpf(proprietarios, moradores)
    pessoas = []
    indice_por_cpf = {}

    for morador in moradores:
        item = {
            "id": morador.get("id"),
            "nome_completo": morador["nome_completo"],
            "cpf": morador.get("cpf") or "",
            "vinculo": morador["vinculo"],
            "telefone": morador.get("telefone") or "",
            "email": morador.get("email"),
            "parentesco": morador.get("parentesco"),
            "data_nascimento": morador.get("data_nascimento"),
            "is_responsavel": bool(morador.get("is_responsavel")),
            "autoriza_interfone": bool(morador.get("autoriza_interfone")),
            "eh_proprietario": False,
            "eh_morador": True,
        }
        pessoas.append(item)
        cpf = _somente_digitos(item["cpf"])
        if cpf:
            indice_por_cpf[cpf] = len(pessoas) - 1

    cpfs_somente_dono = set()
    for proprietario in proprietarios:
        cpf = _somente_digitos(proprietario.get("cpf"))
        if cpf and cpf in indice_por_cpf:
            pessoas[indice_por_cpf[cpf]]["eh_proprietario"] = True
            continue
        if cpf and cpf in cpfs_somente_dono:
            continue
        if cpf:
            cpfs_somente_dono.add(cpf)
        pessoas.append(
            {
                "id": proprietario.get("id"),
                "nome_completo": proprietario["nome_completo"],
                "cpf": proprietario.get("cpf") or "",
                "vinculo": VinculoPessoa.PROPRIETARIO,
                "telefone": proprietario.get("telefone") or "",
                "email": proprietario.get("email"),
                "parentesco": None,
                "data_nascimento": proprietario.get("data_nascimento"),
                "is_responsavel": False,
                "autoriza_interfone": False,
                "eh_proprietario": True,
                "eh_morador": False,
            }
        )
    return pessoas


def _aplicar_papeis_formulario_legado(pessoas_data, unidade=None):
    """
    Formulário atual: vínculo Proprietário ocupa e também é dono.
    Quem já é dono com outro vínculo (as duas listas) permanece dono.
    """
    atuais_por_id = {}
    if unidade is not None:
        atuais_por_id = {pessoa.id: pessoa for pessoa in unidade.pessoas.all()}
    for pessoa in pessoas_data:
        pessoa["eh_morador"] = True
        if pessoa.get("vinculo") == VinculoPessoa.PROPRIETARIO:
            pessoa["eh_proprietario"] = True
            continue
        atual = atuais_por_id.get(pessoa.get("id"))
        pessoa["eh_proprietario"] = bool(
            atual
            and atual.eh_proprietario
            and atual.vinculo != VinculoPessoa.PROPRIETARIO
        )


def _pessoa_somente_proprietario(pessoa):
    return bool(pessoa.eh_proprietario) and not bool(pessoa.eh_morador)


def _dados_pessoa_persistida(pessoa):
    return {
        "id": pessoa.id,
        "nome_completo": pessoa.nome_completo,
        "cpf": pessoa.cpf or "",
        "vinculo": pessoa.vinculo,
        "telefone": pessoa.telefone or "",
        "email": pessoa.email,
        "parentesco": pessoa.parentesco,
        "data_nascimento": pessoa.data_nascimento,
        "is_responsavel": bool(pessoa.is_responsavel),
        "autoriza_interfone": bool(pessoa.autoriza_interfone),
        "eh_proprietario": bool(pessoa.eh_proprietario),
        "eh_morador": bool(pessoa.eh_morador),
    }


def _anexar_proprietarios_nao_ocupantes(unidade, pessoas_data):
    """O formulário atual não lista donos que não moram; o save não pode apagá-los."""
    ids = {pessoa.get("id") for pessoa in pessoas_data if pessoa.get("id")}
    cpfs = {
        _somente_digitos(pessoa.get("cpf"))
        for pessoa in pessoas_data
        if _somente_digitos(pessoa.get("cpf"))
    }
    for pessoa in unidade.pessoas.all():
        if not _pessoa_somente_proprietario(pessoa):
            continue
        cpf = _somente_digitos(pessoa.cpf)
        if pessoa.id in ids or (cpf and cpf in cpfs):
            continue
        pessoas_data.append(_dados_pessoa_persistida(pessoa))
    return pessoas_data


def _legado_proprietario_desde_listas(proprietarios, moradores):
    cpfs_moradores = {
        _somente_digitos(morador.get("cpf"))
        for morador in moradores
        if _somente_digitos(morador.get("cpf"))
    }
    externos = [
        item
        for item in proprietarios
        if _somente_digitos(item.get("cpf")) not in cpfs_moradores
    ]
    escolhido = (externos or proprietarios or [None])[0]
    if not escolhido:
        return {
            "proprietario_nome": None,
            "proprietario_telefone": None,
            "proprietario_email": None,
            "proprietario_cpf": None,
        }
    return {
        "proprietario_nome": escolhido.get("nome_completo"),
        "proprietario_telefone": escolhido.get("telefone"),
        "proprietario_email": escolhido.get("email"),
        "proprietario_cpf": escolhido.get("cpf"),
    }


def _registro_autocomplete(nome, cpf, telefone, email, data_nascimento, origem):
    if isinstance(data_nascimento, date):
        nascimento = data_nascimento.isoformat()
    else:
        nascimento = (data_nascimento or "") if data_nascimento else ""
    return {
        "nome": nome or "",
        "cpf": cpf or "",
        "telefone": telefone or "",
        "email": email or "",
        "data_nascimento": nascimento,
        "origem": origem,
    }


def _listas_proprietarios_moradores(unidade):
    pessoas = unidade.pessoas.order_by(Pessoa.id).all()
    moradores = [
        pessoa
        for pessoa in pessoas
        if pessoa.eh_morador or not pessoa.eh_proprietario
    ]
    proprietarios = [pessoa for pessoa in pessoas if pessoa.eh_proprietario]
    cpf_legado = _somente_digitos(unidade.proprietario_cpf)
    nome_legado = (unidade.proprietario_nome or "").strip()
    if nome_legado or cpf_legado:
        ja_listado = False
        for pessoa in proprietarios:
            mesmo_cpf = cpf_legado and _somente_digitos(pessoa.cpf) == cpf_legado
            mesmo_nome = (
                not cpf_legado
                and _normalizar_texto_comparacao(pessoa.nome_completo)
                == _normalizar_texto_comparacao(nome_legado)
            )
            if mesmo_cpf or mesmo_nome:
                ja_listado = True
                break
        if not ja_listado:
            proprietarios.append(
                SimpleNamespace(
                    id=None,
                    nome_completo=nome_legado,
                    cpf=unidade.proprietario_cpf or "",
                    telefone=unidade.proprietario_telefone or "",
                    email=unidade.proprietario_email,
                    data_nascimento=None,
                    vinculo=VinculoPessoa.PROPRIETARIO,
                    parentesco=None,
                    is_responsavel=False,
                    autoriza_interfone=False,
                    eh_proprietario=True,
                    eh_morador=False,
                )
            )
    return proprietarios, moradores


def _sugestoes_pessoas_unidade(unidade, termo):
    termo_limpo = (termo or "").strip()
    termo_nome = termo_limpo.casefold()
    digitos = _somente_digitos(termo_limpo)
    vistos = set()
    sugestoes = []

    def _incluir(nome, cpf, telefone, email, nascimento, origem):
        cpf_digitos = _somente_digitos(cpf)
        chave = f"cpf:{cpf_digitos}" if cpf_digitos else f"nome:{_normalizar_texto_comparacao(nome)}"
        if not chave.strip(":") or chave in vistos:
            return
        if termo_limpo:
            nome_ok = termo_nome in (nome or "").casefold()
            cpf_ok = bool(digitos) and digitos in cpf_digitos
            if not nome_ok and not cpf_ok:
                return
        vistos.add(chave)
        sugestoes.append(
            _registro_autocomplete(nome, cpf, telefone, email, nascimento, origem)
        )

    for pessoa in unidade.pessoas.order_by(Pessoa.nome_completo).all():
        if pessoa.eh_proprietario:
            origem = "Proprietário"
        elif pessoa.eh_morador or not pessoa.eh_proprietario:
            origem = "Morador"
        else:
            continue
        _incluir(
            pessoa.nome_completo,
            pessoa.cpf,
            pessoa.telefone,
            pessoa.email,
            pessoa.data_nascimento,
            origem,
        )

    if unidade.proprietario_nome or unidade.proprietario_cpf:
        _incluir(
            unidade.proprietario_nome,
            unidade.proprietario_cpf,
            unidade.proprietario_telefone,
            unidade.proprietario_email,
            None,
            "Proprietário",
        )

    cpfs_donos = {
        _somente_digitos(pessoa.cpf)
        for pessoa in unidade.pessoas.all()
        if pessoa.eh_proprietario and _somente_digitos(pessoa.cpf)
    }
    cpf_legado = _somente_digitos(unidade.proprietario_cpf)
    if cpf_legado:
        cpfs_donos.add(cpf_legado)
    for item in sugestoes:
        if _somente_digitos(item["cpf"]) in cpfs_donos:
            item["origem"] = "Proprietário"
    return sugestoes


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


def _houve_add_remove_pessoas(unidade, pessoas_data, pessoas_atuais=None):
    if pessoas_atuais is None:
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


def _requer_nova_aprovacao_sindico(
    unidade, pessoas_data, veiculos_data, dados_proprietario, pessoas_atuais=None
):
    if _houve_add_remove_pessoas(
        unidade, pessoas_data, pessoas_atuais=pessoas_atuais
    ):
        return True
    if _houve_add_remove_veiculos(unidade, veiculos_data):
        return True
    if _houve_mudanca_proprietario_ou_responsavel(
        unidade, pessoas_data, dados_proprietario
    ):
        return True
    return False


def _salvar_pessoas_veiculos(unidade, pessoas_data, veiculos_data, *, modo_atualizacao=False):
    try:
        status_por_id = {}
        preservados = {}
        if modo_atualizacao:
            for pessoa in unidade.pessoas.all():
                status_por_id[pessoa.id] = (
                    pessoa.status or StatusPessoa.APROVADO
                )
                preservados[pessoa.id] = _snapshot_acesso_pessoa(pessoa)

        for pessoa in list(unidade.pessoas.all()):
            db.session.delete(pessoa)
        for veiculo in unidade.veiculos.all():
            db.session.delete(veiculo)

        criadas = []
        credenciais_pendentes = []
        for dados in pessoas_data:
            campos_pessoa = {k: v for k, v in dados.items() if k != "id"}
            if "eh_morador" not in campos_pessoa:
                campos_pessoa["eh_morador"] = True
            if "eh_proprietario" not in campos_pessoa:
                campos_pessoa["eh_proprietario"] = (
                    campos_pessoa.get("vinculo") == VinculoPessoa.PROPRIETARIO
                )
            pessoa_id = dados.get("id")
            if modo_atualizacao and pessoa_id and pessoa_id in status_por_id:
                campos_pessoa["status"] = status_por_id[pessoa_id]
            else:
                campos_pessoa["status"] = StatusPessoa.PENDENTE
            pessoa = Pessoa(unidade_id=unidade.id, **campos_pessoa)
            snap = preservados.get(pessoa_id) if pessoa_id else None
            if snap:
                pessoa.foto_perfil = snap["foto_perfil"]
                pessoa.foto_facial = snap["foto_facial"]
                pessoa.foto_atualizada_em = snap["foto_atualizada_em"]
                credenciais_pendentes.append((pessoa, snap["credenciais"]))
            db.session.add(pessoa)
            criadas.append(pessoa)

        for dados in veiculos_data:
            db.session.add(Veiculo(unidade_id=unidade.id, **dados))
        db.session.flush()
        for pessoa, credenciais in credenciais_pendentes:
            for credencial in credenciais:
                db.session.add(
                    CredencialAcesso(morador_id=pessoa.id, **credencial)
                )
        return criadas
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

    if request.method == "GET":
        return redirect(url_for("tenant_login", slug=condominio.slug))

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
    from app.planta_unidades import MENSAGEM_FORA_DA_PLANTA, MENSAGEM_TRAVA

    if (
        not unidade
        or unidade.eh_setor_interno
        or not unidade.criada_pela_admin
    ):
        flash(MENSAGEM_FORA_DA_PLANTA, "danger")
        return _render_tenant_login(
            condominio,
            active_tab="morador",
            bloco=bloco,
            apartamento=apartamento,
        )

    if not unidade.conta_reivindicada:
        session["cadastro_bloco"] = bloco
        session["cadastro_apartamento"] = apartamento
        session["cadastro_condominio_id"] = condominio.id
        session["cadastro_slug"] = condominio.slug
        return redirect(url_for("cadastro_inicial", slug=condominio.slug))

    if unidade.status == StatusUnidade.REPROVADA:
        flash(MENSAGEM_TRAVA, "warning")
        return _render_tenant_login(
            condominio,
            active_tab="morador",
            bloco=bloco,
            apartamento=apartamento,
        )

    senha = request.form.get("senha", "").strip()
    exige_senha = _unidade_exige_senha(unidade)

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
    return redirect(url_for("morador_inicio"))


def status_unidade(slug):
    """JSON para o login do morador revalidar cadastro ao trocar bloco/apto."""
    condominio, bloqueio = _carregar_condominio_entrada(slug)
    if bloqueio is not None:
        resposta = jsonify({"ok": False, "cadastrada": False, "exige_senha": False})
        resposta.status_code = 403
        resposta.headers["Cache-Control"] = "no-store"
        return resposta

    bloco, apartamento = normalizar_bloco_apartamento(
        request.args.get("bloco", ""),
        request.args.get("apartamento", ""),
    )
    if not validar_unidade(bloco, apartamento):
        resposta = jsonify({"ok": False, "cadastrada": False, "exige_senha": False})
        resposta.headers["Cache-Control"] = "no-store"
        return resposta

    unidade = _buscar_unidade(bloco, apartamento, condominio_id=condominio.id)
    na_planta = bool(
        unidade and not unidade.eh_setor_interno and unidade.criada_pela_admin
    )
    exige_senha = _unidade_exige_senha(unidade)
    primeiro_acesso = bool(na_planta and not unidade.conta_reivindicada)
    resposta = jsonify(
        {
            "ok": True,
            "cadastrada": exige_senha,
            "exige_senha": exige_senha,
            "primeiro_acesso": primeiro_acesso,
            "na_planta": na_planta,
        }
    )
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


_MENSAGEM_RECUPERACAO_UNIDADE = (
    "Se os dados informados estiverem corretos e vinculados a esta unidade, "
    "as instruções de recuperação foram enviadas."
)


def _unidade_do_token_redefinicao(email, condominio_id, unidade_id):
    """Unidade do token, só se o e-mail ainda pertencer a ela."""
    if not email or not condominio_id or not unidade_id:
        return None
    try:
        unidade_id = int(unidade_id)
        condominio_id = int(condominio_id)
    except (TypeError, ValueError):
        return None
    unidade = Unidade.query.filter_by(
        id=unidade_id,
        condominio_id=condominio_id,
    ).first()
    if not unidade:
        return None
    if not _email_destino_recuperacao_unidade(unidade, email):
        return None
    return unidade


def _render_esqueci_senha(bloco="", apartamento=""):
    bloco, apartamento = normalizar_bloco_apartamento(bloco, apartamento)
    if not validar_unidade(bloco, apartamento):
        bloco, apartamento = "", ""
    return render_template(
        "esqueci_senha.html",
        slug=_slug_sessao_ou_prp(),
        condominio_estrutura=get_condominio_estrutura(),
        bloco=bloco,
        apartamento=apartamento,
    )


def esqueci_senha():
    if request.method == "POST":
        bloco, apartamento = normalizar_bloco_apartamento(
            request.form.get("bloco", ""),
            request.form.get("apartamento", ""),
        )
        identificacao = request.form.get("identificacao", "")
        condominio_id_solicitacao = _condominio_id_da_sessao()
        unidade = None
        if condominio_id_solicitacao and validar_unidade(bloco, apartamento):
            unidade = _buscar_unidade(
                bloco,
                apartamento,
                condominio_id=condominio_id_solicitacao,
            )
        email_destino = _email_destino_recuperacao_unidade(unidade, identificacao)
        if unidade and email_destino:
            try:
                token = gerar_token_redefinicao(
                    email_destino,
                    SALT_RECUPERACAO_MORADOR,
                    condominio_id=condominio_id_solicitacao,
                    unidade_id=unidade.id,
                )
                link = url_for("redefinir_senha", token=token, _external=True)
                enviar_email_redefinicao_senha(
                    email_destino,
                    link,
                    perfil="morador",
                    bloco=unidade.bloco,
                    apartamento=unidade.apartamento,
                )
            except Exception:
                traceback.print_exc()
                flash(
                    "Não foi possível enviar o e-mail. Tente novamente mais tarde.",
                    "danger",
                )
                return redirect(
                    url_for("esqueci_senha", bloco=bloco, apartamento=apartamento)
                )

        flash(_MENSAGEM_RECUPERACAO_UNIDADE, "info")
        return redirect(url_for("tenant_login", slug=_slug_sessao_ou_prp()))

    return _render_esqueci_senha(
        request.args.get("bloco", ""),
        request.args.get("apartamento", ""),
    )


def _render_redefinir_senha(token, unidade):
    return render_template(
        "redefinir_senha.html",
        token=token,
        unidade=unidade,
    )


def redefinir_senha(token):
    email, condominio_id_token, emitido_em, unidade_id = verificar_token_redefinicao(
        token, SALT_RECUPERACAO_MORADOR
    )
    unidade = _unidade_do_token_redefinicao(
        email, condominio_id_token, unidade_id
    )
    if not unidade:
        flash(
            "Link inválido ou expirado. Solicite uma nova redefinição de senha.",
            "danger",
        )
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
            return _render_redefinir_senha(token, unidade)
        if senha != confirmacao:
            flash("As senhas não coincidem.", "danger")
            return _render_redefinir_senha(token, unidade)

        unidade_alvo = Unidade.query.filter_by(
            id=unidade.id,
            condominio_id=unidade.condominio_id,
        ).first()
        if not unidade_alvo:
            flash(
                "Link inválido ou expirado. Solicite uma nova redefinição de senha.",
                "danger",
            )
            return redirect(url_for("esqueci_senha"))
        unidade_alvo.set_password(senha)
        db.session.commit()
        flash(
            "Senha redefinida com sucesso. Acesse com bloco, apartamento e a nova senha.",
            "success",
        )
        slug = "prp"
        if unidade_alvo.condominio and unidade_alvo.condominio.slug:
            slug = unidade_alvo.condominio.slug
        return redirect(url_for("tenant_login", slug=slug))

    return _render_redefinir_senha(token, unidade)


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

    from app.planta_unidades import (
        MENSAGEM_FORA_DA_PLANTA,
        MENSAGEM_TRAVA,
        documento_pre_autorizado,
        mascarar_documento,
        mascarar_email,
        mascarar_nome,
        mascarar_telefone,
    )

    unidade = _buscar_unidade(bloco, apartamento, condominio_id=condominio.id)
    if not unidade or unidade.eh_setor_interno or not unidade.criada_pela_admin:
        flash(MENSAGEM_FORA_DA_PLANTA, "danger")
        return redirect(url_for("tenant_login", slug=condominio.slug))
    if unidade.conta_reivindicada:
        flash(MENSAGEM_TRAVA, "warning")
        return redirect(url_for("tenant_login", slug=condominio.slug))

    documento = documento_pre_autorizado(unidade)
    return render_template(
        "cadastro_morador.html",
        bloco=bloco,
        apartamento=apartamento,
        modo="cadastro",
        vinculos=VinculoPessoa.CHOICES,
        condominio=condominio,
        slug=condominio.slug,
        pre_cadastro=True,
        exige_cpf=bool(documento),
        mascara={
            "nome": mascarar_nome(unidade.proprietario_nome),
            "documento": mascarar_documento(documento),
            "email": mascarar_email(unidade.proprietario_email),
            "telefone": mascarar_telefone(unidade.proprietario_telefone),
        },
    )


@unidade_required
def morador_inicio(unidade):
    """Painel do dia a dia do morador."""
    configuracao = (
        unidade.condominio.configuracao if unidade.condominio else None
    )
    label_bloco = (
        configuracao.label_agrupamento
        if configuracao and configuracao.label_agrupamento
        else "Bloco"
    )
    label_apto = (
        configuracao.label_unidade
        if configuracao and configuracao.label_unidade
        else "Apto"
    )
    responsavel = (
        unidade.pessoas.filter_by(is_responsavel=True).first()
        or unidade.pessoas.first()
    )
    docs_incompletos = unidade.documento_status in (
        StatusDocumento.PENDENTE,
        StatusDocumento.NAO_ENVIADO,
        StatusDocumento.REJEITADO,
    )
    contrato_incompleto = unidade.contrato_locacao_status in (
        StatusDocumento.PENDENTE,
        StatusDocumento.NAO_ENVIADO,
        StatusDocumento.REJEITADO,
    )
    cadastro_pendente = (
        unidade.status == StatusUnidade.PENDENTE
        or unidade.atualizacao_pendente
        or docs_incompletos
        or contrato_incompleto
    )
    if unidade.documento_status == StatusDocumento.REJEITADO or (
        unidade.contrato_locacao_status == StatusDocumento.REJEITADO
    ):
        alerta_cadastro = (
            "A documentação do seu cadastro foi recusada. "
            "Envie os arquivos novamente para concluir a análise."
        )
    elif unidade.atualizacao_pendente:
        alerta_cadastro = (
            "Seus dados atualizados estão aguardando a aprovação do síndico."
        )
    else:
        alerta_cadastro = (
            "Seu cadastro ainda tem informações ou documentos pendentes."
        )
    encomendas_aguardando = 0
    if unidade.condominio_id:
        encomendas_aguardando = Encomenda.query.filter_by(
            unidade_id=unidade.id,
            condominio_id=unidade.condominio_id,
            status=StatusEncomenda.PENDENTE,
        ).count()

    return render_template(
        "morador/inicio.html",
        unidade=unidade,
        responsavel=responsavel,
        label_bloco=label_bloco,
        label_apto=label_apto,
        cadastro_pendente=cadastro_pendente,
        alerta_cadastro=alerta_cadastro,
        encomendas_aguardando=encomendas_aguardando,
    )


@unidade_required
def atualizar_foto_facial(unidade, pessoa_id):
    """Atualiza só a foto facial do morador, sem reenviar o cadastro."""
    pessoa = Pessoa.query.filter_by(
        id=pessoa_id,
        unidade_id=unidade.id,
        eh_morador=True,
    ).first()
    if pessoa is None:
        return jsonify({"ok": False, "erro": "Morador não encontrado."}), 404
    nome, erro = _ler_foto_facial_requisicao(
        request.files.get("foto"),
        request.form.get("foto_base64", ""),
    )
    if erro:
        return jsonify({"ok": False, "erro": erro}), 400
    if not nome:
        return jsonify({"ok": False, "erro": "Envie uma foto."}), 400
    antigo = pessoa.foto_facial
    pessoa.foto_facial = nome
    pessoa.foto_atualizada_em = _agora_foto_facial()
    sincronizar_credencial_facial(pessoa)
    db.session.commit()
    if antigo and antigo != nome:
        _apagar_arquivo_facial(antigo)
    return jsonify(
        {
            "ok": True,
            "foto_url": url_for("static", filename=f"uploads/faciais/{nome}"),
            "facial_cadastrada": True,
        }
    )


@unidade_required
def atualizar_dados(unidade):
    if unidade.status not in (StatusUnidade.APROVADA, StatusUnidade.REGISTRADA):
        flash("Esta unidade não pode ser atualizada no momento.", "warning")
        return redirect(url_for("tenant_login", slug=_slug_sessao_ou_prp()))

    if request.method == "POST":
        return salvar_cadastro()

    pessoas = [
        pessoa
        for pessoa in unidade.pessoas.all()
        if not _pessoa_somente_proprietario(pessoa)
    ]
    veiculos = unidade.veiculos.all()
    proprietarios, moradores = _listas_proprietarios_moradores(unidade)

    return render_template(
        "cadastro_morador.html",
        bloco=unidade.bloco,
        apartamento=unidade.apartamento,
        modo="atualizacao",
        vinculos=VinculoPessoa.CHOICES,
        pessoas=pessoas,
        proprietarios=proprietarios,
        moradores=moradores,
        veiculos=veiculos,
        unidade=unidade,
        slug=_slug_sessao_ou_prp(),
    )


@unidade_required
def api_unidade_pessoas_autocomplete(unidade):
    termo = request.args.get("q", "")
    return jsonify(_sugestoes_pessoas_unidade(unidade, termo))


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


def _parceiro_visivel_no_condominio(parceiro, condominio_id):
    """Parceiro sem vínculo continua visível em todos os condomínios.

    Com pelo menos um condomínio marcado, a oferta fica restrita a eles.
    """
    if not parceiro or parceiro.status != "Ativo":
        return False
    vinculados = list(parceiro.condominios)
    if not vinculados:
        return True
    return any(condominio.id == condominio_id for condominio in vinculados)


@unidade_required
def clube_vantagens(unidade):
    if unidade.eh_setor_interno:
        flash("Setores internos não participam do Clube de Vantagens.", "warning")
        return redirect(url_for("morador_inicio"))
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
        if not _parceiro_visivel_no_condominio(cupom.parceiro, unidade.condominio_id):
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

    parceiros_ativos = [
        parceiro
        for parceiro in Parceiro.query.filter_by(status="Ativo")
        .order_by(Parceiro.nome_empresa)
        .all()
        if _parceiro_visivel_no_condominio(parceiro, unidade.condominio_id)
    ]
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
def morador_clube_vantagens(unidade):
    """Vitrine do morador: parceiros globais ou vinculados ao condomínio dele."""
    if unidade.eh_setor_interno:
        flash("Setores internos não participam do Clube de Vantagens.", "warning")
        return redirect(url_for("morador_inicio"))
    candidatos = (
        Parceiro.query.options(selectinload(Parceiro.produtos))
        .filter_by(ativo=True)
        .order_by(Parceiro.nome_empresa)
        .all()
    )
    parceiros_visiveis = []
    for parceiro in candidatos:
        if parceiro.status != "Ativo":
            continue
        vinculados = list(parceiro.condominios)
        if not vinculados or any(
            condominio.id == unidade.condominio_id for condominio in vinculados
        ):
            parceiros_visiveis.append(parceiro)

    categorias_ids = {
        parceiro.categoria_id
        for parceiro in parceiros_visiveis
        if parceiro.categoria_id
    }
    categorias = []
    if categorias_ids:
        categorias = (
            CategoriaParceiro.query.filter(
                CategoriaParceiro.id.in_(categorias_ids),
                CategoriaParceiro.ativa.is_(True),
            )
            .order_by(CategoriaParceiro.nome)
            .all()
        )

    moradores_ativos = (
        unidade.pessoas.filter(Pessoa.eh_morador.is_(True))
        .order_by(Pessoa.nome_completo)
        .all()
    )
    titular = next(
        (pessoa for pessoa in moradores_ativos if pessoa.is_responsavel),
        None,
    )
    if titular is None and moradores_ativos:
        titular = moradores_ativos[0]
    dependentes = [
        pessoa
        for pessoa in moradores_ativos
        if titular is None or pessoa.id != titular.id
    ]

    parceiro_ids = [parceiro.id for parceiro in parceiros_visiveis]
    ofertas_por_parceiro = {parceiro_id: [] for parceiro_id in parceiro_ids}
    if parceiro_ids:
        data_atual = datetime.utcnow().date()
        cupons_ativos = (
            Cupom.query.filter(
                Cupom.parceiro_id.in_(parceiro_ids),
                Cupom.ativo.is_(True),
                or_(Cupom.data_validade.is_(None), Cupom.data_validade >= data_atual),
            )
            .order_by(Cupom.titulo)
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
        for cupom in cupons_ativos:
            total_resgates = resgates_por_cupom.get(cupom.id, 0)
            if cupom.limite_total is not None and total_resgates >= cupom.limite_total:
                continue
            if resgates_unidade_por_cupom.get(cupom.id, 0) >= cupom.limite_por_unidade:
                continue
            ofertas_por_parceiro.setdefault(cupom.parceiro_id, []).append(cupom)

    cupons_disponiveis = [
        cupom
        for lista in ofertas_por_parceiro.values()
        for cupom in lista
    ]
    cupons_disponiveis.sort(
        key=lambda cupom: (cupom.parceiro.nome_empresa.lower(), cupom.titulo.lower())
    )

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
        .filter(
            ResgateCupom.unidade_id == unidade.id,
            ResgateCupom.status == "Utilizado",
        )
        .order_by(ResgateCupom.data_utilizacao.desc())
        .all()
    )

    return render_template(
        "morador/clube_vantagens.html",
        parceiros_visiveis=parceiros_visiveis,
        categorias=categorias,
        ofertas_por_parceiro=ofertas_por_parceiro,
        cupons_disponiveis=cupons_disponiveis,
        resgates_ativos=resgates_ativos,
        resgates_utilizados=resgates_utilizados,
        unidade=unidade,
        morador=titular,
        dependentes=dependentes,
        aba=request.args.get("aba") or "parceiros",
    )


@unidade_required
def clube_vantagens_resgatar(unidade, cupom_id):
    if unidade.eh_setor_interno:
        flash("Setores internos não participam do Clube de Vantagens.", "warning")
        return redirect(url_for("morador_inicio"))
    cupom = Cupom.query.get_or_404(cupom_id)

    if not cupom.ativo or not cupom.parceiro.ativo:
        flash("Este cupom não está disponível no momento.", "warning")
        return redirect(url_for("morador_clube_vantagens", aba="cupons"))

    if not _parceiro_visivel_no_condominio(cupom.parceiro, unidade.condominio_id):
        flash("Esta oferta não está disponível para o seu condomínio.", "warning")
        return redirect(url_for("morador_clube_vantagens", aba="cupons"))

    if cupom.data_validade and cupom.data_validade < datetime.utcnow().date():
        flash("Este cupom expirou.", "warning")
        return redirect(url_for("morador_clube_vantagens", aba="cupons"))

    resgates_unidade = ResgateCupom.query.filter_by(
        cupom_id=cupom.id,
        unidade_id=unidade.id,
    ).count()
    if resgates_unidade >= cupom.limite_por_unidade:
        flash("Você atingiu o limite de resgates para esta oferta.", "danger")
        return redirect(url_for("morador_clube_vantagens", aba="cupons"))

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
        return redirect(url_for("morador_clube_vantagens", aba="cupons"))

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
        return redirect(url_for("morador_clube_vantagens", aba="cupons"))

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
        return redirect(url_for("morador_clube_vantagens", aba="cupons"))

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
    return redirect(url_for("morador_clube_vantagens", aba="historico"))



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


_ABAS_ATUALIZAR_DADOS = ("proprietarios", "moradores", "veiculos", "seguranca")


def _redirect_atualizar_dados():
    """Volta para Meu Cadastro na mesma aba em que o morador salvou."""
    aba = (request.form.get("aba_ativa") or "").strip()
    if aba in _ABAS_ATUALIZAR_DADOS:
        return redirect(url_for("atualizar_dados", aba=aba))
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
    fotos_novas = []
    fotos_antigas = []

    try:
        listas_separadas = _formulario_usa_listas_separadas(request.form)
        proprietarios_lista = []
        moradores_lista = []
        if listas_separadas:
            proprietarios_lista = _parse_proprietarios_lista_form(request.form)
            moradores_lista = _parse_moradores_form(request.form)
            pessoas_data = _unir_proprietarios_e_moradores(
                proprietarios_lista, moradores_lista
            )
        else:
            pessoas_data = _parse_pessoas_form(request.form)
        veiculos_data = _parse_veiculos_form(request.form)

        if modo_atualizacao:
            if unidade.status not in (StatusUnidade.APROVADA, StatusUnidade.REGISTRADA):
                raise ValueError("Esta unidade não pode ser atualizada.")

            if listas_separadas:
                _validar_ids_pessoas_unidade(
                    unidade, proprietarios_lista + moradores_lista
                )
            else:
                _validar_ids_pessoas_unidade(unidade, pessoas_data)

            if senha:
                if senha != confirmar_senha:
                    raise ValueError("As senhas não conferem.")
                if len(senha) < 6:
                    raise ValueError("A senha deve ter ao menos 6 caracteres.")
                unidade.set_password(senha)
        else:
            from app.planta_unidades import (
                MENSAGEM_CPF,
                MENSAGEM_FORA_DA_PLANTA,
                MENSAGEM_TRAVA,
                cpf_confere,
            )

            unidade = _buscar_unidade(bloco, apartamento, condominio_id=condominio_id)
            if (
                not unidade
                or unidade.eh_setor_interno
                or not unidade.criada_pela_admin
            ):
                raise ValueError(MENSAGEM_FORA_DA_PLANTA)
            if unidade.conta_reivindicada:
                raise ValueError(MENSAGEM_TRAVA)
            if not cpf_confere(unidade, request.form.get("cpf_confirmacao")):
                raise ValueError(MENSAGEM_CPF)
            if not senha or senha != confirmar_senha:
                raise ValueError("Informe e confirme a senha do cadastro.")
            if len(senha) < 6:
                raise ValueError("A senha deve ter ao menos 6 caracteres.")

            unidade.status = StatusUnidade.PENDENTE
            unidade.documento_status = StatusDocumento.PENDENTE
            unidade.conta_reivindicada = True
            unidade.criada_pela_admin = True
            unidade.set_password(senha)

        if not listas_separadas:
            _aplicar_papeis_formulario_legado(
                pessoas_data, unidade if modo_atualizacao else None
            )

        if listas_separadas:
            dados_proprietario = _legado_proprietario_desde_listas(
                proprietarios_lista, moradores_lista
            )
        else:
            dados_proprietario = _parse_proprietario_form(request.form)
        requer_nova_aprovacao = False
        if modo_atualizacao:
            pessoas_atuais_diff = None
            if not listas_separadas:
                pessoas_atuais_diff = [
                    pessoa
                    for pessoa in unidade.pessoas.all()
                    if not _pessoa_somente_proprietario(pessoa)
                ]
            requer_nova_aprovacao = _requer_nova_aprovacao_sindico(
                unidade,
                pessoas_data,
                veiculos_data,
                dados_proprietario,
                pessoas_atuais=pessoas_atuais_diff,
            )
            if not listas_separadas:
                _anexar_proprietarios_nao_ocupantes(unidade, pessoas_data)

        pessoas_criadas = _salvar_pessoas_veiculos(
            unidade,
            pessoas_data,
            veiculos_data,
            modo_atualizacao=modo_atualizacao,
        )
        fotos_novas = []
        fotos_antigas = []
        if listas_separadas:
            fotos_antigas = _aplicar_fotos_faciais_moradores(
                pessoas_criadas[: len(moradores_lista)],
                fotos_novas,
            )

        avisos_upload = []
        slug_drive = _slug_drive_cadastro(unidade)
        arquivo_documento = request.files.get("documento")
        if arquivo_documento and arquivo_documento.filename:
            resultado_doc, erro_doc = _upload_documento_drive(
                arquivo_documento,
                filename=_nome_arquivo_drive(
                    unidade.bloco,
                    unidade.apartamento,
                    "comprovante",
                    arquivo_documento.filename,
                ),
                tenant_slug=slug_drive,
            )
            if erro_doc:
                raise ValueError(erro_doc)
            if resultado_doc:
                unidade.documento_drive_id = resultado_doc.get("id")
                unidade.documento_url = resultado_doc.get("webViewLink")
                unidade.documento_status = StatusDocumento.PENDENTE
            else:
                avisos_upload.append(
                    "o comprovante de residência/propriedade não foi enviado ao Drive"
                )

        arquivo_documento2 = request.files.get("documento2")
        if arquivo_documento2 and arquivo_documento2.filename:
            resultado_doc2, erro_doc2 = _upload_documento_drive(
                arquivo_documento2,
                filename=_nome_arquivo_drive(
                    unidade.bloco,
                    unidade.apartamento,
                    "comprovante2",
                    arquivo_documento2.filename,
                ),
                tenant_slug=slug_drive,
            )
            if erro_doc2:
                raise ValueError(erro_doc2)
            if resultado_doc2:
                unidade.documento2_drive_id = resultado_doc2.get("id")
                unidade.documento2_url = resultado_doc2.get("webViewLink")
                unidade.documento_status = StatusDocumento.PENDENTE
            else:
                avisos_upload.append(
                    "o anexo adicional não foi enviado ao Drive"
                )

        if _responsavel_e_locatario(pessoas_data):
            if not modo_atualizacao:
                unidade.contrato_locacao_status = StatusDocumento.PENDENTE
            elif unidade.contrato_locacao_status == StatusDocumento.NAO_APLICAVEL:
                unidade.contrato_locacao_status = StatusDocumento.PENDENTE

            unidade.proprietario_nome = dados_proprietario["proprietario_nome"]
            unidade.proprietario_telefone = dados_proprietario["proprietario_telefone"]
            unidade.proprietario_email = dados_proprietario["proprietario_email"]
            if listas_separadas:
                unidade.proprietario_cpf = dados_proprietario.get("proprietario_cpf")
            elif not modo_atualizacao:
                unidade.proprietario_cpf = None

            arquivo_contrato = request.files.get("contrato_locacao")
            if arquivo_contrato and arquivo_contrato.filename:
                resultado_ctr, erro_ctr = _upload_documento_drive(
                    arquivo_contrato,
                    filename=_nome_arquivo_drive(
                        unidade.bloco,
                        unidade.apartamento,
                        "contrato",
                        arquivo_contrato.filename,
                    ),
                    tenant_slug=slug_drive,
                )
                if erro_ctr:
                    raise ValueError(erro_ctr)
                if resultado_ctr:
                    unidade.contrato_locacao_drive_id = resultado_ctr.get("id")
                    unidade.contrato_locacao_url = resultado_ctr.get("webViewLink")
                    unidade.contrato_locacao_status = StatusDocumento.PENDENTE
                else:
                    avisos_upload.append(
                        "o contrato de locação não foi enviado ao Drive"
                    )
        else:
            unidade.contrato_locacao_drive_id = None
            unidade.contrato_locacao_url = None
            unidade.contrato_locacao_status = StatusDocumento.NAO_APLICAVEL
            unidade.proprietario_nome = None
            unidade.proprietario_cpf = None
            unidade.proprietario_telefone = None
            unidade.proprietario_email = None

        if modo_atualizacao:
            unidade.data_alteracao = datetime.utcnow()
            if requer_nova_aprovacao:
                # Mantém Aprovada/Registrada para não bloquear o login do titular.
                unidade.atualizacao_pendente = True
            # Sem mudança crítica: atualizacao_pendente permanece como estava.
        db.session.commit()
        for nome_antigo in fotos_antigas:
            _apagar_arquivo_facial(nome_antigo)

        if avisos_upload:
            flash(
                "Cadastro salvo, mas "
                + " e ".join(avisos_upload)
                + ". Envie o(s) arquivo(s) à administração se necessário.",
                "warning",
            )

        if modo_atualizacao:
            if requer_nova_aprovacao:
                flash(
                    "Dados atualizados e enviados para análise do síndico. "
                    "O seu acesso ao sistema continua normal.",
                    "success",
                )
            else:
                flash("Dados atualizados com sucesso.", "success")
            return _redirect_atualizar_dados()

        session.pop("cadastro_bloco", None)
        session.pop("cadastro_apartamento", None)
        flash(
            "Cadastro enviado! Aguarde a aprovação do síndico do seu bloco.",
            "success",
        )
        return redirect(url_for("tenant_login", slug=slug_retorno))

    except ValueError as exc:
        for nome_novo in fotos_novas:
            _apagar_arquivo_facial(nome_novo)
        db.session.rollback()
        flash(str(exc), "danger")
        if modo_atualizacao:
            return _redirect_atualizar_dados()
        return redirect(url_for("cadastro_inicial", slug=slug_retorno))
    except Exception:
        for nome_novo in fotos_novas:
            _apagar_arquivo_facial(nome_novo)
        db.session.rollback()
        traceback.print_exc()
        flash(
            "Ocorreu um erro ao salvar o cadastro. Tente novamente em instantes.",
            "danger",
        )
        if modo_atualizacao:
            return _redirect_atualizar_dados()
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
        AgendamentoMudanca.query.filter(
            AgendamentoMudanca.unidade_id == unidade.id,
            or_(
                AgendamentoMudanca.condominio_id == unidade.condominio_id,
                AgendamentoMudanca.condominio_id.is_(None),
            ),
        )
        .order_by(
            AgendamentoMudanca.data_solicitacao.desc(),
            AgendamentoMudanca.id.desc(),
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

        placa_raw = (request.form.get("placa_veiculo") or "").strip()
        placa_veiculo = placa_raw.upper() if placa_raw else None

        autorizacao = AutorizacaoAcesso(
            condominio_id=unidade.condominio_id,
            unidade_id=unidade.id,
            nome_visitante=nome_visitante,
            documento=documento,
            data_prevista=data_prevista,
            tipo=tipo,
            status=StatusAutorizacaoAcesso.PENDENTE,
            placa_veiculo=placa_veiculo,
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
        lida_pela_gestao=False,
        ultima_interacao_por="MORADOR",
        ultima_interacao_em=datetime.utcnow(),
    )
    db.session.add(ocorrencia)
    db.session.flush()
    _sinalizar_ocorrencia_para_gestao(ocorrencia, nova=True)
    db.session.commit()
    flash("Ocorrência registrada com sucesso.", "success")
    return redirect(url_for("morador_ocorrencias"))


@unidade_required
def morador_ocorrencias_mensagem(unidade, ocorrencia_id):
    """Nova mensagem do morador num chamado ainda em aberto ou em atendimento."""
    if not _unidade_pode_abrir_ocorrencia(unidade):
        return redirect(url_for("atualizar_dados"))
    ocorrencia = Ocorrencia.query.filter_by(
        id=ocorrencia_id,
        unidade_id=unidade.id,
        condominio_id=unidade.condominio_id,
    ).first()
    if ocorrencia is None:
        flash("Ocorrência não encontrada.", "danger")
        return redirect(url_for("morador_ocorrencias"))
    if ocorrencia.status not in (StatusOcorrencia.ABERTO, StatusOcorrencia.EM_ANDAMENTO):
        flash("Este chamado já foi encerrado e não aceita nova mensagem.", "warning")
        return redirect(url_for("morador_ocorrencias"))
    texto = " ".join((request.form.get("mensagem") or "").split())
    if not texto:
        flash("Escreva a mensagem antes de enviar.", "danger")
        return redirect(url_for("morador_ocorrencias"))
    agora = datetime.now(ZoneInfo("America/Sao_Paulo"))
    trecho = f"\n\n[Morador em {agora.strftime('%d/%m/%Y %H:%M')}]\n{texto}"
    ocorrencia.descricao = f"{ocorrencia.descricao}{trecho}"[:8000]
    ocorrencia.lida_pela_gestao = False
    ocorrencia.ultima_interacao_por = "MORADOR"
    ocorrencia.ultima_interacao_em = datetime.utcnow()
    _sinalizar_ocorrencia_para_gestao(ocorrencia, nova=False)
    db.session.commit()
    flash("Mensagem enviada para a gestão.", "success")
    return redirect(url_for("morador_ocorrencias"))


@unidade_required
def morador_encomendas(unidade):
    """Lista encomendas da unidade: pendentes e histórico recente."""
    from sqlalchemy.orm import joinedload

    if not unidade.condominio_id:
        flash("Unidade sem condomínio vinculado. Contate a administração.", "danger")
        return redirect(url_for("atualizar_dados"))

    base_filter = {
        "unidade_id": unidade.id,
        "condominio_id": unidade.condominio_id,
    }

    encomendas_pendentes = (
        Encomenda.query.filter_by(
            **base_filter,
            status=StatusEncomenda.PENDENTE,
        )
        .order_by(Encomenda.data_recebimento.desc())
        .all()
    )
    encomendas_historico = (
        Encomenda.query.options(joinedload(Encomenda.porteiro_entrega))
        .filter_by(
            **base_filter,
            status=StatusEncomenda.ENTREGUE,
        )
        .order_by(Encomenda.data_entrega.desc())
        .limit(50)
        .all()
    )
    return render_template(
        "morador/encomendas.html",
        unidade=unidade,
        encomendas_pendentes=encomendas_pendentes,
        encomendas_historico=encomendas_historico,
    )


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

    notificacoes = _query_notificacoes(
        perfil, condominio_id, unidade_id, get_current_user()
    ).all()
    return render_template(
        "notificacoes.html",
        layout=_layout_notificacoes(perfil),
        notificacoes=notificacoes,
        current_user=get_current_user(),
    )


def _notificacao_do_ator(notificacao_id):
    perfil, condominio_id, unidade_id = _destinatario_notificacoes()
    if not perfil or not condominio_id:
        return None
    return (
        _query_notificacoes(perfil, condominio_id, unidade_id, get_current_user())
        .filter(Notificacao.id == notificacao_id)
        .first()
    )


def _destino_notificacao_seguro(link):
    destino = (link or "").strip()
    prefixos = (
        "/admin/ocorrencias",
        "/sindico/ocorrencias",
        "/morador/ocorrencias",
        "/admin/infracoes",
        "/sindico/infracoes",
        "/morador/infracoes",
        "/admin/reservas",
        "/sindico/reservas",
        "/morador/reservas",
    )
    if destino.startswith(prefixos) and not destino.startswith("//"):
        return destino
    return None


def notificacoes_ler(notificacao_id):
    if _destinatario_notificacoes()[0] is None:
        flash("Faça login para gerenciar notificações.", "warning")
        return _redirect_login_tenant()

    notificacao = _notificacao_do_ator(notificacao_id)
    if not notificacao:
        flash("Notificação não encontrada.", "danger")
        return redirect(url_for("listar_notificacoes"))

    notificacao.lida = True
    db.session.commit()
    return redirect(url_for("listar_notificacoes"))


def notificacoes_abrir(notificacao_id):
    if _destinatario_notificacoes()[0] is None:
        flash("Faça login para gerenciar notificações.", "warning")
        return _redirect_login_tenant()
    notificacao = _notificacao_do_ator(notificacao_id)
    if notificacao is None:
        flash("Notificação não encontrada.", "danger")
        return redirect(url_for("listar_notificacoes"))
    notificacao.lida = True
    destino = _destino_notificacao_seguro(notificacao.link_destino)
    db.session.commit()
    if destino:
        return redirect(destino)
    return redirect(url_for("listar_notificacoes"))


def portal_morador(slug):
    """Porta curta do mural: /c/<slug> abre o login e o cadastro do morador."""
    condominio, bloqueio = _carregar_condominio_entrada(slug)
    if bloqueio is not None:
        return bloqueio
    return redirect(url_for("tenant_login", slug=condominio.slug))


def _condominio_dos_documentos():
    """Tenant dos PDFs oficiais a partir da sessão. Ignora ?id= fora do Super Admin."""
    unidade = get_unidade_logada()
    if unidade and unidade.condominio_id:
        return db.session.get(Condominio, unidade.condominio_id)
    usuario = get_current_user()
    if not usuario:
        return None
    if usuario.role == Role.SUPERADMIN:
        bruto = request.args.get("id", type=int)
        if bruto is None:
            bruto = session.get("condominio_id")
        if not bruto:
            return None
        return db.session.get(Condominio, int(bruto))
    if usuario.condominio_id and usuario.role in (
        Role.ADMIN,
        Role.SINDICO,
        Role.PORTEIRO,
        Role.ASSISTENTE,
    ):
        return db.session.get(Condominio, usuario.condominio_id)
    return None


def condominio_documento(tipo):
    campos = {
        "regimento": ("regimento_filename", "Regimento Interno.pdf"),
        "convencao": ("convencao_filename", "Convencao do Condominio.pdf"),
    }
    if tipo not in campos:
        abort(404)
    condominio = _condominio_dos_documentos()
    if condominio is None:
        flash("Faça login para consultar os documentos do condomínio.", "warning")
        return redirect(url_for("index"))
    atributo, download_name = campos[tipo]
    nome = getattr(condominio, atributo) or ""
    base = os.path.basename(nome)
    if not base or base != nome or ".." in base:
        abort(404)
    pasta = current_app.config["UPLOAD_DOCUMENTOS_FOLDER"]
    if not os.path.isfile(os.path.join(pasta, base)):
        abort(404)
    return send_from_directory(
        pasta,
        base,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=download_name,
    )


def init_app(app):
    from app.blueprints import admin as admin_routes
    from app.blueprints import areas as areas_routes
    from app.blueprints import infracoes as infracoes_routes
    from app.blueprints import api as api_routes
    from app.blueprints import financeiro as financeiro_routes
    from app.blueprints import planta as planta_routes
    from app.blueprints import parceiro as parceiro_routes
    from app.blueprints import portaria as portaria_routes
    from app.blueprints import sindico as sindico_routes
    from app.blueprints import superadmin as superadmin_routes

    parceiro_routes.register(app)
    superadmin_routes.register(app)
    sindico_routes.register(app)
    admin_routes.register(app)
    infracoes_routes.register(app)
    areas_routes.register(app)
    financeiro_routes.register(app)
    planta_routes.register(app)
    portaria_routes.register(app)
    api_routes.register(app)

    app.add_url_rule("/", "index", index, methods=["GET"])
    app.add_url_rule("/c/<slug>", "portal_morador", portal_morador, methods=["GET"])
    app.add_url_rule(
        "/condominio/documentos/<tipo>",
        "condominio_documento",
        condominio_documento,
        methods=["GET"],
    )
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
        "/c/<slug>/status-unidade",
        "status_unidade",
        status_unidade,
        methods=["GET"],
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
    app.add_url_rule("/inicio", "morador_inicio", morador_inicio, methods=["GET"])
    app.add_url_rule(
        "/atualizar-dados",
        "atualizar_dados",
        atualizar_dados,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/atualizar-dados/foto-facial/<int:pessoa_id>",
        "atualizar_foto_facial",
        atualizar_foto_facial,
        methods=["POST"],
    )
    app.add_url_rule(
        "/api/unidade/pessoas-autocomplete",
        "api_unidade_pessoas_autocomplete",
        api_unidade_pessoas_autocomplete,
        methods=["GET"],
    )
    app.add_url_rule(
        "/clube-vantagens",
        "morador_clube_vantagens",
        morador_clube_vantagens,
        methods=["GET"],
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
        "/morador/ocorrencias/<int:ocorrencia_id>/mensagem",
        "morador_ocorrencias_mensagem",
        morador_ocorrencias_mensagem,
        methods=["POST"],
    )
    app.add_url_rule(
        "/morador/encomendas",
        "morador_encomendas",
        morador_encomendas,
        methods=["GET"],
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
    app.add_url_rule(
        "/notificacoes/abrir/<int:notificacao_id>",
        "notificacoes_abrir",
        notificacoes_abrir,
        methods=["POST"],
    )
