"""Verificação local da API de credenciais. Apaga os dados de teste ao final."""
from sqlalchemy import inspect

from app import create_app, db
from app.models import Condominio, CredencialAcesso, LogAuditoria, Pessoa, Unidade, Usuario
from app.utils import gerar_api_key

app = create_app()
client = app.test_client()

with app.app_context():
    inspetor = inspect(db.engine)
    colunas = {c["name"] for c in inspetor.get_columns("condominio")}
    indices = {i["name"] for i in inspetor.get_indexes("condominio")}
    assert "api_key" in colunas, colunas
    assert "uq_condominio_api_key" in indices, indices

    condo = Condominio.query.filter_by(ativo=True).order_by(Condominio.id).first()
    pessoa = (
        Pessoa.query.join(Unidade)
        .filter(Unidade.condominio_id == condo.id)
        .order_by(Pessoa.id)
        .first()
    )
    assert pessoa is not None
    superadmin = Usuario.query.filter_by(role="superadmin").first()
    assert superadmin is not None

    chave = gerar_api_key()
    chave_inativa = gerar_api_key()
    condo.api_key = chave
    isolado = Condominio(
        nome="QA API Isolado",
        slug="qa-api-isolado",
        ativo=True,
        api_key=gerar_api_key(),
    )
    inativo = Condominio(
        nome="QA API Inativo",
        slug="qa-api-inativo",
        ativo=False,
        api_key=chave_inativa,
    )
    db.session.add_all([isolado, inativo])
    db.session.flush()
    ativa = CredencialAcesso(
        tipo="Tag RFID",
        codigo_identificador="QA-API-ATIVA",
        morador_id=pessoa.id,
        condominio_id=condo.id,
        ativa=True,
    )
    revogada = CredencialAcesso(
        tipo="Cartão",
        codigo_identificador="QA-API-REVOGADA",
        morador_id=pessoa.id,
        condominio_id=condo.id,
        ativa=False,
    )
    de_outro = CredencialAcesso(
        tipo="Biometria Facial",
        codigo_identificador="QA-API-OUTRO",
        morador_id=pessoa.id,
        condominio_id=isolado.id,
        ativa=True,
    )
    db.session.add_all([ativa, revogada, de_outro])
    db.session.commit()
    condo_id = condo.id
    isolado_id = isolado.id
    inativo_id = inativo.id
    superadmin_id = superadmin.id
    nome = condo.nome
    bloco = pessoa.unidade.bloco
    apto = pessoa.unidade.apartamento
    morador = pessoa.nome_completo

sem = client.get("/api/v1/credenciais/ativas")
ruim = client.get("/api/v1/credenciais/ativas", headers={"X-API-Key": "chave-inexistente"})
ok = client.get("/api/v1/credenciais/ativas", headers={"X-API-Key": chave})
suspenso = client.get("/api/v1/credenciais/ativas", headers={"X-API-Key": chave_inativa})
assert sem.status_code == 401, sem.status_code
assert ruim.status_code == 401, ruim.status_code
assert suspenso.status_code == 401, suspenso.status_code
assert ok.status_code == 200, ok.get_data(as_text=True)
payload = ok.get_json()
codigos = [item["codigo"] for item in payload["credenciais"]]
assert payload["condominio"] == nome
assert payload["total_credenciais"] == len(payload["credenciais"])
assert "QA-API-ATIVA" in codigos
assert "QA-API-REVOGADA" not in codigos
assert "QA-API-OUTRO" not in codigos
item = next(c for c in payload["credenciais"] if c["codigo"] == "QA-API-ATIVA")
assert item["tipo"] == "Tag RFID"
assert item["morador"] == morador
assert item["unidade"] == f"Bloco {bloco} - Apto {apto}", item["unidade"]

with client.session_transaction() as sess:
    sess["user_id"] = superadmin_id
    sess["role"] = "superadmin"

pagina = client.get("/superadmin/condominios")
html = pagina.get_data(as_text=True)
assert pagina.status_code == 200, pagina.status_code
assert "API Key do Condomínio" in html
assert "Gerar/Regerar Chave" in html
assert chave in html

antes = chave
resposta = client.post(f"/superadmin/condominios/{condo_id}/api-key", follow_redirects=True)
assert resposta.status_code == 200, resposta.status_code
assert "Chave de API do condomínio atualizada" in resposta.get_data(as_text=True)
with app.app_context():
    novo = Condominio.query.get(condo_id).api_key
    log = (
        LogAuditoria.query.filter_by(condominio_id=condo_id)
        .order_by(LogAuditoria.id.desc())
        .first()
    )
    assert novo and novo != antes and len(novo) == 64
    assert antes not in (log.mensagem if log else "")
    assert novo not in (log.mensagem if log else "")

velha = client.get("/api/v1/credenciais/ativas", headers={"X-API-Key": antes})
nova = client.get("/api/v1/credenciais/ativas", headers={"X-API-Key": novo})
assert velha.status_code == 401
assert nova.status_code == 200
assert "QA-API-ATIVA" in [c["codigo"] for c in nova.get_json()["credenciais"]]

with app.app_context():
    CredencialAcesso.query.filter(
        CredencialAcesso.codigo_identificador.in_(
            ["QA-API-ATIVA", "QA-API-REVOGADA", "QA-API-OUTRO"]
        )
    ).delete(synchronize_session=False)
    LogAuditoria.query.filter(
        LogAuditoria.mensagem.like("API Key do condomínio%")
    ).delete(synchronize_session=False)
    Condominio.query.filter(Condominio.id.in_([isolado_id, inativo_id])).delete(
        synchronize_session=False
    )
    alvo = Condominio.query.get(condo_id)
    alvo.api_key = None
    db.session.commit()
    restante = CredencialAcesso.query.filter(
        CredencialAcesso.codigo_identificador.in_(
            ["QA-API-ATIVA", "QA-API-REVOGADA", "QA-API-OUTRO"]
        )
    ).count()
    assert restante == 0
    assert Condominio.query.get(condo_id).api_key is None

print("ok", payload["total_credenciais"], item["unidade"])
