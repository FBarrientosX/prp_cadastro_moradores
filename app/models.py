from datetime import date, datetime

from werkzeug.security import check_password_hash, generate_password_hash

from app import db


class Role:
    ADMIN = "admin"
    SINDICO = "sindico"


class StatusUnidade:
    PENDENTE = "Pendente"
    APROVADA = "Aprovada"
    REGISTRADA = "Registrada"
    REPROVADA = "Reprovada"


class StatusDocumento:
    PENDENTE = "Pendente"
    ENTREGUE = "Entregue"
    NAO_ENVIADO = "Nao Enviado"


class VinculoPessoa:
    PROPRIETARIO = "Proprietário"
    LOCATARIO = "Locatário"
    MORADOR = "Morador"

    CHOICES = (PROPRIETARIO, LOCATARIO, MORADOR)


class Usuario(db.Model):
    __tablename__ = "usuarios"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    bloco_responsavel = db.Column(db.String(50), nullable=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == Role.ADMIN

    @property
    def is_sindico(self):
        return self.role == Role.SINDICO

    def __repr__(self):
        return f"<Usuario {self.username} ({self.role})>"


class Unidade(db.Model):
    __tablename__ = "unidades"
    __table_args__ = (
        db.UniqueConstraint("bloco", "apartamento", name="uq_bloco_apartamento"),
    )

    id = db.Column(db.Integer, primary_key=True)
    bloco = db.Column(db.String(50), nullable=False, index=True)
    apartamento = db.Column(db.String(20), nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    status = db.Column(db.String(20), nullable=False, default=StatusUnidade.PENDENTE)
    data_criacao = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    data_alteracao = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
    documento_drive_id = db.Column(db.String(100), nullable=True)
    documento_url = db.Column(db.String(500), nullable=True)
    documento_status = db.Column(
        db.String(20), nullable=False, default=StatusDocumento.NAO_ENVIADO
    )

    pessoas = db.relationship(
        "Pessoa",
        back_populates="unidade",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    veiculos = db.relationship(
        "Veiculo",
        back_populates="unidade",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def identificador(self):
        return f"{self.bloco} - {self.apartamento}"

    def __repr__(self):
        return f"<Unidade {self.identificador} ({self.status})>"


class Pessoa(db.Model):
    __tablename__ = "pessoas"

    id = db.Column(db.Integer, primary_key=True)
    unidade_id = db.Column(
        db.Integer, db.ForeignKey("unidades.id"), nullable=False, index=True
    )
    nome_completo = db.Column(db.String(200), nullable=False)
    cpf = db.Column(db.String(14), nullable=False)
    vinculo = db.Column(db.String(30), nullable=False)
    telefone = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(120), nullable=True)
    parentesco = db.Column(db.String(100), nullable=True)
    data_nascimento = db.Column(db.Date, nullable=True)
    is_responsavel = db.Column(db.Boolean, nullable=False, default=False)

    unidade = db.relationship("Unidade", back_populates="pessoas")

    def __repr__(self):
        return f"<Pessoa {self.nome_completo}>"


class Veiculo(db.Model):
    __tablename__ = "veiculos"

    id = db.Column(db.Integer, primary_key=True)
    unidade_id = db.Column(
        db.Integer, db.ForeignKey("unidades.id"), nullable=False, index=True
    )
    placa = db.Column(db.String(10), nullable=False)
    marca = db.Column(db.String(50), nullable=False)
    cor = db.Column(db.String(30), nullable=False)

    unidade = db.relationship("Unidade", back_populates="veiculos")

    def __repr__(self):
        return f"<Veiculo {self.placa}>"
