"""Planta física, pré-cadastro do pagador e trava do primeiro acesso."""

import re
import secrets

from werkzeug.security import generate_password_hash

MENSAGEM_TRAVA = (
    "🔒 Esta unidade já possui cadastro ativo e senha definida. "
    "Por segurança, não é possível refazer o cadastro ou redefinir o acesso por aqui. "
    "Faça login com sua senha ou solicite suporte à Administração."
)
MENSAGEM_FORA_DA_PLANTA = (
    "Esta unidade não faz parte da planta autorizada pela administração. "
    "Confira o bloco e o apartamento ou fale com a administração."
)
MENSAGEM_CPF = "O CPF informado não confere com o pré-cadastro desta unidade."


def apenas_digitos(texto):
    return re.sub(r"\D", "", texto or "")


def mascarar_nome(nome):
    partes = [parte for parte in (nome or "").split() if parte]
    if not partes:
        return ""
    if len(partes) == 1:
        return partes[0][:1] + "***"
    return f"{partes[0]} {partes[-1][:1]}."


def mascarar_documento(documento):
    digitos = apenas_digitos(documento)
    if len(digitos) < 2:
        return ""
    return f"***.***.***-{digitos[-2:]}"


def mascarar_email(email):
    bruto = (email or "").strip()
    if "@" not in bruto:
        return ""
    local, dominio = bruto.split("@", 1)
    if not local or not dominio:
        return ""
    return f"{local[:1]}***@{dominio}"


def mascarar_telefone(telefone):
    digitos = apenas_digitos(telefone)
    if len(digitos) < 4:
        return ""
    return f"(**) *****-{digitos[-4:]}"


def documento_pre_autorizado(unidade):
    return apenas_digitos(unidade.cpf_pre_autorizado) or apenas_digitos(unidade.proprietario_cpf)


def cpf_confere(unidade, informado):
    esperado = documento_pre_autorizado(unidade)
    if not esperado:
        return True
    return apenas_digitos(informado) == esperado


def nome_pagador_publico(unidade, nome_conhecido=""):
    nome = (nome_conhecido or "").strip()
    if not nome:
        nome = (unidade.proprietario_nome or "").strip()
    if nome:
        return nome[:200]
    return f"Sem pagador definido (Apto {unidade.apartamento})"[:200]


def _senha_inutilizavel():
    return generate_password_hash(secrets.token_urlsafe(32))


def reabrir_primeiro_acesso(unidade, limpar_moradores=False):
    """Devolve a unidade ao pré-cadastro sem apagar a planta. Não faz commit."""
    from app import db
    from app.models import StatusUnidade

    if limpar_moradores:
        for pessoa in list(unidade.pessoas.all()):
            db.session.delete(pessoa)
        for veiculo in list(unidade.veiculos.all()):
            db.session.delete(veiculo)
    unidade.criada_pela_admin = True
    unidade.conta_reivindicada = False
    unidade.atualizacao_pendente = False
    unidade.status = StatusUnidade.PRE_CADASTRO
    unidade.set_password(secrets.token_urlsafe(32))


def definir_senha_temporaria(unidade, senha):
    """Senha definida pela administração. A unidade continua reivindicada."""
    from app.models import StatusUnidade

    unidade.criada_pela_admin = True
    unidade.conta_reivindicada = True
    unidade.set_password(senha)
    if unidade.status == StatusUnidade.PRE_CADASTRO:
        unidade.status = StatusUnidade.APROVADA


def gravar_pagador(unidade, nome, documento, email, telefone, substituir_vazios=False):
    nome = (nome or "").strip()
    documento = apenas_digitos(documento)
    email = (email or "").strip().lower()
    telefone = (telefone or "").strip()
    if nome or substituir_vazios:
        unidade.proprietario_nome = nome[:200] or None
    if documento or substituir_vazios:
        unidade.cpf_pre_autorizado = documento or None
        unidade.proprietario_cpf = (documento[:14] or None) if documento else None
    if email or substituir_vazios:
        unidade.proprietario_email = email[:120] or None
    if telefone or substituir_vazios:
        unidade.proprietario_telefone = telefone[:20] or None


def garantir_unidade_planta(condominio_id, bloco, apartamento):
    """Localiza ou cria a unidade oficial. Não faz commit."""
    from app import db
    from app.models import StatusDocumento, StatusUnidade, Unidade

    unidade = Unidade.query.filter_by(
        condominio_id=condominio_id,
        bloco=bloco,
        apartamento=apartamento,
    ).first()
    if unidade is not None:
        if unidade.eh_setor_interno:
            return None
        unidade.criada_pela_admin = True
        return unidade
    unidade = Unidade(
        condominio_id=condominio_id,
        bloco=bloco,
        apartamento=apartamento,
        password_hash=_senha_inutilizavel(),
        status=StatusUnidade.PRE_CADASTRO,
        documento_status=StatusDocumento.NAO_ENVIADO,
        contrato_locacao_status=StatusDocumento.NAO_APLICAVEL,
        eh_setor_interno=False,
        criada_pela_admin=True,
        conta_reivindicada=False,
    )
    db.session.add(unidade)
    db.session.flush()
    return unidade


def completar_planta_prp():
    """Cria os apartamentos oficiais que ainda não existem e entra na fração igualitária."""
    from app import db
    from app.financeiro_fracao import TITULO_IGUALITARIA, incluir_unidades
    from app.models import Condominio, GrupoFracao, Unidade
    from app.utils import get_condominio_estrutura, normalizar_bloco_apartamento

    condominio = Condominio.query.filter_by(slug="prp").first()
    if condominio is None:
        return 0
    existentes = {
        normalizar_bloco_apartamento(unidade.bloco, unidade.apartamento)
        for unidade in Unidade.query.filter_by(
            condominio_id=condominio.id,
            eh_setor_interno=False,
        ).all()
    }
    criadas = 0
    for bloco, andares in get_condominio_estrutura().items():
        for apartamentos in andares.values():
            for apartamento in apartamentos:
                chave = (bloco, apartamento)
                if chave in existentes:
                    continue
                if garantir_unidade_planta(condominio.id, bloco, apartamento) is not None:
                    criadas += 1
                    existentes.add(chave)
    grupo = (
        GrupoFracao.query.filter_by(condominio_id=condominio.id, padrao=True)
        .order_by(GrupoFracao.id.asc())
        .first()
    )
    if grupo is None:
        grupo = GrupoFracao.query.filter_by(
            condominio_id=condominio.id,
            titulo=TITULO_IGUALITARIA,
        ).first()
    if grupo is not None:
        residenciais = Unidade.query.filter_by(
            condominio_id=condominio.id,
            eh_setor_interno=False,
            criada_pela_admin=True,
        ).all()
        incluir_unidades(grupo, residenciais, 1.0)
    db.session.commit()
    return criadas


def ler_linhas_pagadores(texto):
    """Lê CSV ou texto. Devolve (linhas válidas, quantidade ignorada)."""
    from app.utils import normalizar_bloco_apartamento, validar_unidade

    linhas = []
    ignoradas = 0
    for bruto in (texto or "").splitlines():
        linha = bruto.strip()
        if not linha:
            continue
        if linha.lower().startswith("bloco"):
            continue
        partes = [parte.strip() for parte in (linha.split(";") if ";" in linha else linha.split(","))]
        if len(partes) < 2:
            ignoradas += 1
            continue
        bloco, apartamento = normalizar_bloco_apartamento(partes[0], partes[1])
        if not validar_unidade(bloco, apartamento):
            ignoradas += 1
            continue
        documento = apenas_digitos(partes[3] if len(partes) > 3 else "")
        if documento and len(documento) not in (11, 14):
            ignoradas += 1
            continue
        email = (partes[4] if len(partes) > 4 else "").strip().lower()
        if email and "@" not in email:
            ignoradas += 1
            continue
        linhas.append(
            {
                "bloco": bloco,
                "apartamento": apartamento,
                "nome": (partes[2] if len(partes) > 2 else "").strip(),
                "documento": documento,
                "email": email,
                "telefone": (partes[5] if len(partes) > 5 else "").strip(),
            }
        )
    return linhas, ignoradas
