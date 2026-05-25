import os

from flask import Flask
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}


def create_app(config=None):
    app = Flask(__name__)

    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-change-me-in-production"),
        SQLALCHEMY_DATABASE_URI=os.environ.get(
            "DATABASE_URL", "sqlite:///condominio.db"
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    )

    if config:
        app.config.update(config)

    db.init_app(app)

    from app import routes

    routes.init_app(app)

    with app.app_context():
        from app import models  # noqa: F401

        db.create_all()

    return app
