from datetime import date, datetime

from werkzeug.security import check_password_hash, generate_password_hash

from app import db


class Role:
    ADMIN = "admin"
    ASSISTENTE = "assistente"
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
    NAO_APLICAVEL = "Nao Aplicavel"


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

    @property
    def is_assistente(self):
        return self.role == Role.ASSISTENTE

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
    contrato_locacao_drive_id = db.Column(db.String(100), nullable=True)
    contrato_locacao_url = db.Column(db.String(500), nullable=True)
    contrato_locacao_status = db.Column(
        db.String(20), nullable=False, default=StatusDocumento.NAO_APLICAVEL
    )
    proprietario_nome = db.Column(db.String(200), nullable=True)
    proprietario_cpf = db.Column(db.String(14), nullable=True)
    proprietario_telefone = db.Column(db.String(20), nullable=True)
    proprietario_email = db.Column(db.String(120), nullable=True)
    notificacao_sindico = db.Column(db.Text, nullable=True)

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
    cupons_resgatados = db.relationship("ResgateCupom", backref="unidade", lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def identificador(self):
        return f"{self.bloco} - {self.apartamento}"

    def __repr__(self):
        return f"<Unidade {self.identificador} ({self.status})>"


class EspacoComum(db.Model):
    __tablename__ = "espacos_comuns"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False)
    tipo = db.Column(db.String(40), nullable=False, default="SALAO_FESTAS")
    gerenciado_por = db.Column(db.String(20), nullable=False)
    bloco_vinculado = db.Column(db.String(50), nullable=True)
    apenas_moradores_bloco = db.Column(db.Boolean, nullable=False, default=False)
    dias_funcionamento = db.Column(
        db.String(80),
        nullable=False,
        default="seg,ter,qua,qui,sex,sab,dom",
    )
    valor_reserva = db.Column(db.Float, nullable=False, default=0.0)

    reservas = db.relationship(
        "Reserva",
        back_populates="espaco",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    def __repr__(self):
        return f"<EspacoComum {self.nome}>"


class Reserva(db.Model):
    __tablename__ = "reservas"

    id = db.Column(db.Integer, primary_key=True)
    espaco_id = db.Column(
        db.Integer, db.ForeignKey("espacos_comuns.id"), nullable=False, index=True
    )
    unidade_id = db.Column(
        db.Integer, db.ForeignKey("unidades.id"), nullable=True, index=True
    )
    data_reserva = db.Column(db.Date, nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="Pendente")
    motivo_reserva = db.Column(db.String(255), nullable=True)
    valor_pago = db.Column(db.Float, nullable=False, default=0.0)
    data_solicitacao = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    espaco = db.relationship("EspacoComum", back_populates="reservas")
    unidade = db.relationship("Unidade")

    def __repr__(self):
        return f"<Reserva {self.id} ({self.status})>"


class Parceiro(db.Model):
    __tablename__ = "parceiro"

    id = db.Column(db.Integer, primary_key=True)
    nome_empresa = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    senha_hash = db.Column(db.String(256), nullable=False)
    telefone = db.Column(db.String(20), nullable=True)
    categoria = db.Column(db.String(50), nullable=False)
    ativo = db.Column(db.Boolean, nullable=False, default=True)
    status = db.Column(db.String(20), nullable=False, default="Pendente")
    data_cadastro = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    cupons = db.relationship("Cupom", backref="parceiro", lazy=True)

    def __repr__(self):
        return f"<Parceiro {self.nome_empresa}>"


class Cupom(db.Model):
    __tablename__ = "cupom"

    id = db.Column(db.Integer, primary_key=True)
    parceiro_id = db.Column(db.Integer, db.ForeignKey("parceiro.id"), nullable=False)
    titulo = db.Column(db.String(100), nullable=False)
    descricao = db.Column(db.Text, nullable=False)
    codigo_prefixo = db.Column(db.String(10), nullable=False)
    data_validade = db.Column(db.Date, nullable=True)
    ativo = db.Column(db.Boolean, nullable=False, default=True)
    limite_total = db.Column(db.Integer, nullable=True)
    limite_por_unidade = db.Column(db.Integer, nullable=False, default=1)
    data_criacao = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    data_update = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
    data_desativacao = db.Column(db.DateTime, nullable=True)

    resgates = db.relationship("ResgateCupom", backref="cupom", lazy=True)

    def __repr__(self):
        return f"<Cupom {self.titulo}>"


class ResgateCupom(db.Model):
    __tablename__ = "resgate_cupom"

    id = db.Column(db.Integer, primary_key=True)
    cupom_id = db.Column(db.Integer, db.ForeignKey("cupom.id"), nullable=False)
    unidade_id = db.Column(db.Integer, db.ForeignKey("unidades.id"), nullable=False)
    codigo_unico = db.Column(db.String(50), unique=True, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="Ativo")
    data_resgate = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    data_utilizacao = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f"<ResgateCupom {self.codigo_unico}>"


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
    autoriza_interfone = db.Column(db.Boolean, nullable=False, default=False)

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


class LogAuditoria(db.Model):
    __tablename__ = "logs_auditoria"

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(
        db.Integer, db.ForeignKey("usuarios.id"), nullable=False, index=True
    )
    mensagem = db.Column(db.Text, nullable=False)
    data_criacao = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    usuario = db.relationship("Usuario")

    def __repr__(self):
        return f"<LogAuditoria {self.id}>"
