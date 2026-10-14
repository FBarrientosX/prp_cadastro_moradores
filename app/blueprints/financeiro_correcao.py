"""Tela de correção monetária, série histórica e calculadora."""

from datetime import date

from flask import abort, flash, redirect, render_template, request, url_for

from app import db
from app.auth import admin_required
from app.blueprints.financeiro import (
    _condominio_atual,
    _hoje,
    _parse_data,
    _parse_valor,
)
from app.financeiro_correcao import (
    SerieIndisponivel,
    atualizar_serie,
    buscar_serie_bacen,
    calcular_correcao_serie,
    catalogos_visiveis,
    mapa_valores,
    rotulo_formula,
    situacao_serie,
)
from app.models import CatalogoIndice, TipoCalculoIndice

_TIPOS = (
    TipoCalculoIndice.PERCENTUAL_MENSAL,
    TipoCalculoIndice.NUMERO_INDICE,
    TipoCalculoIndice.SOMA_SIMPLES,
)
_ORGAOS = ("SEFAZ-RJ", "IBGE", "FGV", "BACEN", "TJ-SP", "TJ-MG", "ENCOGE", "FIPE", "Governo Federal")


def _parse_indice(texto):
    bruto = str(texto or "").strip().replace(" ", "").replace("%", "")
    if not bruto:
        raise ValueError("vazio")
    if "," in bruto and "." in bruto:
        bruto = bruto.replace(".", "").replace(",", ".")
    elif "," in bruto:
        bruto = bruto.replace(",", ".")
    return float(bruto)


def _catalogo_visivel(indice_id, condominio_id):
    catalogo = db.session.get(CatalogoIndice, indice_id)
    if catalogo is None:
        abort(404)
    if catalogo.condominio_id not in (None, condominio_id):
        abort(404)
    return catalogo


def _cards(condominio_id, padrao, hoje):
    cards = []
    for catalogo in catalogos_visiveis(condominio_id):
        valores = mapa_valores(catalogo.id)
        cards.append(
            {
                "catalogo": catalogo,
                "completo": situacao_serie(valores, hoje, catalogo.tipo_calculo),
                "formula": rotulo_formula(catalogo.tipo_calculo),
                "padrao": catalogo.sigla == padrao,
            }
        )
    return cards


@admin_required
def admin_financeiro_correcao():
    condominio_id, condominio = _condominio_atual()
    hoje = _hoje()
    padrao = (condominio.fin_indice_correcao or "").strip() if condominio else ""
    return render_template(
        "admin/financeiro/correcao.html",
        cards=_cards(condominio_id, padrao, hoje),
        tipos=_TIPOS,
        orgaos=_ORGAOS,
        hoje=hoje,
        multa=float(condominio.fin_multa_percentual or 2) if condominio else 2,
        juros=float(condominio.fin_juros_mensal or 1) if condominio else 1,
    )


@admin_required
def admin_financeiro_correcao_detalhe(indice_id):
    condominio_id, _condominio = _condominio_atual()
    catalogo = _catalogo_visivel(indice_id, condominio_id)
    hoje = _hoje()
    valores = mapa_valores(catalogo.id)
    anos = list(range(hoje.year, 2008, -1))
    grade = []
    for ano in anos:
        meses = []
        for mes in range(1, 13):
            chave = f"{ano:04d}-{mes:02d}"
            passado = date(ano, mes, 1) < date(hoje.year, hoje.month, 1)
            meses.append(
                {
                    "mes": mes,
                    "ano_mes": chave,
                    "valor": valores.get(chave),
                    "alerta": passado and chave not in valores,
                }
            )
        grade.append({"ano": ano, "meses": meses})
    return render_template(
        "admin/financeiro/correcao_detalhe.html",
        catalogo=catalogo,
        grade=grade,
        formula=rotulo_formula(catalogo.tipo_calculo),
    )


@admin_required
def admin_financeiro_correcao_novo():
    condominio_id, _condominio = _condominio_atual()
    sigla = (request.form.get("sigla") or "").strip().upper()
    nome = (request.form.get("nome_completo") or "").strip()
    orgao = (request.form.get("orgao") or "").strip()
    tipo = (request.form.get("tipo_calculo") or "").strip()
    if not sigla or not nome or tipo not in _TIPOS or len(sigla) > 20:
        flash("Informe sigla, nome e o tipo de fórmula.", "danger")
        return redirect(url_for("admin_financeiro_correcao"))
    if CatalogoIndice.query.filter_by(sigla=sigla, condominio_id=condominio_id).first():
        flash("Este condomínio já tem um índice com essa sigla.", "warning")
        return redirect(url_for("admin_financeiro_correcao"))
    codigo = request.form.get("codigo_sgs_bacen", type=int)
    url = (request.form.get("url_fonte_oficial") or "").strip()
    if url and not url.startswith("https://"):
        flash("A fonte oficial precisa ser um link https.", "danger")
        return redirect(url_for("admin_financeiro_correcao"))
    db.session.add(
        CatalogoIndice(
            condominio_id=condominio_id,
            sigla=sigla,
            nome_completo=nome[:120],
            orgao=(orgao or "Outro")[:40],
            tipo_calculo=tipo,
            ignorar_deflacao="1" in request.form.getlist("ignorar_deflacao"),
            codigo_sgs_bacen=codigo if codigo and codigo > 0 else None,
            url_fonte_oficial=url[:300] or None,
            sistema_padrao=False,
        )
    )
    db.session.commit()
    flash("Índice criado para este condomínio.", "success")
    return redirect(url_for("admin_financeiro_correcao"))


@admin_required
def admin_financeiro_correcao_padrao(indice_id):
    condominio_id, condominio = _condominio_atual()
    catalogo = _catalogo_visivel(indice_id, condominio_id)
    if condominio is None:
        abort(404)
    condominio.fin_indice_correcao = catalogo.sigla
    db.session.commit()
    flash("Índice definido como padrão do condomínio.", "success")
    return redirect(url_for("admin_financeiro_correcao"))


@admin_required
def admin_financeiro_correcao_sincronizar():
    condominio_id, _condominio = _condominio_atual()
    total = 0
    falhas = []
    for catalogo in catalogos_visiveis(condominio_id):
        if not catalogo.codigo_sgs_bacen:
            continue
        try:
            meses = buscar_serie_bacen(int(catalogo.codigo_sgs_bacen))
        except SerieIndisponivel:
            falhas.append(catalogo.sigla)
            continue
        total += atualizar_serie(catalogo, meses, substituir=True)
    db.session.commit()
    if falhas:
        flash(
            f"Séries atualizadas ({total} meses). Sem resposta para: {', '.join(falhas)}.",
            "warning",
        )
    else:
        flash(f"Séries do Banco Central atualizadas ({total} meses).", "success")
    return redirect(url_for("admin_financeiro_correcao"))


@admin_required
def admin_financeiro_correcao_bacen(indice_id):
    condominio_id, _condominio = _condominio_atual()
    catalogo = _catalogo_visivel(indice_id, condominio_id)
    if not catalogo.codigo_sgs_bacen:
        flash("Este índice não tem código no SGS do Banco Central.", "warning")
        return redirect(url_for("admin_financeiro_correcao_detalhe", indice_id=catalogo.id))
    try:
        meses = buscar_serie_bacen(int(catalogo.codigo_sgs_bacen))
    except SerieIndisponivel as erro:
        flash(str(erro), "danger")
        return redirect(url_for("admin_financeiro_correcao_detalhe", indice_id=catalogo.id))
    total = atualizar_serie(catalogo, meses, substituir=True)
    db.session.commit()
    flash(f"Série oficial atualizada ({total} meses).", "success")
    return redirect(url_for("admin_financeiro_correcao_detalhe", indice_id=catalogo.id))


@admin_required
def admin_financeiro_correcao_ano(indice_id):
    condominio_id, _condominio = _condominio_atual()
    catalogo = _catalogo_visivel(indice_id, condominio_id)
    try:
        ano = int(request.form.get("ano") or "0")
        valor = _parse_indice(request.form.get("valor"))
    except (TypeError, ValueError):
        flash("Informe o ano e o valor.", "danger")
        return redirect(url_for("admin_financeiro_correcao_detalhe", indice_id=catalogo.id))
    if ano < 1980 or ano > 2100:
        flash("Ano fora da faixa aceita.", "danger")
        return redirect(url_for("admin_financeiro_correcao_detalhe", indice_id=catalogo.id))
    meses = {f"{ano:04d}-{mes:02d}": valor for mes in range(1, 13)}
    atualizar_serie(catalogo, meses, substituir=True)
    db.session.commit()
    flash(f"Os doze meses de {ano} foram preenchidos.", "success")
    return redirect(url_for("admin_financeiro_correcao_detalhe", indice_id=catalogo.id))


@admin_required
def admin_financeiro_correcao_mes(indice_id):
    condominio_id, _condominio = _condominio_atual()
    catalogo = _catalogo_visivel(indice_id, condominio_id)
    ano_mes = (request.form.get("ano_mes") or "").strip()
    try:
        ano = int(ano_mes[:4])
        mes = int(ano_mes[5:])
        valor = _parse_indice(request.form.get("valor"))
    except (TypeError, ValueError):
        flash("Mês ou valor inválido.", "danger")
        return redirect(url_for("admin_financeiro_correcao_detalhe", indice_id=catalogo.id))
    if len(ano_mes) != 7 or mes < 1 or mes > 12:
        flash("Mês inválido.", "danger")
        return redirect(url_for("admin_financeiro_correcao_detalhe", indice_id=catalogo.id))
    atualizar_serie(catalogo, {ano_mes: valor}, substituir=True)
    db.session.commit()
    flash("Mês atualizado.", "success")
    return redirect(url_for("admin_financeiro_correcao_detalhe", indice_id=catalogo.id))


@admin_required
def admin_financeiro_correcao_calcular():
    condominio_id, condominio = _condominio_atual()
    catalogo = _catalogo_visivel(request.form.get("indice_id", type=int) or 0, condominio_id)
    data_inicial = _parse_data(request.form.get("data_inicial"))
    data_final = _parse_data(request.form.get("data_final"))
    try:
        original = _parse_valor(request.form.get("valor_original") or "0")
    except (TypeError, ValueError):
        flash("Informe o valor original.", "danger")
        return redirect(url_for("admin_financeiro_correcao"))
    if data_inicial is None or data_final is None or original <= 0:
        flash("Informe valor, vencimento e data de atualização.", "danger")
        return redirect(url_for("admin_financeiro_correcao"))
    try:
        resultado = calcular_correcao_serie(
            catalogo.tipo_calculo,
            mapa_valores(catalogo.id),
            original,
            data_inicial,
            data_final,
            ignorar_deflacao=bool(catalogo.ignorar_deflacao),
        )
    except ValueError as erro:
        flash(str(erro), "danger")
        return redirect(url_for("admin_financeiro_correcao"))
    multa = juros = 0.0
    if data_final > data_inicial and not resultado["faltantes"]:
        dias = (data_final - data_inicial).days
        base = round(original + resultado["correcao"], 2)
        if request.form.get("incluir_multa") == "1" and condominio is not None:
            multa = round(base * (float(condominio.fin_multa_percentual or 0) / 100.0), 2)
        if request.form.get("incluir_juros") == "1" and condominio is not None:
            juros = round(
                base * ((float(condominio.fin_juros_mensal or 0) / 100.0) / 30.0) * dias,
                2,
            )
    return render_template(
        "financeiro/correcao_memoria.html",
        catalogo=catalogo,
        condominio=condominio,
        original=original,
        data_inicial=data_inicial,
        data_final=data_final,
        resultado=resultado,
        multa=multa,
        juros=juros,
        total=round(original + resultado["correcao"] + multa + juros, 2),
    )


def _formatar_indice(valor):
    if valor is None:
        return "—"
    texto = f"{float(valor):.9f}".rstrip("0").rstrip(".")
    if texto == "-0":
        texto = "0"
    return texto.replace(".", ",")


def register(app):
    app.add_template_filter(_formatar_indice, "indice_num")
    app.add_url_rule(
        "/admin/financeiro/correcao-monetaria",
        "admin_financeiro_correcao",
        admin_financeiro_correcao,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/financeiro/correcao-monetaria/<int:indice_id>",
        "admin_financeiro_correcao_detalhe",
        admin_financeiro_correcao_detalhe,
        methods=["GET"],
    )
    app.add_url_rule(
        "/admin/financeiro/correcao-monetaria/novo",
        "admin_financeiro_correcao_novo",
        admin_financeiro_correcao_novo,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/correcao-monetaria/<int:indice_id>/padrao",
        "admin_financeiro_correcao_padrao",
        admin_financeiro_correcao_padrao,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/correcao-monetaria/sincronizar",
        "admin_financeiro_correcao_sincronizar",
        admin_financeiro_correcao_sincronizar,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/correcao-monetaria/<int:indice_id>/bacen",
        "admin_financeiro_correcao_bacen",
        admin_financeiro_correcao_bacen,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/correcao-monetaria/<int:indice_id>/ano",
        "admin_financeiro_correcao_ano",
        admin_financeiro_correcao_ano,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/correcao-monetaria/<int:indice_id>/mes",
        "admin_financeiro_correcao_mes",
        admin_financeiro_correcao_mes,
        methods=["POST"],
    )
    app.add_url_rule(
        "/admin/financeiro/correcao-monetaria/calcular",
        "admin_financeiro_correcao_calcular",
        admin_financeiro_correcao_calcular,
        methods=["POST"],
    )
