from dotenv import load_dotenv
import os

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

db = SQLAlchemy()

ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}

load_dotenv()


def _garantir_colunas_unidades():
    inspetor = inspect(db.engine)
    if "unidades" not in inspetor.get_table_names():
        return

    colunas = {coluna["name"] for coluna in inspetor.get_columns("unidades")}
    alteracoes = []

    if "contrato_locacao_drive_id" not in colunas:
        alteracoes.append(
            "ALTER TABLE unidades ADD COLUMN contrato_locacao_drive_id VARCHAR(100)"
        )
    if "contrato_locacao_url" not in colunas:
        alteracoes.append(
            "ALTER TABLE unidades ADD COLUMN contrato_locacao_url VARCHAR(500)"
        )
    if "contrato_locacao_status" not in colunas:
        alteracoes.append(
            "ALTER TABLE unidades ADD COLUMN contrato_locacao_status "
            "VARCHAR(20) NOT NULL DEFAULT 'Nao Aplicavel'"
        )
    if "proprietario_nome" not in colunas:
        alteracoes.append(
            "ALTER TABLE unidades ADD COLUMN proprietario_nome VARCHAR(200)"
        )
    if "proprietario_cpf" not in colunas:
        alteracoes.append(
            "ALTER TABLE unidades ADD COLUMN proprietario_cpf VARCHAR(14)"
        )
    if "proprietario_telefone" not in colunas:
        alteracoes.append(
            "ALTER TABLE unidades ADD COLUMN proprietario_telefone VARCHAR(20)"
        )
    if "proprietario_email" not in colunas:
        alteracoes.append(
            "ALTER TABLE unidades ADD COLUMN proprietario_email VARCHAR(120)"
        )
    if "notificacao_sindico" not in colunas:
        alteracoes.append("ALTER TABLE unidades ADD COLUMN notificacao_sindico TEXT")

    for alteracao in alteracoes:
        db.session.execute(text(alteracao))
    if alteracoes:
        db.session.commit()


def _garantir_colunas_pessoas():
    inspetor = inspect(db.engine)
    if "pessoas" not in inspetor.get_table_names():
        return

    colunas = {coluna["name"] for coluna in inspetor.get_columns("pessoas")}
    alteracoes = []

    if "autoriza_interfone" not in colunas:
        alteracoes.append(
            "ALTER TABLE pessoas ADD COLUMN autoriza_interfone BOOLEAN NOT NULL DEFAULT 0"
        )

    for alteracao in alteracoes:
        db.session.execute(text(alteracao))
    if alteracoes:
        db.session.commit()


def _garantir_colunas_reservas():
    inspetor = inspect(db.engine)
    if "reservas" not in inspetor.get_table_names():
        return

    colunas = {coluna["name"] for coluna in inspetor.get_columns("reservas")}
    alteracoes = []

    if "valor_pago" not in colunas:
        alteracoes.append(
            "ALTER TABLE reservas ADD COLUMN valor_pago FLOAT NOT NULL DEFAULT 0"
        )

    for alteracao in alteracoes:
        db.session.execute(text(alteracao))
    if alteracoes:
        db.session.commit()


def create_app(config=None):
    app = Flask(__name__)

    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-change-me-in-production"),
        SQLALCHEMY_DATABASE_URI=os.environ.get(
            "DATABASE_URL", "sqlite:///condominio.db"
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"timeout": 15}},
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    )

    if config:
        app.config.update(config)

    db.init_app(app)

    @app.context_processor
    def inject_nav_context():
        from app.auth import get_current_user, get_unidade_logada
        from app.models import Reserva

        usuario = get_current_user()
        reservas_pendentes_count = 0

        if usuario:
            query = Reserva.query.join(Reserva.espaco).filter(Reserva.status == "Pendente")
            if usuario.role == "sindico":
                reservas_pendentes_count = query.filter(
                    Reserva.espaco.has(bloco_vinculado=usuario.bloco_responsavel)
                ).count()
            elif usuario.role in ("admin", "assistente"):
                reservas_pendentes_count = query.filter(
                    Reserva.espaco.has(gerenciado_por="admin")
                ).count()

        return {
            "sidebar_user": usuario,
            "sidebar_unidade": get_unidade_logada(),
            "reservas_pendentes_count": reservas_pendentes_count,
        }

    from app import routes

    routes.init_app(app)

    with app.app_context():
        from app import models  # noqa: F401

        db.create_all()
        _garantir_colunas_unidades()
        _garantir_colunas_pessoas()
        _garantir_colunas_reservas()

    return app
