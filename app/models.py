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
    CONCLUIDA = "Concluída"

    PENDENTES = (PENDENTE_SINDICO, PENDENTE_ADMINISTRACAO)
    TIPOS = ("Entrada", "Saída")


class StatusUnidade:
    PENDENTE = "Pendente"
    APROVADA = "Aprovada"
    REGISTRADA = "Registrada"
    REPROVADA = "Reprovada"
    PRE_CADASTRO = "Pré-Cadastro Admin"


class StatusPessoa:
    PENDENTE = "Pendente"
    APROVADO = "Aprovado"

    CHOICES = (PENDENTE, APROVADO)


class StatusDocumento:
    PENDENTE = "Pendente"
    ENTREGUE = "Entregue"
    APROVADO = "Aprovado"
    REJEITADO = "Rejeitado"
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
    DELIVERY = "Delivery/Entrega"

    CHOICES = (VISITANTE, PRESTADOR, DELIVERY)


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
    ADMIN = "ADMIN"
    SINDICO = "SINDICO"

    CHOICES = (MORADOR, PORTARIA, ADMIN, SINDICO)


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
    # Chave M2M dos equipamentos de acesso (catraca, RFID, facial).
    api_key = db.Column(db.String(64), unique=True, nullable=True)
    # Token do Vizinsync Agent na portaria. Não é a api_key dos equipamentos.
    agent_api_token = db.Column(db.String(64), unique=True, nullable=True)
    agent_ultimo_ping = db.Column(db.DateTime, nullable=True)
    # Livro de serviço: campos opcionais de apoio e ronda na abertura do plantão.
    permitir_apoio = db.Column(db.Boolean, nullable=False, default=False)
    permitir_ronda = db.Column(db.Boolean, nullable=False, default=False)
    data_cadastro = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    razao_social = db.Column(db.String(200), nullable=True)
    plano = db.Column(db.String(40), nullable=False, default="Profissional")
    fuso_horario = db.Column(
        db.String(64), nullable=False, default="America/Sao_Paulo"
    )
    criado_em = db.Column(db.DateTime, nullable=True, default=datetime.utcnow)
    telefone_fixo = db.Column(db.String(30), nullable=True)
    telefone_whatsapp = db.Column(db.String(30), nullable=True)
    whatsapp_api_url = db.Column(db.String(255), nullable=True)
    whatsapp_api_token = db.Column(db.String(255), nullable=True)
    whatsapp_auto_encomendas = db.Column(db.Boolean, nullable=False, default=True)
    email_contato = db.Column(db.String(120), nullable=True)
    cep = db.Column(db.String(9), nullable=True)
    logradouro = db.Column(db.String(200), nullable=True)
    numero = db.Column(db.String(20), nullable=True)
    complemento = db.Column(db.String(120), nullable=True)
    bairro = db.Column(db.String(120), nullable=True)
    cidade = db.Column(db.String(120), nullable=True)
    uf = db.Column(db.String(2), nullable=True)
    tipo_divisao = db.Column(db.String(20), nullable=False, default="bloco_apto")
    total_unidades_previsto = db.Column(db.Integer, nullable=True)
    nome_responsavel_gestao = db.Column(db.String(200), nullable=True)
    fim_mandato = db.Column(db.Date, nullable=True)
    horario_mudancas = db.Column(db.String(200), nullable=True)
    logo_filename = db.Column(db.String(255), nullable=True)
    regimento_filename = db.Column(db.String(255), nullable=True)
    convencao_filename = db.Column(db.String(255), nullable=True)
    # True: receitas de ADM e de bloco ficam separadas para repasse.
    # False: o condomínio opera em caixa único.
    fin_repasses_ativos = db.Column(db.Boolean, nullable=False, default=True)
    fin_multa_percentual = db.Column(db.Float, nullable=False, default=2.0)
    fin_juros_mensal = db.Column(db.Float, nullable=False, default=1.0)
    fin_indice_correcao = db.Column(db.String(20), nullable=False, default="UFIR-RJ")

    configuracao = db.relationship(
        "ConfiguracaoCondominio",
        back_populates="condominio",
        uselist=False,
        cascade="all, delete-orphan",
    )
    credenciais_acesso = db.relationship(
        "CredencialAcesso",
        back_populates="condominio",
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
    # Escopo do síndico: "1", "1,2" ou "*" (todos os blocos).
    blocos_escopo = db.Column(db.String(120), nullable=True)
    # Permissões extras. Default False trava portaria, áreas gerais e
    # configurações. Reservas do próprio bloco não dependem de flag.
    perm_portaria = db.Column(db.Boolean, nullable=False, default=False)
    perm_reservas_geral = db.Column(db.Boolean, nullable=False, default=False)
    perm_configuracoes = db.Column(db.Boolean, nullable=False, default=False)

    condominio = db.relationship("Condominio", backref=db.backref("usuarios", lazy=True))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
        self.senha_atualizada_em = datetime.utcnow()

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def get_blocos_permitidos(self):
        """Blocos que este usuário enxerga.

        None = todos os blocos (admin, superadmin, assistente ou síndico geral).
        Lista de códigos ("1", "2") = recorte. Lista vazia = nenhum bloco.
        """
        if self.role in (Role.ADMIN, Role.SUPERADMIN, Role.ASSISTENTE):
            return None
        if self.role != Role.SINDICO:
            return []

        from app.utils import get_blocos, normalizar_bloco_codigo

        validos = set(get_blocos())
        bruto = (self.blocos_escopo or "").strip()
        if bruto == "*":
            return None

        if bruto:
            partes = [parte.strip() for parte in bruto.split(",") if parte.strip()]
        else:
            partes = [agrup.nome_agrupamento for agrup in self.agrupamentos.all()]

        codigos = []
        for parte in partes:
            codigo = normalizar_bloco_codigo(parte)
            if codigo in validos and codigo not in codigos:
                codigos.append(codigo)
        return codigos

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
        db.UniqueConstraint(
            "condominio_id",
            "bloco",
            "apartamento",
            name="uq_condominio_bloco_apartamento",
        ),
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
    documento2_drive_id = db.Column(db.String(100), nullable=True)
    documento2_url = db.Column(db.String(500), nullable=True)
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
    # Soft flag: atualização crítica aguarda síndico sem derrubar o login.
    atualizacao_pendente = db.Column(db.Boolean, nullable=False, default=False)
    # Administração, zeladoria e outros destinos da portaria. Não é unidade residencial.
    eh_setor_interno = db.Column(db.Boolean, nullable=False, default=False)
    criada_pela_admin = db.Column(db.Boolean, nullable=False, default=True)
    conta_reivindicada = db.Column(db.Boolean, nullable=False, default=False)
    cpf_pre_autorizado = db.Column(db.String(20), nullable=True)
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
        if self.eh_setor_interno:
            return f"Setor: {self.apartamento}"
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
    # Soft disable (reforma/manutenção): some da vitrine do morador sem apagar histórico.
    ativo = db.Column(db.Boolean, nullable=False, default=True)

    condominio = db.relationship(
        "Condominio", backref=db.backref("espacos_comuns", lazy=True)
    )
    reservas = db.relationship(
        "Reserva",
        back_populates="espaco",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    @property
    def rotulo_vinculo(self):
        """'Geral' quando o espaço é do condomínio; senão 'Bloco N'."""
        bloco = (self.bloco_vinculado or "").strip()
        if not bloco or bloco.upper() == "GERAL":
            return "Geral"
        if bloco.lower().startswith("bloco "):
            return bloco
        return f"Bloco {bloco}"

    def __repr__(self):
        return f"<EspacoComum {self.nome}>"


class Reserva(db.Model):
    __tablename__ = "reservas"
    # Não usar índice único parcial (sqlite_where): o MySQL não suporta
    # CREATE UNIQUE INDEX ... WHERE. Duplo-booking de espaço+data é
    # bloqueado na aplicação antes do commit.

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
    lista_convidados = db.Column(db.Text, nullable=True)
    chaves_entregue_em = db.Column(db.DateTime, nullable=True)
    chaves_devolvida_em = db.Column(db.DateTime, nullable=True)
    porteiro_entrega_chaves_id = db.Column(
        db.Integer, db.ForeignKey("usuarios.id"), nullable=True
    )
    porteiro_devolucao_chaves_id = db.Column(
        db.Integer, db.ForeignKey("usuarios.id"), nullable=True
    )

    espaco = db.relationship("EspacoComum", back_populates="reservas")
    unidade = db.relationship("Unidade")

    def __repr__(self):
        return f"<Reserva {self.id} ({self.status})>"


class CategoriaParceiro(db.Model):
    """Categoria global do Clube de Vantagens (plataforma, sem condomínio)."""

    __tablename__ = "categoria_parceiro"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(80), nullable=False, unique=True)
    ativa = db.Column(db.Boolean, nullable=False, default=True)

    def __repr__(self):
        return f"<CategoriaParceiro {self.nome}>"


parceiro_condominio = db.Table(
    "parceiro_condominio",
    db.Column(
        "parceiro_id",
        db.Integer,
        db.ForeignKey("parceiro.id"),
        primary_key=True,
    ),
    db.Column(
        "condominio_id",
        db.Integer,
        db.ForeignKey("condominio.id"),
        primary_key=True,
    ),
)


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
    categoria_id = db.Column(
        db.Integer,
        db.ForeignKey("categoria_parceiro.id"),
        nullable=True,
        index=True,
    )
    endereco = db.Column(db.String(255), nullable=True)
    descricao = db.Column(db.Text, nullable=True)
    descricao_vantagem = db.Column(db.Text, nullable=True)
    cupom = db.Column(db.String(80), nullable=True)
    logo_arquivo = db.Column(db.String(255), nullable=True)
    logo_drive_id = db.Column(db.String(100), nullable=True)
    logo_url = db.Column(db.String(500), nullable=True)
    link_instagram = db.Column(db.String(255), nullable=True)
    link_facebook = db.Column(db.String(255), nullable=True)
    link_catalogo_externo = db.Column(db.String(500), nullable=True)
    ativo = db.Column(db.Boolean, nullable=False, default=True)
    status = db.Column(db.String(20), nullable=False, default="Pendente")
    data_cadastro = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # Marca a última troca de senha; usado para invalidar tokens de
    # redefinição já consumidos (evita reuso do mesmo link).
    senha_atualizada_em = db.Column(db.DateTime, nullable=True)

    cupons = db.relationship("Cupom", backref="parceiro", lazy=True)
    produtos = db.relationship(
        "ProdutoParceiro",
        backref="parceiro",
        lazy=True,
        cascade="all, delete-orphan",
    )
    categoria_ref = db.relationship("CategoriaParceiro", backref="parceiros")
    condominios = db.relationship(
        "Condominio",
        secondary=parceiro_condominio,
        backref=db.backref("parceiros_vinculados", lazy="dynamic"),
    )

    @property
    def nome(self):
        return self.nome_empresa

    def set_password(self, password):
        self.senha_hash = generate_password_hash(password)
        self.senha_atualizada_em = datetime.utcnow()

    def check_password(self, password):
        return check_password_hash(self.senha_hash, password)

    def __repr__(self):
        return f"<Parceiro {self.nome_empresa}>"


class ProdutoParceiro(db.Model):
    """Item do catálogo virtual do parceiro (produto ou serviço)."""

    __tablename__ = "produto_parceiro"

    id = db.Column(db.Integer, primary_key=True)
    parceiro_id = db.Column(
        db.Integer, db.ForeignKey("parceiro.id"), nullable=False, index=True
    )
    nome = db.Column(db.String(100), nullable=False)
    descricao = db.Column(db.String(255), nullable=True)
    preco_original = db.Column(db.Numeric(10, 2), nullable=True)
    preco_com_desconto = db.Column(db.Numeric(10, 2), nullable=False)
    imagem_drive_id = db.Column(db.String(100), nullable=True)
    imagem_url = db.Column(db.String(500), nullable=True)
    ativo = db.Column(db.Boolean, nullable=False, default=True)

    def __repr__(self):
        return f"<ProdutoParceiro {self.nome}>"


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
    # A mesma pessoa pode ser dona legal e ocupante. Quem já estava
    # cadastrado como ocupante permanece morador; vínculo Proprietário
    # também liga a flag de dono na migração de boot.
    eh_proprietario = db.Column(db.Boolean, nullable=False, default=False)
    eh_morador = db.Column(db.Boolean, nullable=False, default=True)
    status = db.Column(
        db.String(20), nullable=False, default=StatusPessoa.PENDENTE
    )
    foto_perfil = db.Column(db.String(255), nullable=True)
    foto_facial = db.Column(db.String(255), nullable=True)
    foto_atualizada_em = db.Column(db.DateTime, nullable=True)

    unidade = db.relationship("Unidade", back_populates="pessoas")
    credenciais = db.relationship(
        "CredencialAcesso",
        back_populates="morador",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<Pessoa {self.nome_completo}>"


class CredencialAcesso(db.Model):
    """Tag, biometria, controle ou cartão de um morador. Escopo do condomínio."""

    __tablename__ = "credencial_acesso"

    TIPOS_FORMULARIO = (
        "Facial",
        "Tag Veicular",
        "Cartão/Chaveiro RFID",
        "Controle Remoto",
    )
    # Tipos antigos continuam válidos para credenciais já emitidas.
    TIPOS = TIPOS_FORMULARIO + ("Biometria Facial", "Tag RFID", "Cartão")
    TIPOS_FACIAL = ("Facial", "Biometria Facial")
    TIPOS_TAG_CARTAO = ("Tag Veicular", "Cartão/Chaveiro RFID", "Tag RFID", "Cartão")

    id = db.Column(db.Integer, primary_key=True)
    tipo = db.Column(db.String(40), nullable=False)
    codigo_identificador = db.Column(db.String(120), nullable=False, index=True)
    morador_id = db.Column(
        db.Integer, db.ForeignKey("pessoas.id"), nullable=False, index=True
    )
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    ativa = db.Column(db.Boolean, nullable=False, default=True)
    data_emissao = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    morador = db.relationship("Pessoa", back_populates="credenciais")
    condominio = db.relationship("Condominio", back_populates="credenciais_acesso")

    def __repr__(self):
        return f"<CredencialAcesso {self.tipo} {self.codigo_identificador}>"


class EquipamentoAcesso(db.Model):
    """Controladora física (Control iD ou Intelbras) de um condomínio."""

    __tablename__ = "equipamento_acesso"

    FABRICANTES = ("control_id", "intelbras")
    ROTULOS_FABRICANTE = {
        "control_id": "Control iD",
        "intelbras": "Intelbras",
    }

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    nome = db.Column(db.String(120), nullable=False)
    fabricante = db.Column(db.String(20), nullable=False)
    ip_local = db.Column(db.String(45), nullable=False)
    porta = db.Column(db.Integer, nullable=False, default=80)
    usuario_equipamento = db.Column(db.String(80), nullable=False, default="admin")
    senha_equipamento = db.Column(db.String(120), nullable=False, default="admin")
    # Vazio ou GERAL: portaria por onde passam todos os blocos.
    bloco_escopo = db.Column(db.String(20), nullable=True)
    ativo = db.Column(db.Boolean, nullable=False, default=True)
    ultima_sincronia = db.Column(db.DateTime, nullable=True)
    status_ultimo_envio = db.Column(db.String(255), nullable=True)

    condominio = db.relationship(
        "Condominio",
        backref=db.backref(
            "equipamentos_acesso",
            lazy="dynamic",
            cascade="all, delete-orphan",
        ),
    )

    @property
    def rotulo_fabricante(self):
        return self.ROTULOS_FABRICANTE.get(self.fabricante, self.fabricante)

    @property
    def rotulo_escopo(self):
        escopo = (self.bloco_escopo or "").strip()
        if not escopo or escopo.upper() == "GERAL":
            return "Portaria geral"
        return f"Bloco {escopo}"

    def __repr__(self):
        return f"<EquipamentoAcesso {self.nome}>"


class StatusRateio:
    RASCUNHO = "Rascunho"
    RATEADO = "Rateado"


class StatusCobranca:
    A_VENCER = "A Vencer"
    VENCIDA = "Vencida"
    PAGA = "Paga"
    CANCELADA = "Cancelada"
    ACORDO = "Acordo"
    ABERTAS = (A_VENCER, VENCIDA)
    CONSIDERADAS = (A_VENCER, VENCIDA, PAGA)


class StatusBanco:
    NAO_ENVIADO = "Nao Enviado"
    REMESSA_GERADA = "Remessa Gerada"
    REGISTRADO = "Registrado no Banco"
    LIQUIDADO = "Liquidado Retorno"
    REJEITADO = "Rejeitado"


class TipoPlanoConta:
    RECEITA = "RECEITA"
    DESPESA = "DESPESA"
    CHOICES = (RECEITA, DESPESA)


class EscopoRepasse:
    ADM_GERAL = "ADM_GERAL"
    BLOCO = "BLOCO"
    CHOICES = (ADM_GERAL, BLOCO)


class ContaBancaria(db.Model):
    """Conta de caixa ou banco usada na cobrança do condomínio."""

    __tablename__ = "conta_bancaria"

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    nome_banco = db.Column(db.String(80), nullable=False)
    codigo_banco = db.Column(db.String(10), nullable=False)
    agencia = db.Column(db.String(10), nullable=False)
    agencia_dv = db.Column(db.String(2), nullable=True)
    conta = db.Column(db.String(20), nullable=False)
    conta_dv = db.Column(db.String(2), nullable=True)
    carteira = db.Column(db.String(10), nullable=True)
    convenio = db.Column(db.String(20), nullable=True)
    saldo_inicial = db.Column(db.Float, nullable=False, default=0.0)
    saldo_atual = db.Column(db.Float, nullable=False, default=0.0)
    principal = db.Column(db.Boolean, nullable=False, default=True)
    ativa = db.Column(db.Boolean, nullable=False, default=True)

    condominio = db.relationship(
        "Condominio",
        backref=db.backref("contas_bancarias", lazy="dynamic"),
    )

    @property
    def rotulo_agencia(self):
        if self.agencia_dv:
            return f"{self.agencia}-{self.agencia_dv}"
        return self.agencia or ""

    @property
    def rotulo_conta(self):
        if self.conta_dv:
            return f"{self.conta}-{self.conta_dv}"
        return self.conta or ""

    def __repr__(self):
        return f"<ContaBancaria {self.nome_banco} {self.conta}>"


class FundoFinanceiro(db.Model):
    """Caixa, fundo de reserva, fundo de obras e demais bolsos do condomínio."""

    __tablename__ = "fundo_financeiro"
    __table_args__ = (
        db.UniqueConstraint("condominio_id", "codigo", name="uq_fundo_codigo_tenant"),
    )

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    codigo = db.Column(db.String(10), nullable=False)
    nome = db.Column(db.String(120), nullable=False)

    condominio = db.relationship(
        "Condominio",
        backref=db.backref("fundos_financeiros", lazy="dynamic"),
    )
    planos = db.relationship("PlanoConta", back_populates="fundo", lazy="dynamic")

    def __repr__(self):
        return f"<FundoFinanceiro {self.nome}>"


class PlanoConta(db.Model):
    """Plano de contas. O escopo diz se o valor fica na ADM ou vai para o bloco."""

    __tablename__ = "plano_conta"
    __table_args__ = (
        db.UniqueConstraint("condominio_id", "codigo", name="uq_plano_codigo_tenant"),
    )

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    codigo = db.Column(db.String(20), nullable=False)
    nome = db.Column(db.String(120), nullable=False)
    tipo = db.Column(db.String(10), nullable=False)
    fundo_id = db.Column(
        db.Integer, db.ForeignKey("fundo_financeiro.id"), nullable=False, index=True
    )
    escopo_repasse = db.Column(db.String(20), nullable=False, default=EscopoRepasse.ADM_GERAL)

    condominio = db.relationship(
        "Condominio",
        backref=db.backref("planos_conta", lazy="dynamic"),
    )
    fundo = db.relationship("FundoFinanceiro", back_populates="planos")

    def __repr__(self):
        return f"<PlanoConta {self.codigo} {self.nome}>"


class IndiceEconomico(db.Model):
    """Fator mensal de correção (UFIR-RJ, IGP-M, IPCA) por competência."""

    __tablename__ = "indice_economico"
    __table_args__ = (
        db.UniqueConstraint(
            "condominio_id", "sigla", "ano_mes", name="uq_indice_mes_tenant"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    sigla = db.Column(db.String(20), nullable=False)
    ano_mes = db.Column(db.String(7), nullable=False)
    fator_mensal = db.Column(db.Float, nullable=False, default=0.0)
    valor_referencia = db.Column(db.Float, nullable=True)

    condominio = db.relationship(
        "Condominio",
        backref=db.backref("indices_economicos", lazy="dynamic"),
    )

    def __repr__(self):
        return f"<IndiceEconomico {self.sigla} {self.ano_mes}>"


class TipoCalculoIndice:
    PERCENTUAL_MENSAL = "PERCENTUAL_MENSAL"
    NUMERO_INDICE = "NUMERO_INDICE"
    SOMA_SIMPLES = "SOMA_SIMPLES"


class CatalogoIndice(db.Model):
    """Índice de correção. Sem condomínio, vale para toda a plataforma."""

    __tablename__ = "catalogo_indice"

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=True, index=True
    )
    sigla = db.Column(db.String(20), nullable=False, index=True)
    nome_completo = db.Column(db.String(120), nullable=False)
    orgao = db.Column(db.String(40), nullable=False)
    tipo_calculo = db.Column(db.String(30), nullable=False)
    ignorar_deflacao = db.Column(db.Boolean, nullable=False, default=True)
    codigo_sgs_bacen = db.Column(db.Integer, nullable=True)
    url_fonte_oficial = db.Column(db.String(300), nullable=True)
    sistema_padrao = db.Column(db.Boolean, nullable=False, default=True)

    condominio = db.relationship("Condominio")
    valores = db.relationship(
        "ValorIndiceMensal",
        back_populates="catalogo",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    def __repr__(self):
        return f"<CatalogoIndice {self.sigla}>"


class ValorIndiceMensal(db.Model):
    """Ponto da série. Percentual ou número-índice, conforme o catálogo."""

    __tablename__ = "valor_indice_mensal"
    __table_args__ = (
        db.UniqueConstraint("catalogo_id", "ano_mes", name="uq_valor_indice_mes"),
    )

    id = db.Column(db.Integer, primary_key=True)
    catalogo_id = db.Column(
        db.Integer, db.ForeignKey("catalogo_indice.id"), nullable=False, index=True
    )
    ano = db.Column(db.Integer, nullable=False)
    mes = db.Column(db.Integer, nullable=False)
    ano_mes = db.Column(db.String(7), nullable=False)
    valor = db.Column(db.Float, nullable=False, default=0.0)

    catalogo = db.relationship("CatalogoIndice", back_populates="valores")

    def __repr__(self):
        return f"<ValorIndiceMensal {self.ano_mes}>"


class RateioCondominio(db.Model):
    """Lote de taxa mensal: uma composição por bloco ou pelo condomínio geral."""

    __tablename__ = "rateio_condominio"

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    titulo = db.Column(db.String(200), nullable=False)
    competencia = db.Column(db.String(7), nullable=False)
    vencimento_padrao = db.Column(db.Date, nullable=False)
    criterio_bloco = db.Column(db.String(20), nullable=False, default="GERAL")
    itens_json = db.Column(db.JSON, nullable=False)
    valor_unitario = db.Column(db.Float, nullable=False, default=0.0)
    total_gerado = db.Column(db.Float, nullable=False, default=0.0)
    status = db.Column(db.String(20), nullable=False, default=StatusRateio.RASCUNHO)
    grupo_fracao_id = db.Column(
        db.Integer, db.ForeignKey("grupo_fracao.id"), nullable=True, index=True
    )
    modo_rateio = db.Column(db.String(20), nullable=False, default="VALOR_UNITARIO")
    criado_em = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    condominio = db.relationship(
        "Condominio",
        backref=db.backref("rateios", lazy="dynamic"),
    )
    cobrancas = db.relationship("CobrancaUnidade", back_populates="rateio", lazy="dynamic")
    grupo_fracao = db.relationship("GrupoFracao")

    def __repr__(self):
        return f"<RateioCondominio {self.titulo}>"


class ModoFracao:
    VALOR = "VALOR"
    PROPORCAO = "PROPORCAO"


class TipoIsencaoFracao:
    NENHUMA = "NENHUMA"
    PERCENTUAL = "PERCENTUAL"
    VALOR_FIXO = "VALOR_FIXO"


class ModoRateio:
    VALOR_UNITARIO = "VALOR_UNITARIO"
    DIVIDIR_TOTAL = "DIVIDIR_TOTAL"


class GrupoFracao(db.Model):
    """Pesos usados para dividir a taxa entre as unidades."""

    __tablename__ = "grupo_fracao"

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    titulo = db.Column(db.String(160), nullable=False)
    modo_calculo = db.Column(db.String(20), nullable=False, default=ModoFracao.VALOR)
    redistribuir_isencoes = db.Column(db.Boolean, nullable=False, default=True)
    padrao = db.Column(db.Boolean, nullable=False, default=False)
    criado_em = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    atualizado_em = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    condominio = db.relationship("Condominio")
    itens = db.relationship(
        "ItemFracaoUnidade",
        back_populates="grupo",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    def __repr__(self):
        return f"<GrupoFracao {self.id}>"


class ItemFracaoUnidade(db.Model):
    """Peso e isenção de uma unidade dentro de uma fração."""

    __tablename__ = "item_fracao_unidade"
    __table_args__ = (
        db.UniqueConstraint("grupo_fracao_id", "unidade_id", name="uq_fracao_unidade"),
    )

    id = db.Column(db.Integer, primary_key=True)
    grupo_fracao_id = db.Column(
        db.Integer, db.ForeignKey("grupo_fracao.id"), nullable=False, index=True
    )
    unidade_id = db.Column(
        db.Integer, db.ForeignKey("unidades.id"), nullable=False, index=True
    )
    valor_base = db.Column(db.Float, nullable=False, default=1.0)
    ativa_no_rateio = db.Column(db.Boolean, nullable=False, default=True)
    isencao_tipo = db.Column(db.String(20), nullable=False, default=TipoIsencaoFracao.NENHUMA)
    isencao_valor = db.Column(db.Float, nullable=False, default=0.0)
    motivo_isencao = db.Column(db.String(120), nullable=True)

    grupo = db.relationship("GrupoFracao", back_populates="itens")
    unidade = db.relationship("Unidade")

    def __repr__(self):
        return f"<ItemFracaoUnidade {self.unidade_id}>"


class StatusAcordo:
    ATIVO = "Ativo"
    QUITADO = "Quitado"
    CANCELADO = "Cancelado"


class AcordoFinanceiro(db.Model):
    """Parcelamento de cobranças em aberto. As parcelas nascem como novos títulos."""

    __tablename__ = "acordo_financeiro"

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    unidade_id = db.Column(
        db.Integer, db.ForeignKey("unidades.id"), nullable=False, index=True
    )
    cobrancas_originais_ids = db.Column(db.JSON, nullable=False)
    indice_correcao = db.Column(db.String(40), nullable=False, default="UFIR-RJ")
    valor_principal = db.Column(db.Float, nullable=False, default=0.0)
    valor_correcao = db.Column(db.Float, nullable=False, default=0.0)
    valor_juros = db.Column(db.Float, nullable=False, default=0.0)
    valor_multa = db.Column(db.Float, nullable=False, default=0.0)
    valor_judiciais = db.Column(db.Float, nullable=False, default=0.0)
    valor_desconto = db.Column(db.Float, nullable=False, default=0.0)
    valor_total_acordo = db.Column(db.Float, nullable=False, default=0.0)
    qtd_parcelas = db.Column(db.Integer, nullable=False, default=1)
    primeiro_vencimento = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(20), nullable=False, default=StatusAcordo.ATIVO)
    observacoes = db.Column(db.Text, nullable=True)
    criado_em = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    condominio = db.relationship(
        "Condominio",
        backref=db.backref("acordos_financeiros", lazy="dynamic"),
    )
    unidade = db.relationship(
        "Unidade",
        backref=db.backref("acordos_financeiros", lazy="dynamic"),
    )

    def __repr__(self):
        return f"<AcordoFinanceiro {self.id}>"


class CobrancaUnidade(db.Model):
    """Título / boleto de uma unidade. acordo_id aponta para AcordoFinanceiro."""

    __tablename__ = "cobranca_unidade"
    __table_args__ = (
        db.UniqueConstraint(
            "condominio_id", "nosso_numero", name="uq_cobranca_nosso_numero_tenant"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    unidade_id = db.Column(
        db.Integer, db.ForeignKey("unidades.id"), nullable=False, index=True
    )
    conta_bancaria_id = db.Column(
        db.Integer, db.ForeignKey("conta_bancaria.id"), nullable=False, index=True
    )
    rateio_id = db.Column(
        db.Integer, db.ForeignKey("rateio_condominio.id"), nullable=True, index=True
    )
    acordo_id = db.Column(db.Integer, nullable=True)
    competencia = db.Column(db.String(7), nullable=False)
    titulo = db.Column(db.String(200), nullable=False)
    nosso_numero = db.Column(db.String(20), nullable=False)
    vencimento = db.Column(db.Date, nullable=False, index=True)
    pagador_nome = db.Column(db.String(200), nullable=True)
    pagador_documento = db.Column(db.String(20), nullable=True)
    pagador_email = db.Column(db.String(120), nullable=True)
    pagador_telefone = db.Column(db.String(20), nullable=True)
    composicao_json = db.Column(db.JSON, nullable=False)
    valor_original = db.Column(db.Float, nullable=False, default=0.0)
    valor_multa = db.Column(db.Float, nullable=False, default=0.0)
    valor_juros = db.Column(db.Float, nullable=False, default=0.0)
    valor_correcao = db.Column(db.Float, nullable=False, default=0.0)
    valor_outros_acrescimos = db.Column(db.Float, nullable=False, default=0.0)
    valor_desconto = db.Column(db.Float, nullable=False, default=0.0)
    valor_pago = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(20), nullable=False, default=StatusCobranca.A_VENCER, index=True)
    data_pagamento = db.Column(db.Date, nullable=True)
    data_extrato = db.Column(db.Date, nullable=True)
    forma_pagamento = db.Column(db.String(40), nullable=True)
    remessa_gerada = db.Column(db.Boolean, nullable=False, default=False)
    remessa_lote_id = db.Column(db.Integer, nullable=True, index=True)
    status_banco = db.Column(
        db.String(30), nullable=False, default=StatusBanco.NAO_ENVIADO
    )
    codigo_ocorrencia_banco = db.Column(db.String(10), nullable=True)
    observacoes = db.Column(db.Text, nullable=True)
    destinatario_tipo = db.Column(db.String(40), nullable=False, default="Proprietário")
    situacao_juridica = db.Column(db.String(40), nullable=False, default="Normal")
    notificacoes_json = db.Column(db.Text, nullable=False, default="[]")
    anexos_json = db.Column(db.Text, nullable=False, default="[]")
    retorno_cnab_id = db.Column(db.Integer, nullable=True, index=True)
    criado_em = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    condominio = db.relationship(
        "Condominio",
        backref=db.backref("cobrancas_unidade", lazy="dynamic"),
    )
    unidade = db.relationship("Unidade", backref=db.backref("cobrancas", lazy="dynamic"))
    conta_bancaria = db.relationship("ContaBancaria")
    rateio = db.relationship("RateioCondominio", back_populates="cobrancas")

    def __repr__(self):
        return f"<CobrancaUnidade {self.nosso_numero}>"


class ArquivoCnabLog(db.Model):
    """Remessa ou retorno CNAB de uma conta do condomínio."""

    __tablename__ = "arquivo_cnab_log"

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    conta_bancaria_id = db.Column(
        db.Integer, db.ForeignKey("conta_bancaria.id"), nullable=False, index=True
    )
    tipo = db.Column(db.String(10), nullable=False)
    layout = db.Column(db.String(30), nullable=False, default="CNAB400_ITAU")
    nome_arquivo = db.Column(db.String(80), nullable=False)
    sequencial = db.Column(db.Integer, nullable=False, default=1)
    qtd_titulos = db.Column(db.Integer, nullable=False, default=0)
    valor_total = db.Column(db.Float, nullable=False, default=0.0)
    qtd_liquidados = db.Column(db.Integer, nullable=False, default=0)
    qtd_confirmados = db.Column(db.Integer, nullable=False, default=0)
    qtd_nao_encontrados = db.Column(db.Integer, nullable=False, default=0)
    conteudo_texto = db.Column(db.Text, nullable=True)
    criado_em = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    usuario_nome = db.Column(db.String(80), nullable=False, default="")

    condominio = db.relationship("Condominio")
    conta_bancaria = db.relationship("ContaBancaria")

    def __repr__(self):
        return f"<ArquivoCnabLog {self.tipo} {self.nome_arquivo}>"


class StatusDespesa:
    A_VENCER = "A Vencer"
    VENCIDO = "Vencido"
    PAGO = "Pago"
    CANCELADO = "Cancelado"
    ABERTAS = (A_VENCER, VENCIDO)


class StatusRepasse:
    REALIZADO = "Realizado"
    CANCELADO = "Cancelado"


class Fornecedor(db.Model):
    """Credor do condomínio. O nome também fica copiado na despesa."""

    __tablename__ = "fornecedor"

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    nome = db.Column(db.String(200), nullable=False)
    documento = db.Column(db.String(20), nullable=True)
    chave_pix = db.Column(db.String(120), nullable=True)
    telefone = db.Column(db.String(20), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    categoria_padrao = db.Column(db.String(80), nullable=True)
    ativo = db.Column(db.Boolean, nullable=False, default=True)

    condominio = db.relationship("Condominio")

    def __repr__(self):
        return f"<Fornecedor {self.id}>"


class DespesaPagamento(db.Model):
    """Conta a pagar. bloco_alocado GERAL é da administração; senão é do bloco."""

    __tablename__ = "despesa_pagamento"

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    fornecedor_id = db.Column(
        db.Integer, db.ForeignKey("fornecedor.id"), nullable=True, index=True
    )
    fornecedor_nome = db.Column(db.String(200), nullable=True)
    conta_bancaria_id = db.Column(
        db.Integer, db.ForeignKey("conta_bancaria.id"), nullable=False, index=True
    )
    plano_conta_id = db.Column(
        db.Integer, db.ForeignKey("plano_conta.id"), nullable=False, index=True
    )
    fundo_id = db.Column(
        db.Integer, db.ForeignKey("fundo_financeiro.id"), nullable=False, index=True
    )
    titulo = db.Column(db.String(200), nullable=False)
    competencia = db.Column(db.String(7), nullable=False, index=True)
    vencimento = db.Column(db.Date, nullable=False, index=True)
    data_pagamento = db.Column(db.Date, nullable=True)
    data_extrato = db.Column(db.Date, nullable=True)
    operacao = db.Column(db.String(40), nullable=False, default="PIX")
    bloco_alocado = db.Column(db.String(20), nullable=False, default="GERAL")
    valor_original = db.Column(db.Float, nullable=False, default=0.0)
    valor_impostos = db.Column(db.Float, nullable=False, default=0.0)
    valor_descontos = db.Column(db.Float, nullable=False, default=0.0)
    valor_acrescimos = db.Column(db.Float, nullable=False, default=0.0)
    valor_pago = db.Column(db.Float, nullable=False, default=0.0)
    status = db.Column(db.String(20), nullable=False, default=StatusDespesa.A_VENCER, index=True)
    arquivo_anexo = db.Column(db.String(120), nullable=True)
    observacoes = db.Column(db.Text, nullable=True)
    criado_em = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    condominio = db.relationship("Condominio")
    fornecedor = db.relationship("Fornecedor")
    conta_bancaria = db.relationship("ContaBancaria")
    plano_conta = db.relationship("PlanoConta")
    fundo = db.relationship("FundoFinanceiro")

    def __repr__(self):
        return f"<DespesaPagamento {self.id}>"


class RepasseBloco(db.Model):
    """Transferência da administração para o caixa de um bloco."""

    __tablename__ = "repasse_bloco"

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    conta_bancaria_id = db.Column(
        db.Integer, db.ForeignKey("conta_bancaria.id"), nullable=False, index=True
    )
    bloco = db.Column(db.String(20), nullable=False, index=True)
    competencia = db.Column(db.String(7), nullable=False, index=True)
    valor_arrecadado_epoca = db.Column(db.Float, nullable=False, default=0.0)
    valor_descontos_despesas = db.Column(db.Float, nullable=False, default=0.0)
    valor_repassado = db.Column(db.Float, nullable=False, default=0.0)
    data_repasse = db.Column(db.Date, nullable=False)
    forma_transferencia = db.Column(db.String(40), nullable=False, default="PIX")
    favorecido_descricao = db.Column(db.String(200), nullable=True)
    comprovante_anexo = db.Column(db.String(120), nullable=True)
    status = db.Column(db.String(20), nullable=False, default=StatusRepasse.REALIZADO)
    observacoes = db.Column(db.Text, nullable=True)
    criado_em = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    condominio = db.relationship("Condominio")
    conta_bancaria = db.relationship("ContaBancaria")

    def __repr__(self):
        return f"<RepasseBloco {self.bloco} {self.competencia}>"


class TipoRecursoMedidor:
    AGUA = "AGUA"
    GAS = "GAS"
    ENERGIA = "ENERGIA"
    OUTRO = "OUTRO"


class NivelMedicao:
    POR_UNIDADE = "POR_UNIDADE"
    POR_BLOCO = "POR_BLOCO"
    ADM_SETOR = "ADM_SETOR"


class ModoCalculoMedidor:
    METRAGEM = "METRAGEM"
    POR_FAIXA = "POR_FAIXA"
    RATEIO_FATURA = "RATEIO_FATURA"
    MONITORAMENTO_DESPESA = "MONITORAMENTO_DESPESA"


class StatusCicloMedidor:
    ABERTO = "Em Aberto"
    FECHADO = "Fechado"
    COBRADO = "Cobrado"


class MedidorConfig(db.Model):
    """Regra de um relógio: individual, coletivo do bloco ou interno da administração."""

    __tablename__ = "medidor_config"

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    titulo = db.Column(db.String(160), nullable=False)
    tipo_recurso = db.Column(db.String(20), nullable=False, default=TipoRecursoMedidor.AGUA)
    unidade_medida = db.Column(db.String(10), nullable=False, default="m³")
    nivel_medicao = db.Column(db.String(20), nullable=False, default=NivelMedicao.POR_UNIDADE)
    bloco_vinculado = db.Column(db.String(20), nullable=False, default="GERAL")
    modo_calculo = db.Column(db.String(30), nullable=False, default=ModoCalculoMedidor.METRAGEM)
    tarifa_unitaria = db.Column(db.Float, nullable=False, default=0.0)
    taxa_fixa_minima = db.Column(db.Float, nullable=False, default=0.0)
    faixas_json = db.Column(db.Text, nullable=True)
    plano_conta_id = db.Column(db.Integer, db.ForeignKey("plano_conta.id"), nullable=True)
    fundo_id = db.Column(db.Integer, db.ForeignKey("fundo_financeiro.id"), nullable=True)
    permitir_leitura_morador = db.Column(db.Boolean, nullable=False, default=False)
    embutido_taxa_ordinaria = db.Column(db.Boolean, nullable=False, default=True)
    ativo = db.Column(db.Boolean, nullable=False, default=True)
    criado_em = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    condominio = db.relationship("Condominio")
    plano_conta = db.relationship("PlanoConta")
    fundo = db.relationship("FundoFinanceiro")
    participantes = db.relationship(
        "ParticipanteMedidor",
        back_populates="medidor",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    def __repr__(self):
        return f"<MedidorConfig {self.id}>"


class ParticipanteMedidor(db.Model):
    """Unidade ou ponto físico que entra num medidor, com o marco inicial do relógio."""

    __tablename__ = "participante_medidor"

    id = db.Column(db.Integer, primary_key=True)
    medidor_id = db.Column(
        db.Integer, db.ForeignKey("medidor_config.id"), nullable=False, index=True
    )
    unidade_id = db.Column(db.Integer, db.ForeignKey("unidades.id"), nullable=True, index=True)
    identificador_ponto = db.Column(db.String(160), nullable=False)
    numero_serie_relogio = db.Column(db.String(60), nullable=True)
    leitura_inicial = db.Column(db.Float, nullable=False, default=0.0)
    credito_acumulado = db.Column(db.Float, nullable=False, default=0.0)
    ativo = db.Column(db.Boolean, nullable=False, default=True)

    medidor = db.relationship("MedidorConfig", back_populates="participantes")
    unidade = db.relationship("Unidade")

    def __repr__(self):
        return f"<ParticipanteMedidor {self.id}>"


class CicloLeituraMedidor(db.Model):
    """Competência em que as leituras de um medidor são lançadas."""

    __tablename__ = "ciclo_leitura_medidor"
    __table_args__ = (
        db.UniqueConstraint("medidor_id", "competencia", name="uq_ciclo_medidor_competencia"),
    )

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    medidor_id = db.Column(
        db.Integer, db.ForeignKey("medidor_config.id"), nullable=False, index=True
    )
    competencia = db.Column(db.String(7), nullable=False)
    data_leitura = db.Column(db.Date, nullable=False)
    valor_fatura_concessionaria = db.Column(db.Float, nullable=False, default=0.0)
    consumo_total = db.Column(db.Float, nullable=False, default=0.0)
    valor_total_apurado = db.Column(db.Float, nullable=False, default=0.0)
    status = db.Column(db.String(20), nullable=False, default=StatusCicloMedidor.ABERTO)
    observacoes = db.Column(db.Text, nullable=True)
    criado_em = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    condominio = db.relationship("Condominio")
    medidor = db.relationship("MedidorConfig")
    itens = db.relationship(
        "ItemLeituraMedidor",
        back_populates="ciclo",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    def __repr__(self):
        return f"<CicloLeituraMedidor {self.id}>"


class ItemLeituraMedidor(db.Model):
    """Leitura de um ponto dentro do ciclo."""

    __tablename__ = "item_leitura_medidor"

    id = db.Column(db.Integer, primary_key=True)
    ciclo_id = db.Column(
        db.Integer, db.ForeignKey("ciclo_leitura_medidor.id"), nullable=False, index=True
    )
    participante_id = db.Column(
        db.Integer, db.ForeignKey("participante_medidor.id"), nullable=False, index=True
    )
    unidade_id = db.Column(db.Integer, db.ForeignKey("unidades.id"), nullable=True, index=True)
    leitura_anterior = db.Column(db.Float, nullable=False, default=0.0)
    leitura_atual = db.Column(db.Float, nullable=True)
    reiniciada = db.Column(db.Boolean, nullable=False, default=False)
    consumo_apurado = db.Column(db.Float, nullable=False, default=0.0)
    credito_abatido = db.Column(db.Float, nullable=False, default=0.0)
    consumo_final = db.Column(db.Float, nullable=False, default=0.0)
    valor_calculado = db.Column(db.Float, nullable=False, default=0.0)
    foto_relogio = db.Column(db.String(120), nullable=True)
    enviado_pelo_morador = db.Column(db.Boolean, nullable=False, default=False)
    data_envio_morador = db.Column(db.DateTime, nullable=True)
    alerta_anomalia = db.Column(db.Boolean, nullable=False, default=False)
    lancamento_gerado = db.Column(db.Boolean, nullable=False, default=False)
    cobranca_id = db.Column(db.Integer, nullable=True)

    ciclo = db.relationship("CicloLeituraMedidor", back_populates="itens")
    participante = db.relationship("ParticipanteMedidor")
    unidade = db.relationship("Unidade")

    def __repr__(self):
        return f"<ItemLeituraMedidor {self.id}>"


class PrevisaoOrcamentaria(db.Model):
    """Meta mensal de uma conta, da administração geral ou de um bloco."""

    __tablename__ = "previsao_orcamentaria"
    __table_args__ = (
        db.UniqueConstraint(
            "condominio_id",
            "ano",
            "plano_conta_id",
            "bloco_escopo",
            name="uq_previsao_conta_escopo",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    ano = db.Column(db.Integer, nullable=False)
    plano_conta_id = db.Column(
        db.Integer, db.ForeignKey("plano_conta.id"), nullable=False, index=True
    )
    bloco_escopo = db.Column(db.String(20), nullable=False, default="GERAL")
    valores_mensais_json = db.Column(db.Text, nullable=True)
    valor_anual_total = db.Column(db.Float, nullable=False, default=0.0)
    observacoes = db.Column(db.String(300), nullable=True)
    atualizado_em = db.Column(db.DateTime, nullable=True)

    condominio = db.relationship("Condominio")
    plano_conta = db.relationship("PlanoConta")

    def __repr__(self):
        return f"<PrevisaoOrcamentaria {self.ano} {self.plano_conta_id}>"


class TipoLancamentoCaixa:
    ENTRADA = "ENTRADA"
    SAIDA = "SAIDA"
    TRANSFERENCIA_FUNDO = "TRANSFERENCIA_FUNDO"


class LancamentoCaixaAvulso(db.Model):
    """Entrada, saída ou transferência entre fundos que não nasce de boleto ou despesa."""

    __tablename__ = "lancamento_caixa_avulso"

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    conta_bancaria_id = db.Column(
        db.Integer, db.ForeignKey("conta_bancaria.id"), nullable=False, index=True
    )
    plano_conta_id = db.Column(db.Integer, db.ForeignKey("plano_conta.id"), nullable=True)
    fundo_id = db.Column(
        db.Integer, db.ForeignKey("fundo_financeiro.id"), nullable=False, index=True
    )
    tipo = db.Column(db.String(30), nullable=False)
    fundo_destino_id = db.Column(db.Integer, db.ForeignKey("fundo_financeiro.id"), nullable=True)
    competencia = db.Column(db.String(7), nullable=False, index=True)
    data_lancamento = db.Column(db.Date, nullable=False)
    descricao = db.Column(db.String(200), nullable=False)
    bloco_escopo = db.Column(db.String(20), nullable=False, default="GERAL")
    valor = db.Column(db.Float, nullable=False, default=0.0)
    criado_por = db.Column(db.String(80), nullable=False, default="")
    criado_em = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    condominio = db.relationship("Condominio")
    conta_bancaria = db.relationship("ContaBancaria")
    plano_conta = db.relationship("PlanoConta")
    fundo = db.relationship("FundoFinanceiro", foreign_keys=[fundo_id])
    fundo_destino = db.relationship("FundoFinanceiro", foreign_keys=[fundo_destino_id])

    def __repr__(self):
        return f"<LancamentoCaixaAvulso {self.id}>"


class FechamentoMensal(db.Model):
    """Competência auditada. Fechada, não aceita novo lançamento nem baixa."""

    __tablename__ = "fechamento_mensal"
    __table_args__ = (
        db.UniqueConstraint(
            "condominio_id", "competencia", name="uq_fechamento_competencia_tenant"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    competencia = db.Column(db.String(7), nullable=False)
    fechado = db.Column(db.Boolean, nullable=False, default=True)
    saldo_inicial_mes = db.Column(db.Float, nullable=False, default=0.0)
    total_receitas = db.Column(db.Float, nullable=False, default=0.0)
    total_despesas = db.Column(db.Float, nullable=False, default=0.0)
    total_repasses = db.Column(db.Float, nullable=False, default=0.0)
    saldo_final_mes = db.Column(db.Float, nullable=False, default=0.0)
    resumo_snapshot_json = db.Column(db.Text, nullable=True)
    fechado_por = db.Column(db.String(80), nullable=False, default="")
    fechado_em = db.Column(db.DateTime, nullable=True)
    motivo_reabertura = db.Column(db.String(300), nullable=True)

    condominio = db.relationship("Condominio")

    def __repr__(self):
        return f"<FechamentoMensal {self.competencia}>"


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
    data_termino = db.Column(db.DateTime, nullable=True)
    observacao_portaria = db.Column(db.Text, nullable=True)
    porteiro_termino_id = db.Column(
        db.Integer, db.ForeignKey("usuarios.id"), nullable=True
    )

    unidade = db.relationship("Unidade", back_populates="agendamentos_mudanca")
    porteiro = db.relationship("Usuario", foreign_keys=[porteiro_id])
    porteiro_termino = db.relationship("Usuario", foreign_keys=[porteiro_termino_id])
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
    # Não usar índice único parcial (sqlite_where): o MySQL não suporta
    # CREATE UNIQUE INDEX ... WHERE. Duas entradas abertas do mesmo
    # visitante são bloqueadas na aplicação antes do commit.

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
    placa_veiculo = db.Column(db.String(10), nullable=True)

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
    foto_entrega = db.Column(db.String(255), nullable=True)
    status = db.Column(
        db.String(20), nullable=False, default=StatusEncomenda.PENDENTE, index=True
    )
    data_recebimento = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, index=True
    )
    data_entrega = db.Column(db.DateTime, nullable=True)
    entregue_para = db.Column(db.String(200), nullable=True)
    tentativas_contato = db.Column(db.Integer, nullable=False, default=1)
    porteiro_recebimento_id = db.Column(
        db.Integer, db.ForeignKey("usuarios.id"), nullable=False, index=True
    )
    porteiro_entrega_id = db.Column(
        db.Integer, db.ForeignKey("usuarios.id"), nullable=True, index=True
    )
    destinatario_telefone = db.Column(db.String(20), nullable=True)
    whatsapp_notificado = db.Column(db.Boolean, nullable=False, default=False)
    whatsapp_notificado_em = db.Column(db.DateTime, nullable=True)
    whatsapp_modo_envio = db.Column(db.String(20), nullable=True)

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
    placa_veiculo = db.Column(db.String(10), nullable=True)
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
    link_destino = db.Column(db.String(255), nullable=True)
    tipo = db.Column(db.String(30), nullable=False, default="GERAL")
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
    resposta = db.Column(db.Text, nullable=True)
    respondida_em = db.Column(db.DateTime, nullable=True)
    respondida_por_id = db.Column(
        db.Integer, db.ForeignKey("usuarios.id"), nullable=True
    )
    # bloco = síndico do agrupamento; geral = administração do condomínio.
    competencia = db.Column(db.String(20), nullable=False, default="bloco")
    lida_pela_gestao = db.Column(db.Boolean, nullable=False, default=False)
    ultima_interacao_em = db.Column(db.DateTime, nullable=True)
    ultima_interacao_por = db.Column(db.String(20), nullable=True)

    condominio = db.relationship(
        "Condominio", backref=db.backref("ocorrencias", lazy=True)
    )
    unidade = db.relationship("Unidade", back_populates="ocorrencias")
    respondida_por = db.relationship("Usuario", foreign_keys=[respondida_por_id])

    def __repr__(self):
        return f"<Ocorrencia {self.id} ({self.status})>"


class StatusPlantao:
    ABERTO = "Aberto"
    FECHADO = "Fechado"

    CHOICES = (ABERTO, FECHADO)


class Guarita(db.Model):
    """Posto/guarita física de um condomínio (multi-guarita por tenant)."""

    __tablename__ = "guaritas"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    ativa = db.Column(db.Boolean, nullable=False, default=True)

    condominio = db.relationship(
        "Condominio", backref=db.backref("guaritas", lazy="dynamic")
    )
    itens_checklist = db.relationship(
        "ItemChecklist",
        backref="guarita",
        lazy=True,
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<Guarita {self.id} ({self.nome})>"


class Plantao(db.Model):
    """Registro de abertura/fechamento de plantão numa guarita."""

    __tablename__ = "plantoes"

    id = db.Column(db.Integer, primary_key=True)
    guarita_id = db.Column(
        db.Integer, db.ForeignKey("guaritas.id"), nullable=False, index=True
    )
    porteiro_id = db.Column(
        db.Integer, db.ForeignKey("usuarios.id"), nullable=False, index=True
    )
    data_abertura = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    data_fechamento = db.Column(db.DateTime, nullable=True)
    status = db.Column(
        db.String(20),
        nullable=False,
        default=StatusPlantao.ABERTO,
        index=True,
    )
    # JSON serializado: lista de {chave, label, valor} do checklist de abertura.
    checklist_json = db.Column(db.Text, nullable=True)
    ocorrencias = db.Column(db.Text, nullable=True)
    apoio_id = db.Column(
        db.Integer, db.ForeignKey("usuarios.id"), nullable=True, index=True
    )
    ronda_id = db.Column(
        db.Integer, db.ForeignKey("usuarios.id"), nullable=True, index=True
    )

    guarita = db.relationship(
        "Guarita", backref=db.backref("plantoes", lazy="dynamic")
    )
    porteiro = db.relationship(
        "Usuario",
        foreign_keys=[porteiro_id],
        backref=db.backref("plantoes", lazy="dynamic"),
    )
    apoio = db.relationship("Usuario", foreign_keys=[apoio_id])
    ronda = db.relationship("Usuario", foreign_keys=[ronda_id])

    def __repr__(self):
        return f"<Plantao {self.id} ({self.status})>"


class TipoRespostaChecklist:
    BOOLEANO = "booleano"
    TEXTO = "texto"

    CHOICES = (BOOLEANO, TEXTO)


class ItemChecklist(db.Model):
    """Item configurável do checklist de abertura de plantão (por condomínio)."""

    __tablename__ = "itens_checklist"

    id = db.Column(db.Integer, primary_key=True)
    nome_item = db.Column(db.String(120), nullable=False)
    tipo_resposta = db.Column(
        db.String(20), nullable=False, default=TipoRespostaChecklist.BOOLEANO
    )
    condominio_id = db.Column(
        db.Integer, db.ForeignKey("condominio.id"), nullable=False, index=True
    )
    # Nullable na transição; conceptualmente o item pertence a uma guarita.
    guarita_id = db.Column(
        db.Integer, db.ForeignKey("guaritas.id"), nullable=True, index=True
    )
    ativo = db.Column(db.Boolean, nullable=False, default=True)

    condominio = db.relationship(
        "Condominio", backref=db.backref("itens_checklist", lazy="dynamic")
    )

    def __repr__(self):
        return f"<ItemChecklist {self.id} ({self.nome_item})>"
