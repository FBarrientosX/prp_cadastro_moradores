from datetime import date, datetime

from werkzeug.security import check_password_hash, generate_password_hash

from app import db


class Role:
    SUPERADMIN = "superadmin"
    ADMIN = "admin"
    ASSISTENTE = "assistente"
    SINDICO = "sindico"
    PORTEIRO = "porteiro"


class StatusAgendamentoMudanca:
    PENDENTE_SINDICO = "Pendente Síndico"
    PENDENTE_ADMINISTRACAO = "Pendente Administração"
    APROVADA = "Aprovada"
    REJEITADA = "Rejeitada"
    CANCELADA = "Cancelada"

    PENDENTES = (PENDENTE_SINDICO, PENDENTE_ADMINISTRACAO)
    TIPOS = ("Entrada", "Saída")


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


class TipoVisitante:
    VISITANTE = "Visitante"
    PRESTADOR = "Prestador"

    CHOICES = (VISITANTE, PRESTADOR)


class StatusEncomenda:
    PENDENTE = "Pendente"
    ENTREGUE = "Entregue"

    CHOICES = (PENDENTE, ENTREGUE)


class StatusAutorizacaoAcesso:
    PENDENTE = "Pendente"
    CONCLUIDA = "Concluída"
    CANCELADA = "Cancelada"

    CHOICES = (PENDENTE, CONCLUIDA, CANCELADA)


class PerfilDestinoNotificacao:
    MORADOR = "MORADOR"
    PORTARIA = "PORTARIA"

    CHOICES = (MORADOR, PORTARIA)


class StatusOcorrencia:
    ABERTO = "Aberto"
    EM_ANDAMENTO = "Em Andamento"
    RESOLVIDO = "Resolvido"

    CHOICES = (ABERTO, EM_ANDAMENTO, RESOLVIDO)


class CategoriaOcorrencia:
    MANUTENCAO = "Manutenção"
    RECLAMACAO = "Reclamação"
    SUGESTAO = "Sugestão"
    OUTROS = "Outros"

    CHOICES = (MANUTENCAO, RECLAMACAO, SUGESTAO, OUTROS)


class Condominio(db.Model):
    """Tenant raiz do SaaS multi-condomínio."""

    __tablename__ = "condominio"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(50), unique=True, nullable=True, index=True)
    cnpj = db.Column(db.String(18), nullable=True)
    # Soft delete: cliente inativo permanece no histórico (não hard delete).
    ativo = db.Column(db.Boolean, nullable=False, default=True)
    data_cadastro = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    configuracao = db.relationship(
        "ConfiguracaoCondominio",
        back_populates="condominio",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<Condominio {self.id} ({self.nome})>"


class ConfiguracaoCondominio(db.Model):
    """Configurações operacionais 1:1 com Condominio."""

    __tablename__ = "configuracao_condominio"

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer,
        db.ForeignKey("condominio.id"),
        nullable=False,
        unique=True,
        index=True,
    )
    label_agrupamento = db.Column(db.String(50), nullable=False, default="Bloco")
    label_unidade = db.Column(db.String(50), nullable=False, default="Apto")
    usa_agrupamentos = db.Column(db.Boolean, nullable=False, default=True)
    tem_subsindicos = db.Column(db.Boolean, nullable=False, default=True)
    # Valores esperados: 'Simples' | 'Dupla'
    fluxo_aprovacao_mudanca = db.Column(db.String(20), nullable=False, default="Dupla")
    # White-label
    cor_primaria = db.Column(db.String(7), nullable=False, default="#0d6efd")
    logo_filename = db.Column(db.String(255), nullable=True)

    condominio = db.relationship("Condominio", back_populates="configuracao")

    def __repr__(self):
        return f"<ConfiguracaoCondominio condominio_id={self.condominio_id}>"


class SindicoAgrupamento(db.Model):
    """Associa síndico a um ou mais agrupamentos (ex.: blocos) dentro de um condomínio."""

    __tablename__ = "sindico_agrupamento"

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(
        db.Integer, db.ForeignKey("usuarios.id"), nullable=False, index=True
    )
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    nome_agrupamento = db.Column(db.String(50), nullable=False)

    usuario = db.relationship(
        "Usuario",
        backref=db.backref("agrupamentos", lazy="dynamic"),
    )
    condominio = db.relationship(
        "Condominio",
        backref=db.backref("sindico_agrupamentos", lazy="dynamic"),
    )

    def __repr__(self):
        return (
            f"<SindicoAgrupamento usuario_id={self.usuario_id} "
            f"agrupamento={self.nome_agrupamento}>"
        )


class Usuario(db.Model):
    __tablename__ = "usuarios"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    # Multi-tenant: nullable na transição; rotas ainda serão adaptadas.
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=True, index=True
    )
    # Evolução síndico 1:N — responsabilidade passa para SindicoAgrupamento.
    # bloco_responsavel = db.Column(db.String(50), nullable=True)
    # Marca a última troca de senha; usado para invalidar tokens antigos.
    senha_atualizada_em = db.Column(db.DateTime, nullable=True)

    condominio = db.relationship("Condominio", backref=db.backref("usuarios", lazy=True))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
        self.senha_atualizada_em = datetime.utcnow()

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_superadmin(self):
        return self.role == Role.SUPERADMIN

    @property
    def is_admin(self):
        return self.role == Role.ADMIN

    @property
    def is_sindico(self):
        return self.role == Role.SINDICO

    @property
    def is_assistente(self):
        return self.role == Role.ASSISTENTE

    @property
    def is_porteiro(self):
        return self.role == Role.PORTEIRO

    def __repr__(self):
        return f"<Usuario {self.username} ({self.role})>"


class Unidade(db.Model):
    __tablename__ = "unidades"
    __table_args__ = (
        db.UniqueConstraint("bloco", "apartamento", name="uq_bloco_apartamento"),
    )

    id = db.Column(db.Integer, primary_key=True)
    # Multi-tenant: nullable na transição.
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=True, index=True
    )
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
    # Marca a última troca de senha; usado para invalidar tokens de
    # redefinição já consumidos (evita reuso do mesmo link).
    senha_atualizada_em = db.Column(db.DateTime, nullable=True)

    condominio = db.relationship(
        "Condominio", backref=db.backref("unidades", lazy=True)
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
    cupons_resgatados = db.relationship("ResgateCupom", backref="unidade", lazy=True)
    agendamentos_mudanca = db.relationship(
        "AgendamentoMudanca",
        back_populates="unidade",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    registros_acesso = db.relationship(
        "RegistroAcesso",
        back_populates="unidade",
        lazy="dynamic",
    )
    encomendas = db.relationship(
        "Encomenda",
        back_populates="unidade",
        lazy="dynamic",
    )
    ocorrencias = db.relationship(
        "Ocorrencia",
        back_populates="unidade",
        lazy="dynamic",
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
        self.senha_atualizada_em = datetime.utcnow()

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
    # Multi-tenant: nullable na transição; backfill no boot.
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=True, index=True
    )
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

    condominio = db.relationship(
        "Condominio", backref=db.backref("espacos_comuns", lazy=True)
    )
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
    __table_args__ = (
        # Impede duplo-booking do mesmo espaço/data sob concorrência: o banco,
        # não só a checagem em Python, rejeita a segunda reserva ativa.
        db.Index(
            "ux_reserva_espaco_data_ativa",
            "espaco_id",
            "data_reserva",
            unique=True,
            sqlite_where=db.text("status IN ('Pendente', 'Aprovada')"),
        ),
    )

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
    """Parceiro comercial — escopo GLOBAL (sem condominio_id)."""

    __tablename__ = "parceiro"

    id = db.Column(db.Integer, primary_key=True)
    nome_empresa = db.Column(db.String(100), nullable=False)
    usuario_login = db.Column(db.String(80), unique=True, nullable=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    senha_hash = db.Column(db.String(256), nullable=False)
    telefone = db.Column(db.String(20), nullable=True)
    categoria = db.Column(db.String(50), nullable=False)
    endereco = db.Column(db.String(255), nullable=True)
    descricao = db.Column(db.Text, nullable=True)
    logo_arquivo = db.Column(db.String(255), nullable=True)
    link_instagram = db.Column(db.String(255), nullable=True)
    link_facebook = db.Column(db.String(255), nullable=True)
    ativo = db.Column(db.Boolean, nullable=False, default=True)
    status = db.Column(db.String(20), nullable=False, default="Pendente")
    data_cadastro = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # Marca a última troca de senha; usado para invalidar tokens de
    # redefinição já consumidos (evita reuso do mesmo link).
    senha_atualizada_em = db.Column(db.DateTime, nullable=True)

    cupons = db.relationship("Cupom", backref="parceiro", lazy=True)

    def set_password(self, password):
        self.senha_hash = generate_password_hash(password)
        self.senha_atualizada_em = datetime.utcnow()

    def check_password(self, password):
        return check_password_hash(self.senha_hash, password)

    def __repr__(self):
        return f"<Parceiro {self.nome_empresa}>"


class Cupom(db.Model):
    """Cupom do Clube de Vantagens — escopo GLOBAL (sem condominio_id)."""

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
    # Contador atômico: incrementado via UPDATE condicional no resgate, para
    # não depender de um COUNT() seguido de INSERT (vulnerável a corrida).
    total_resgatado = db.Column(db.Integer, nullable=False, default=0)
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
    """
    Resgate transacional do Clube de Vantagens.
    Sem condominio_id direto: o isolamento/rastreio por tenant ocorre via
    unidade_id → Unidade.condominio_id (métricas globais com corte por condomínio).
    """

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
    # Multi-tenant: nullable na transição.
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=True, index=True
    )
    usuario_id = db.Column(
        db.Integer, db.ForeignKey("usuarios.id"), nullable=False, index=True
    )
    mensagem = db.Column(db.Text, nullable=False)
    data_criacao = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    usuario = db.relationship("Usuario")
    condominio = db.relationship(
        "Condominio", backref=db.backref("logs_auditoria", lazy=True)
    )

    def __repr__(self):
        return f"<LogAuditoria {self.id}>"


class AgendamentoMudanca(db.Model):
    __tablename__ = "agendamentos_mudanca"

    id = db.Column(db.Integer, primary_key=True)
    # Multi-tenant: nullable na transição.
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=True, index=True
    )
    unidade_id = db.Column(
        db.Integer, db.ForeignKey("unidades.id"), nullable=False, index=True
    )
    tipo = db.Column(db.String(20), nullable=False)
    data_mudanca = db.Column(db.Date, nullable=False, index=True)
    status = db.Column(
        db.String(50),
        nullable=False,
        default=StatusAgendamentoMudanca.PENDENTE_SINDICO,
    )
    data_solicitacao = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    observacoes = db.Column(db.Text, nullable=True)
    motivo_rejeicao = db.Column(db.Text, nullable=True)
    data_chegada = db.Column(db.DateTime, nullable=True)
    porteiro_id = db.Column(
        db.Integer, db.ForeignKey("usuarios.id"), nullable=True, index=True
    )

    unidade = db.relationship("Unidade", back_populates="agendamentos_mudanca")
    porteiro = db.relationship("Usuario", foreign_keys=[porteiro_id])
    condominio = db.relationship(
        "Condominio", backref=db.backref("agendamentos_mudanca", lazy=True)
    )

    def __repr__(self):
        return f"<AgendamentoMudanca {self.id} ({self.tipo} / {self.status})>"


class Visitante(db.Model):
    """Cadastro de visitante ou prestador de serviço (isolamento por condomínio)."""

    __tablename__ = "visitantes"
    __table_args__ = (
        db.UniqueConstraint(
            "condominio_id",
            "documento",
            name="uq_visitante_documento_condominio",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    nome = db.Column(db.String(200), nullable=False)
    # RG ou CPF; unicidade composta com condominio_id (não global).
    documento = db.Column(db.String(20), nullable=False)
    telefone = db.Column(db.String(20), nullable=True)
    tipo = db.Column(db.String(20), nullable=False, default=TipoVisitante.VISITANTE)
    empresa = db.Column(db.String(200), nullable=True)
    data_criacao = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    condominio = db.relationship(
        "Condominio", backref=db.backref("visitantes", lazy=True)
    )
    registros_acesso = db.relationship(
        "RegistroAcesso",
        back_populates="visitante",
        lazy="dynamic",
    )

    def __repr__(self):
        return f"<Visitante {self.nome} ({self.tipo})>"


class RegistroAcesso(db.Model):
    """Log transacional de entrada/saída na portaria (imutável após criação)."""

    __tablename__ = "registros_acesso"
    __table_args__ = (
        # Impede duas "entradas abertas" simultâneas do mesmo visitante sob
        # concorrência (dois check-ins quase ao mesmo tempo).
        db.Index(
            "ux_registro_acesso_aberto",
            "visitante_id",
            unique=True,
            sqlite_where=db.text("data_saida IS NULL"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    visitante_id = db.Column(
        db.Integer, db.ForeignKey("visitantes.id"), nullable=False, index=True
    )
    unidade_id = db.Column(
        db.Integer, db.ForeignKey("unidades.id"), nullable=False, index=True
    )
    data_entrada = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    data_saida = db.Column(db.DateTime, nullable=True)
    porteiro_id = db.Column(
        db.Integer, db.ForeignKey("usuarios.id"), nullable=False, index=True
    )
    porteiro_saida_id = db.Column(
        db.Integer, db.ForeignKey("usuarios.id"), nullable=True, index=True
    )
    observacoes = db.Column(db.Text, nullable=True)

    condominio = db.relationship(
        "Condominio", backref=db.backref("registros_acesso", lazy=True)
    )
    visitante = db.relationship("Visitante", back_populates="registros_acesso")
    unidade = db.relationship("Unidade", back_populates="registros_acesso")
    porteiro = db.relationship("Usuario", foreign_keys=[porteiro_id])
    porteiro_saida = db.relationship("Usuario", foreign_keys=[porteiro_saida_id])

    def __repr__(self):
        return f"<RegistroAcesso {self.id} visitante_id={self.visitante_id}>"


class Encomenda(db.Model):
    """Pacote recebido na portaria, isolado por condomínio."""

    __tablename__ = "encomendas"

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    unidade_id = db.Column(
        db.Integer, db.ForeignKey("unidades.id"), nullable=False, index=True
    )
    destinatario = db.Column(db.String(200), nullable=True)
    transportadora = db.Column(db.String(100), nullable=True)
    codigo_rastreio = db.Column(db.String(100), nullable=True)
    foto_pacote = db.Column(db.String(255), nullable=True)
    status = db.Column(
        db.String(20), nullable=False, default=StatusEncomenda.PENDENTE, index=True
    )
    data_recebimento = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, index=True
    )
    data_entrega = db.Column(db.DateTime, nullable=True)
    porteiro_recebimento_id = db.Column(
        db.Integer, db.ForeignKey("usuarios.id"), nullable=False, index=True
    )
    porteiro_entrega_id = db.Column(
        db.Integer, db.ForeignKey("usuarios.id"), nullable=True, index=True
    )

    condominio = db.relationship(
        "Condominio", backref=db.backref("encomendas", lazy=True)
    )
    unidade = db.relationship("Unidade", back_populates="encomendas")
    porteiro_recebimento = db.relationship(
        "Usuario", foreign_keys=[porteiro_recebimento_id]
    )
    porteiro_entrega = db.relationship(
        "Usuario", foreign_keys=[porteiro_entrega_id]
    )

    def __repr__(self):
        return f"<Encomenda {self.id} ({self.status})>"


class AutorizacaoAcesso(db.Model):
    """Autorização prévia de visitante/prestador criada pelo morador."""

    __tablename__ = "autorizacoes_acesso"

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    unidade_id = db.Column(
        db.Integer, db.ForeignKey("unidades.id"), nullable=False, index=True
    )
    nome_visitante = db.Column(db.String(200), nullable=False)
    documento = db.Column(db.String(20), nullable=True)
    data_prevista = db.Column(db.Date, nullable=False, index=True)
    tipo = db.Column(db.String(20), nullable=False, default=TipoVisitante.VISITANTE)
    status = db.Column(
        db.String(20),
        nullable=False,
        default=StatusAutorizacaoAcesso.PENDENTE,
        index=True,
    )
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    condominio = db.relationship(
        "Condominio", backref=db.backref("autorizacoes_acesso", lazy=True)
    )
    unidade = db.relationship(
        "Unidade",
        backref=db.backref("autorizacoes_acesso", lazy="dynamic"),
    )

    def __repr__(self):
        return (
            f"<AutorizacaoAcesso {self.id} "
            f"({self.nome_visitante} / {self.status})>"
        )


class Notificacao(db.Model):
    """Alerta interno entre portaria e moradores (isolamento por condomínio)."""

    __tablename__ = "notificacoes"

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    unidade_id = db.Column(
        db.Integer, db.ForeignKey("unidades.id"), nullable=True, index=True
    )
    perfil_destino = db.Column(db.String(20), nullable=False, index=True)
    titulo = db.Column(db.String(120), nullable=False)
    mensagem = db.Column(db.Text, nullable=False)
    lida = db.Column(db.Boolean, nullable=False, default=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    condominio = db.relationship(
        "Condominio", backref=db.backref("notificacoes", lazy=True)
    )
    unidade = db.relationship(
        "Unidade", backref=db.backref("notificacoes", lazy="dynamic")
    )

    def __repr__(self):
        return f"<Notificacao {self.id} ({self.perfil_destino})>"


class Ocorrencia(db.Model):
    """Chamado do helpdesk (livro digital de registros da portaria)."""

    __tablename__ = "ocorrencias"

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    unidade_id = db.Column(
        db.Integer, db.ForeignKey("unidades.id"), nullable=False, index=True
    )
    titulo = db.Column(db.String(200), nullable=False)
    descricao = db.Column(db.Text, nullable=False)
    categoria = db.Column(db.String(30), nullable=False)
    status = db.Column(
        db.String(20),
        nullable=False,
        default=StatusOcorrencia.ABERTO,
        index=True,
    )
    foto_arquivo = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    condominio = db.relationship(
        "Condominio", backref=db.backref("ocorrencias", lazy=True)
    )
    unidade = db.relationship("Unidade", back_populates="ocorrencias")

    def __repr__(self):
        return f"<Ocorrencia {self.id} ({self.status})>"
