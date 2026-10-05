"""Planta física e pagadores. Admin gere; síndico libera o 1º acesso do próprio bloco."""

from flask import flash, redirect, render_template, request, url_for

from app import db
from app.auth import condominio_id_obrigatorio, get_current_user
from app.models import Role, StatusUnidade, Unidade
from app.planta_unidades import (
    gravar_pagador,
    garantir_unidade_planta,
    ler_linhas_pagadores,
    mascarar_documento,
    nome_pagador_publico,
    reabrir_primeiro_acesso,
    definir_senha_temporaria,
)
from app.utils import get_blocos, normalizar_bloco_apartamento, validar_unidade


def _negar_planta():
    if get_current_user():
        flash("Acesso restrito à administração e ao síndico.", "danger")
        return redirect(url_for("admin_index"))
    from app.auth import _redirect_login_tenant

    return _redirect_login_tenant()


def _usuario_planta():
    usuario = get_current_user()
    if not usuario or not usuario.condominio_id:
        return None
    if usuario.role not in (Role.ADMIN, Role.SINDICO):
        return None
    return usuario


def _pode_bloco(usuario, bloco):
    if usuario.role == Role.ADMIN:
        return True
    from app.routes import _sindico_gerencia_bloco

    return _sindico_gerencia_bloco(usuario, bloco)


def _unidades_visiveis(usuario, condominio_id):
    consulta = Unidade.query.filter(
        Unidade.condominio_id == condominio_id,
        Unidade.eh_setor_interno.is_(False),
        Unidade.criada_pela_admin.is_(True),
    )
    if usuario.role == Role.SINDICO:
        blocos = usuario.get_blocos_permitidos()
        if blocos is not None:
            if not blocos:
                return []
            consulta = consulta.filter(Unidade.bloco.in_(blocos))
    return consulta.order_by(Unidade.bloco.asc(), Unidade.apartamento.asc()).all()


def _situacao(unidade):
    if unidade.status == StatusUnidade.PENDENTE or unidade.atualizacao_pendente:
        return "pendente"
    if not unidade.conta_reivindicada or unidade.status == StatusUnidade.PRE_CADASTRO:
        return "pre"
    return "ativa"


def _render_planta(origem):
    usuario = _usuario_planta()
    if usuario is None:
        return _negar_planta()
    condominio_id = condominio_id_obrigatorio(usuario)
    todas = _unidades_visiveis(usuario, condominio_id)
    contagens = {"todas": len(todas), "ativas": 0, "pre": 0, "pendentes": 0}
    for unidade in todas:
        situacao = _situacao(unidade)
        if situacao == "ativa":
            contagens["ativas"] += 1
        elif situacao == "pre":
            contagens["pre"] += 1
        else:
            contagens["pendentes"] += 1
    filtro = request.args.get("filtro") or "todas"
    if filtro not in contagens:
        filtro = "todas"
    bloco = (request.args.get("bloco") or "").strip()
    busca = (request.args.get("q") or "").strip().lower()
    linhas = []
    for unidade in todas:
        situacao = _situacao(unidade)
        if filtro == "ativas" and situacao != "ativa":
            continue
        if filtro == "pre" and situacao != "pre":
            continue
        if filtro == "pendentes" and situacao != "pendente":
            continue
        if bloco and unidade.bloco != bloco:
            continue
        nome = nome_pagador_publico(unidade)
        haystack = f"{unidade.bloco} {unidade.apartamento} {nome}".lower()
        if busca and busca not in haystack:
            continue
        linhas.append(
            {
                "unidade": unidade,
                "nome": nome,
                "documento": mascarar_documento(unidade.cpf_pre_autorizado or unidade.proprietario_cpf),
                "situacao": situacao,
            }
        )
    return render_template(
        "admin/planta.html",
        linhas=linhas,
        contagens=contagens,
        filtro=filtro,
        bloco_atual=bloco,
        busca=busca,
        blocos=get_blocos() if usuario.get_blocos_permitidos() is None else usuario.get_blocos_permitidos(),
        pode_editar=usuario.role == Role.ADMIN,
        origem=origem,
    )


def admin_cadastros():
    return _render_planta("cadastros")


def admin_financeiro_pagadores():
    return _render_planta("financeiro")


def admin_cadastros_salvar():
    usuario = _usuario_planta()
    if usuario is None:
        return _negar_planta()
    if usuario.role != Role.ADMIN:
        flash("Somente a administração cadastra pagadores.", "danger")
        return redirect(url_for("admin_cadastros"))
    condominio_id = condominio_id_obrigatorio(usuario)
    bloco, apartamento = normalizar_bloco_apartamento(
        request.form.get("bloco", ""),
        request.form.get("apartamento", ""),
    )
    if not validar_unidade(bloco, apartamento):
        flash("Combinação de bloco e apartamento inválida.", "danger")
        return redirect(url_for("admin_cadastros"))
    documento = request.form.get("documento") or ""
    from app.planta_unidades import apenas_digitos

    digitos = apenas_digitos(documento)
    if digitos and len(digitos) not in (11, 14):
        flash("Informe um CPF com 11 dígitos ou um CNPJ com 14.", "warning")
        return redirect(url_for("admin_cadastros"))
    email = (request.form.get("email") or "").strip()
    if email and "@" not in email:
        flash("Informe um e-mail válido.", "warning")
        return redirect(url_for("admin_cadastros"))
    unidade = garantir_unidade_planta(condominio_id, bloco, apartamento)
    if unidade is None:
        flash("Esse destino é um setor interno, não uma unidade residencial.", "warning")
        return redirect(url_for("admin_cadastros"))
    gravar_pagador(
        unidade,
        request.form.get("nome"),
        digitos,
        email,
        request.form.get("telefone"),
        substituir_vazios=True,
    )
    from app.routes import _registrar_auditoria

    _registrar_auditoria(usuario, f"Pagador da unidade #{unidade.id} atualizado.")
    db.session.commit()
    flash("Pagador salvo. Rateios e boletos desta unidade já usam esses dados.", "success")
    return redirect(url_for("admin_cadastros", filtro=request.form.get("filtro") or "todas"))


def admin_cadastros_importar():
    usuario = _usuario_planta()
    if usuario is None:
        return _negar_planta()
    if usuario.role != Role.ADMIN:
        flash("Somente a administração importa pagadores.", "danger")
        return redirect(url_for("admin_cadastros"))
    condominio_id = condominio_id_obrigatorio(usuario)
    linhas, ignoradas = ler_linhas_pagadores(request.form.get("texto") or "")
    if not linhas and not ignoradas:
        flash("Cole ao menos uma linha com bloco e apartamento.", "warning")
        return redirect(url_for("admin_cadastros"))
    from app.routes import _registrar_auditoria

    salvas = 0
    for linha in linhas:
        unidade = garantir_unidade_planta(condominio_id, linha["bloco"], linha["apartamento"])
        if unidade is None:
            ignoradas += 1
            continue
        gravar_pagador(
            unidade,
            linha["nome"],
            linha["documento"],
            linha["email"],
            linha["telefone"],
            substituir_vazios=False,
        )
        salvas += 1
    _registrar_auditoria(usuario, f"Importação de pagadores: {salvas} unidade(s).")
    db.session.commit()
    flash(f"Importação concluída: {salvas} unidade(s) atualizada(s). {ignoradas} linha(s) ignorada(s).", "success")
    return redirect(url_for("admin_cadastros"))


def _unidade_do_ator(unidade_id):
    usuario = _usuario_planta()
    if usuario is None:
        return None, None
    condominio_id = condominio_id_obrigatorio(usuario)
    unidade = Unidade.query.filter_by(id=unidade_id, condominio_id=condominio_id).first()
    if unidade is None or unidade.eh_setor_interno or not _pode_bloco(usuario, unidade.bloco):
        flash("Unidade não encontrada neste condomínio.", "warning")
        return usuario, None
    return usuario, unidade


def admin_cadastros_liberar(unidade_id):
    usuario, unidade = _unidade_do_ator(unidade_id)
    if usuario is None:
        return _negar_planta()
    if unidade is None:
        return redirect(url_for("admin_cadastros"))
    reabrir_primeiro_acesso(unidade, limpar_moradores=False)
    from app.routes import _registrar_auditoria

    _registrar_auditoria(usuario, f"1º acesso da unidade #{unidade.id} liberado.")
    db.session.commit()
    flash("1º acesso liberado. O morador pode reivindicar a unidade de novo.", "success")
    return redirect(url_for("admin_cadastros"))


def admin_cadastros_senha(unidade_id):
    usuario, unidade = _unidade_do_ator(unidade_id)
    if usuario is None:
        return _negar_planta()
    if unidade is None:
        return redirect(url_for("admin_cadastros"))
    senha = (request.form.get("senha") or "").strip()
    confirmar = (request.form.get("confirmar") or "").strip()
    if len(senha) < 6 or senha != confirmar:
        flash("Informe e confirme uma senha temporária com ao menos 6 caracteres.", "warning")
        return redirect(url_for("admin_cadastros"))
    definir_senha_temporaria(unidade, senha)
    from app.routes import _registrar_auditoria

    _registrar_auditoria(usuario, f"Senha temporária definida na unidade #{unidade.id}.")
    db.session.commit()
    flash("Senha temporária definida. O morador entra pelo login da unidade.", "success")
    return redirect(url_for("admin_cadastros"))


def register(app):
    app.add_url_rule("/admin/cadastros", "admin_cadastros", admin_cadastros, methods=["GET"])
    app.add_url_rule(
        "/admin/financeiro/pagadores",
        "admin_financeiro_pagadores",
        admin_financeiro_pagadores,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/cadastros/salvar",
        "admin_cadastros_salvar",
        admin_cadastros_salvar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/cadastros/importar",
        "admin_cadastros_importar",
        admin_cadastros_importar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/cadastros/<int:unidade_id>/liberar",
        "admin_cadastros_liberar",
        admin_cadastros_liberar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/cadastros/<int:unidade_id>/senha",
        "admin_cadastros_senha",
        admin_cadastros_senha,
        methods=["POST"],
    )
