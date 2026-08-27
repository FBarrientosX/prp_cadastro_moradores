from dotenv import load_dotenv
import os

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

db = SQLAlchemy()

ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}

load_dotenv()


def _garantir_colunas_usuarios():
    inspetor = inspect(db.engine)
    if "usuarios" not in inspetor.get_table_names():
        return

    colunas = {coluna["name"] for coluna in inspetor.get_columns("usuarios")}
    if "senha_atualizada_em" in colunas:
        return

    db.session.execute(
        text("ALTER TABLE usuarios ADD COLUMN senha_atualizada_em DATETIME")
    )
    db.session.commit()


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
    if "senha_atualizada_em" not in colunas:
        alteracoes.append(
            "ALTER TABLE unidades ADD COLUMN senha_atualizada_em DATETIME"
        )

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

    colunas_info = inspetor.get_columns("reservas")
    colunas = {coluna["name"] for coluna in colunas_info}
    alteracoes = []

    if "valor_pago" not in colunas:
        alteracoes.append(
            "ALTER TABLE reservas ADD COLUMN valor_pago FLOAT NOT NULL DEFAULT 0"
        )
    if "motivo_reserva" not in colunas:
        alteracoes.append("ALTER TABLE reservas ADD COLUMN motivo_reserva VARCHAR(255)")

    for alteracao in alteracoes:
        db.session.execute(text(alteracao))
    if alteracoes:
        db.session.commit()

    unidade_coluna = next(
        (coluna for coluna in colunas_info if coluna["name"] == "unidade_id"),
        None,
    )
    if unidade_coluna and unidade_coluna.get("nullable") is False:
        db.session.execute(text("ALTER TABLE reservas RENAME TO reservas_old"))
        db.session.execute(
            text(
                """
                CREATE TABLE reservas (
                    id INTEGER NOT NULL PRIMARY KEY,
                    espaco_id INTEGER NOT NULL,
                    unidade_id INTEGER,
                    data_reserva DATE NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'Pendente',
                    valor_pago FLOAT NOT NULL DEFAULT 0,
                    data_solicitacao DATETIME NOT NULL,
                    motivo_reserva VARCHAR(255),
                    FOREIGN KEY(espaco_id) REFERENCES espacos_comuns (id),
                    FOREIGN KEY(unidade_id) REFERENCES unidades (id)
                )
                """
            )
        )
        db.session.execute(
            text(
                """
                INSERT INTO reservas (
                    id,
                    espaco_id,
                    unidade_id,
                    data_reserva,
                    status,
                    valor_pago,
                    data_solicitacao,
                    motivo_reserva
                )
                SELECT
                    id,
                    espaco_id,
                    unidade_id,
                    data_reserva,
                    status,
                    COALESCE(valor_pago, 0),
                    data_solicitacao,
                    motivo_reserva
                FROM reservas_old
                """
            )
        )
        db.session.execute(text("DROP TABLE reservas_old"))
        db.session.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_reservas_data_reserva ON reservas (data_reserva)"
            )
        )
        db.session.execute(
            text("CREATE INDEX IF NOT EXISTS ix_reservas_espaco_id ON reservas (espaco_id)")
        )
        db.session.execute(
            text("CREATE INDEX IF NOT EXISTS ix_reservas_unidade_id ON reservas (unidade_id)")
        )
        db.session.commit()

    # Índice único parcial: impede duplo-booking do mesmo espaço/data sob
    # concorrência. Só cria se não houver violações herdadas (banco legado
    # com reservas duplicadas de uma corrida anterior a esta correção).
    duplicados = db.session.execute(
        text(
            """
            SELECT espaco_id, data_reserva, COUNT(*) AS total
            FROM reservas
            WHERE status IN ('Pendente', 'Aprovada')
            GROUP BY espaco_id, data_reserva
            HAVING COUNT(*) > 1
            """
        )
    ).fetchall()
    if duplicados:
        print(
            "AVISO: existem reservas duplicadas (mesmo espaço/data, ambas "
            "Pendente/Aprovada) — resolva manualmente antes que o índice "
            "único de proteção contra duplo-booking possa ser criado."
        )
    else:
        db.session.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_reserva_espaco_data_ativa "
                "ON reservas (espaco_id, data_reserva) "
                "WHERE status IN ('Pendente', 'Aprovada')"
            )
        )
        db.session.commit()


def _garantir_coluna_condominio_espacos_comuns():
    """Isolamento multi-tenant: condominio_id em áreas comuns + backfill no cliente legado."""
    inspetor = inspect(db.engine)
    if "espacos_comuns" not in inspetor.get_table_names():
        return

    colunas = {coluna["name"] for coluna in inspetor.get_columns("espacos_comuns")}
    if "condominio_id" not in colunas:
        db.session.execute(
            text("ALTER TABLE espacos_comuns ADD COLUMN condominio_id INTEGER")
        )
        db.session.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_espacos_comuns_condominio_id "
                "ON espacos_comuns (condominio_id)"
            )
        )
        db.session.commit()

    from app.models import Condominio

    condominio = Condominio.query.filter_by(slug="prp").first()
    if condominio is None:
        condominio = Condominio.query.order_by(Condominio.id).first()
    if condominio is None:
        return

    db.session.execute(
        text(
            "UPDATE espacos_comuns "
            "SET condominio_id = :condominio_id "
            "WHERE condominio_id IS NULL"
        ),
        {"condominio_id": condominio.id},
    )
    db.session.commit()


def _garantir_tabelas_parceiros(app):
    with app.app_context():
        db.create_all()


def _garantir_colunas_parceiros():
    inspetor = inspect(db.engine)
    if "parceiro" not in inspetor.get_table_names():
        return

    colunas = {coluna["name"] for coluna in inspetor.get_columns("parceiro")}
    alteracoes = []
    if "status" not in colunas:
        alteracoes.append(
            "ALTER TABLE parceiro ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'Pendente'"
        )
    if "descricao" not in colunas:
        alteracoes.append("ALTER TABLE parceiro ADD COLUMN descricao TEXT")
    if "endereco" not in colunas:
        alteracoes.append("ALTER TABLE parceiro ADD COLUMN endereco VARCHAR(255)")
    if "usuario_login" not in colunas:
        alteracoes.append("ALTER TABLE parceiro ADD COLUMN usuario_login VARCHAR(80)")
    if "logo_arquivo" not in colunas:
        alteracoes.append("ALTER TABLE parceiro ADD COLUMN logo_arquivo VARCHAR(255)")
    if "link_instagram" not in colunas:
        alteracoes.append("ALTER TABLE parceiro ADD COLUMN link_instagram VARCHAR(255)")
    if "link_facebook" not in colunas:
        alteracoes.append("ALTER TABLE parceiro ADD COLUMN link_facebook VARCHAR(255)")
    if "senha_atualizada_em" not in colunas:
        alteracoes.append(
            "ALTER TABLE parceiro ADD COLUMN senha_atualizada_em DATETIME"
        )

    for alteracao in alteracoes:
        db.session.execute(text(alteracao))
    if alteracoes:
        db.session.commit()
        db.session.execute(
            text(
                """
                UPDATE parceiro
                SET status = CASE
                    WHEN ativo = 1 THEN 'Ativo'
                    ELSE 'Bloqueado'
                END
                WHERE status IS NULL OR status = 'Pendente'
                """
            )
        )
        db.session.commit()


def _garantir_colunas_cupom():
    inspetor = inspect(db.engine)
    if "cupom" not in inspetor.get_table_names():
        return

    colunas = {coluna["name"] for coluna in inspetor.get_columns("cupom")}
    alteracoes = []
    atualizacoes = []

    if "limite_total" not in colunas:
        alteracoes.append("ALTER TABLE cupom ADD COLUMN limite_total INTEGER")
    if "limite_por_unidade" not in colunas:
        alteracoes.append(
            "ALTER TABLE cupom ADD COLUMN limite_por_unidade INTEGER NOT NULL DEFAULT 1"
        )
    if "data_criacao" not in colunas:
        alteracoes.append("ALTER TABLE cupom ADD COLUMN data_criacao DATETIME")
        atualizacoes.append(
            "UPDATE cupom SET data_criacao = datetime('now') WHERE data_criacao IS NULL"
        )
    if "data_update" not in colunas:
        alteracoes.append("ALTER TABLE cupom ADD COLUMN data_update DATETIME")
        atualizacoes.append(
            "UPDATE cupom SET data_update = datetime('now') WHERE data_update IS NULL"
        )
    if "data_desativacao" not in colunas:
        alteracoes.append("ALTER TABLE cupom ADD COLUMN data_desativacao DATETIME")
    adicionou_contador = False
    if "total_resgatado" not in colunas:
        alteracoes.append(
            "ALTER TABLE cupom ADD COLUMN total_resgatado INTEGER NOT NULL DEFAULT 0"
        )
        adicionou_contador = True

    for alteracao in alteracoes:
        db.session.execute(text(alteracao))
    for atualizacao in atualizacoes:
        db.session.execute(text(atualizacao))
    if alteracoes or atualizacoes:
        db.session.commit()

    if adicionou_contador:
        # Backfill: contador atômico precisa refletir os resgates já
        # existentes, senão o limite_total poderia ser furado a partir daqui.
        db.session.execute(
            text(
                """
                UPDATE cupom
                SET total_resgatado = (
                    SELECT COUNT(*) FROM resgate_cupom
                    WHERE resgate_cupom.cupom_id = cupom.id
                )
                """
            )
        )
        db.session.commit()


def _garantir_tabela_agendamentos_mudanca():
    """Garante a tabela de agendamentos (db.create_all) e colunas novas via ALTER TABLE."""
    inspetor = inspect(db.engine)
    tabelas = inspetor.get_table_names()
    if "agendamentos_mudanca" not in tabelas:
        # Tabela nova: criada por db.create_all() a partir do model AgendamentoMudanca.
        return

    colunas = {
        coluna["name"] for coluna in inspetor.get_columns("agendamentos_mudanca")
    }
    alteracoes = []
    if "motivo_rejeicao" not in colunas:
        alteracoes.append(
            "ALTER TABLE agendamentos_mudanca ADD COLUMN motivo_rejeicao TEXT"
        )
    if "data_chegada" not in colunas:
        alteracoes.append(
            "ALTER TABLE agendamentos_mudanca ADD COLUMN data_chegada DATETIME"
        )
    adicionou_porteiro = False
    if "porteiro_id" not in colunas:
        alteracoes.append(
            "ALTER TABLE agendamentos_mudanca ADD COLUMN porteiro_id INTEGER"
        )
        adicionou_porteiro = True

    for alteracao in alteracoes:
        db.session.execute(text(alteracao))
    if alteracoes:
        db.session.commit()
    if adicionou_porteiro:
        db.session.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_agendamentos_mudanca_porteiro_id "
                "ON agendamentos_mudanca (porteiro_id)"
            )
        )
        db.session.commit()


def _garantir_colunas_encomendas():
    """Garante codigo_rastreio e foto_pacote em encomendas (SQLite legado)."""
    inspetor = inspect(db.engine)
    if "encomendas" not in inspetor.get_table_names():
        return

    colunas = {coluna["name"] for coluna in inspetor.get_columns("encomendas")}
    alteracoes = []

    if "codigo_rastreio" not in colunas:
        alteracoes.append(
            "ALTER TABLE encomendas ADD COLUMN codigo_rastreio VARCHAR(100)"
        )
    if "foto_pacote" not in colunas:
        alteracoes.append(
            "ALTER TABLE encomendas ADD COLUMN foto_pacote VARCHAR(255)"
        )

    for alteracao in alteracoes:
        db.session.execute(text(alteracao))
    if alteracoes:
        db.session.commit()


def _garantir_colunas_registros_acesso():
    """Garante porteiro_saida_id em registros_acesso (SQLite legado)."""
    inspetor = inspect(db.engine)
    if "registros_acesso" not in inspetor.get_table_names():
        return

    colunas = {coluna["name"] for coluna in inspetor.get_columns("registros_acesso")}
    if "porteiro_saida_id" not in colunas:
        db.session.execute(
            text("ALTER TABLE registros_acesso ADD COLUMN porteiro_saida_id INTEGER")
        )
        db.session.commit()
        db.session.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_registros_acesso_porteiro_saida_id "
                "ON registros_acesso (porteiro_saida_id)"
            )
        )
        db.session.commit()

    # Índice único parcial: impede duas "entradas abertas" simultâneas do
    # mesmo visitante sob concorrência. Só cria se não houver violações
    # herdadas (banco legado com uma corrida anterior a esta correção).
    duplicados = db.session.execute(
        text(
            """
            SELECT visitante_id, COUNT(*) AS total
            FROM registros_acesso
            WHERE data_saida IS NULL
            GROUP BY visitante_id
            HAVING COUNT(*) > 1
            """
        )
    ).fetchall()
    if duplicados:
        print(
            "AVISO: existem visitantes com mais de uma entrada em aberto — "
            "resolva manualmente antes que o índice único de proteção possa "
            "ser criado."
        )
    else:
        db.session.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_registro_acesso_aberto "
                "ON registros_acesso (visitante_id) WHERE data_saida IS NULL"
            )
        )
        db.session.commit()


def _garantir_colunas_multi_tenant():
    """Adiciona condominio_id (nullable) nas tabelas locais para transição SaaS."""
    inspetor = inspect(db.engine)
    tabelas = inspetor.get_table_names()
    tabelas_locais = (
        "usuarios",
        "unidades",
        "agendamentos_mudanca",
        "logs_auditoria",
    )

    for tabela in tabelas_locais:
        if tabela not in tabelas:
            continue
        colunas = {coluna["name"] for coluna in inspetor.get_columns(tabela)}
        if "condominio_id" in colunas:
            continue
        db.session.execute(
            text(f"ALTER TABLE {tabela} ADD COLUMN condominio_id INTEGER")
        )
        db.session.commit()
        db.session.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS ix_{tabela}_condominio_id "
                f"ON {tabela} (condominio_id)"
            )
        )
        db.session.commit()


def _garantir_coluna_slug_condominio():
    """Garante coluna slug na tabela condominio (SQLite legado)."""
    inspetor = inspect(db.engine)
    if "condominio" not in inspetor.get_table_names():
        return
    colunas = {coluna["name"] for coluna in inspetor.get_columns("condominio")}
    if "slug" in colunas:
        return
    db.session.execute(text("ALTER TABLE condominio ADD COLUMN slug VARCHAR(50)"))
    db.session.commit()
    db.session.execute(
        text("CREATE UNIQUE INDEX IF NOT EXISTS ix_condominio_slug ON condominio (slug)")
    )
    db.session.commit()


def _garantir_colunas_whitelabel():
    """Adiciona colunas de identidade visual em configuracao_condominio."""
    inspetor = inspect(db.engine)
    if "configuracao_condominio" not in inspetor.get_table_names():
        return

    colunas = {
        coluna["name"] for coluna in inspetor.get_columns("configuracao_condominio")
    }
    alteracoes = []
    if "cor_primaria" not in colunas:
        alteracoes.append(
            "ALTER TABLE configuracao_condominio "
            "ADD COLUMN cor_primaria VARCHAR(7) NOT NULL DEFAULT '#0d6efd'"
        )
    if "logo_filename" not in colunas:
        alteracoes.append(
            "ALTER TABLE configuracao_condominio ADD COLUMN logo_filename VARCHAR(255)"
        )

    for alteracao in alteracoes:
        db.session.execute(text(alteracao))
    if alteracoes:
        db.session.commit()


def _garantir_coluna_ativo_condominio():
    """Soft delete: garante coluna ativo em condominio e backfill True."""
    inspetor = inspect(db.engine)
    if "condominio" not in inspetor.get_table_names():
        return

    colunas = {coluna["name"] for coluna in inspetor.get_columns("condominio")}
    if "ativo" in colunas:
        return

    db.session.execute(
        text(
            "ALTER TABLE condominio ADD COLUMN ativo BOOLEAN NOT NULL DEFAULT 1"
        )
    )
    db.session.commit()
    db.session.execute(text("UPDATE condominio SET ativo = 1 WHERE ativo IS NULL"))
    db.session.commit()


def _seed_condominio_transicao():
    """
    Seed de transição multi-tenant:
    cria o Cliente Nº 1 se ainda não existir e faz backfill de condominio_id.
    """
    from app.models import Condominio, ConfiguracaoCondominio

    condominio = Condominio.query.order_by(Condominio.id).first()
    if condominio is None:
        condominio = Condominio(nome="PRP Condomínio", slug="prp")
        db.session.add(condominio)
        db.session.flush()
        db.session.add(ConfiguracaoCondominio(condominio_id=condominio.id))
        db.session.commit()

    # Garante slug do cliente legado "PRP Condomínio".
    prp = Condominio.query.filter_by(nome="PRP Condomínio").first()
    if prp is not None and not prp.slug:
        prp.slug = "prp"
        db.session.commit()
    elif condominio.slug is None and condominio.id == 1:
        condominio.slug = "prp"
        db.session.commit()

    condominio_id = condominio.id
    tabelas_backfill = (
        "unidades",
        "usuarios",
        "agendamentos_mudanca",
        "logs_auditoria",
    )
    inspetor = inspect(db.engine)
    tabelas = set(inspetor.get_table_names())

    for tabela in tabelas_backfill:
        if tabela not in tabelas:
            continue
        colunas = {coluna["name"] for coluna in inspetor.get_columns(tabela)}
        if "condominio_id" not in colunas:
            continue
        if tabela == "usuarios":
            # Super Admin da plataforma permanece sem tenant (condominio_id NULL).
            db.session.execute(
                text(
                    "UPDATE usuarios "
                    "SET condominio_id = :condominio_id "
                    "WHERE condominio_id IS NULL AND role != 'superadmin'"
                ),
                {"condominio_id": condominio_id},
            )
        else:
            db.session.execute(
                text(
                    f"UPDATE {tabela} "
                    "SET condominio_id = :condominio_id "
                    "WHERE condominio_id IS NULL"
                ),
                {"condominio_id": condominio_id},
            )
    db.session.commit()
    _seed_superadmin()


def _seed_superadmin():
    """Garante usuário padrão Super Admin da plataforma SaaS."""
    from app.models import Role, Usuario

    existente = Usuario.query.filter_by(role=Role.SUPERADMIN).first()
    if existente is not None:
        return

    if Usuario.query.filter_by(username="superadmin").first() is not None:
        return

    superadmin = Usuario(
        username="superadmin",
        role=Role.SUPERADMIN,
        condominio_id=None,
    )
    superadmin.set_password("admin123")
    db.session.add(superadmin)
    db.session.commit()


def _migrar_sindico_agrupamentos():
    """
    Migra bloco_responsavel legado (coluna SQLite) para SindicoAgrupamento (1:N).
    Usa SQL bruto porque a coluna foi removida do modelo SQLAlchemy.
    """
    from app.models import Condominio, SindicoAgrupamento

    inspetor = inspect(db.engine)
    if "usuarios" not in inspetor.get_table_names():
        return

    colunas = {coluna["name"] for coluna in inspetor.get_columns("usuarios")}
    if "bloco_responsavel" not in colunas:
        return

    condominio_padrao = Condominio.query.order_by(Condominio.id).first()
    rows = db.session.execute(
        text(
            "SELECT id, bloco_responsavel, condominio_id FROM usuarios "
            "WHERE role = 'sindico' AND bloco_responsavel IS NOT NULL"
        )
    ).fetchall()

    for row in rows:
        usuario_id = row[0]
        bloco_responsavel = (row[1] or "").strip()
        condominio_id = row[2] or (
            condominio_padrao.id if condominio_padrao is not None else None
        )
        if not bloco_responsavel or condominio_id is None:
            continue

        ja_possui = SindicoAgrupamento.query.filter_by(usuario_id=usuario_id).first()
        if ja_possui:
            continue

        db.session.add(
            SindicoAgrupamento(
                usuario_id=usuario_id,
                condominio_id=condominio_id,
                nome_agrupamento=bloco_responsavel,
            )
        )

    db.session.commit()


def _hex_para_rgb(hex_color):
    """Converte '#RRGGBB' em string 'r, g, b' para CSS --bs-primary-rgb."""
    valor = str(hex_color or "").strip().lstrip("#")
    if len(valor) != 6:
        return "13, 110, 253"
    try:
        r = int(valor[0:2], 16)
        g = int(valor[2:4], 16)
        b = int(valor[4:6], 16)
    except ValueError:
        return "13, 110, 253"
    return f"{r}, {g}, {b}"


def create_app(config=None):
    app = Flask(__name__)

    upload_logos = os.path.join(app.root_path, "static", "uploads", "logos")
    upload_parceiros = os.path.join(app.root_path, "static", "uploads", "parceiros")
    upload_ocorrencias = os.path.join(app.root_path, "static", "uploads", "ocorrencias")
    upload_encomendas = os.path.join(app.root_path, "static", "uploads", "encomendas")

    secret_key = os.environ.get("SECRET_KEY") or (config or {}).get("SECRET_KEY")
    if not secret_key:
        raise RuntimeError(
            "SECRET_KEY não definida. Configure a variável de ambiente SECRET_KEY "
            "(ex.: no arquivo .env) antes de iniciar a aplicação — nunca use um "
            "valor fixo no código, pois ele assina sessões e tokens de redefinição "
            "de senha. Gere um valor aleatório com: "
            'python -c "import secrets; print(secrets.token_hex(32))"'
        )

    app.config.from_mapping(
        SECRET_KEY=secret_key,
        SQLALCHEMY_DATABASE_URI=os.environ.get(
            "SQLALCHEMY_DATABASE_URI", "sqlite:///condominio.db"
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"pool_recycle": 280},
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        UPLOAD_LOGOS_FOLDER=upload_logos,
        UPLOAD_PARCEIROS_FOLDER=upload_parceiros,
        UPLOAD_OCORRENCIAS_FOLDER=upload_ocorrencias,
        UPLOAD_ENCOMENDAS_FOLDER=upload_encomendas,
    )

    if config:
        app.config.update(config)

    db.init_app(app)

    @app.context_processor
    def inject_nav_context():
        from app.auth import get_current_user, get_unidade_logada
        from app.models import (
            Condominio,
            EspacoComum,
            Notificacao,
            PerfilDestinoNotificacao,
            Reserva,
            Role,
        )

        usuario = get_current_user()
        unidade = get_unidade_logada()
        reservas_pendentes_count = 0
        condominio_ctx = None
        notificacoes_nao_lidas = 0
        notificacoes_habilitadas = False

        if usuario:
            query = Reserva.query.join(Reserva.espaco).filter(Reserva.status == "Pendente")
            if usuario.condominio_id:
                query = query.filter(EspacoComum.condominio_id == usuario.condominio_id)
            if usuario.role == "sindico":
                blocos_sindico = [
                    agrup.nome_agrupamento for agrup in usuario.agrupamentos
                ]
                if blocos_sindico:
                    reservas_pendentes_count = query.filter(
                        EspacoComum.bloco_vinculado.in_(blocos_sindico)
                    ).count()
            elif usuario.role in ("admin", "assistente"):
                reservas_pendentes_count = query.filter(
                    Reserva.espaco.has(gerenciado_por="admin")
                ).count()

            if usuario.condominio_id:
                condominio_ctx = usuario.condominio

            if usuario.role in (Role.PORTEIRO, Role.ADMIN, Role.SUPERADMIN):
                cid_notif = usuario.condominio_id
                if cid_notif:
                    notificacoes_habilitadas = True
                    notificacoes_nao_lidas = Notificacao.query.filter_by(
                        condominio_id=cid_notif,
                        perfil_destino=PerfilDestinoNotificacao.PORTARIA,
                        lida=False,
                    ).filter(Notificacao.unidade_id.is_(None)).count()
        elif unidade and unidade.condominio_id:
            condominio_ctx = unidade.condominio
            notificacoes_habilitadas = True
            notificacoes_nao_lidas = Notificacao.query.filter_by(
                condominio_id=unidade.condominio_id,
                unidade_id=unidade.id,
                perfil_destino=PerfilDestinoNotificacao.MORADOR,
                lida=False,
            ).count()

        # Fallback: slug do tenant na sessão (portas públicas).
        if condominio_ctx is None:
            from flask import session

            slug = session.get("tenant_slug") or session.get("cadastro_slug")
            if slug:
                condominio_ctx = Condominio.query.filter_by(slug=slug).first()

        cor_primaria = "#0d6efd"
        if (
            condominio_ctx
            and condominio_ctx.configuracao
            and condominio_ctx.configuracao.cor_primaria
        ):
            cor_primaria = condominio_ctx.configuracao.cor_primaria

        return {
            "sidebar_user": usuario,
            "sidebar_unidade": unidade,
            "reservas_pendentes_count": reservas_pendentes_count,
            "condominio": condominio_ctx,
            "cor_primaria_rgb": _hex_para_rgb(cor_primaria),
            "notificacoes_nao_lidas": notificacoes_nao_lidas,
            "notificacoes_habilitadas": notificacoes_habilitadas,
        }

    from app import routes

    routes.init_app(app)

    with app.app_context():
        from app import models  # noqa: F401

        db.create_all()
        # Garante condominio_id em bancos SQLite legados antes do backfill.
        _garantir_colunas_multi_tenant()
        _garantir_colunas_usuarios()
        _garantir_coluna_slug_condominio()
        _garantir_colunas_whitelabel()
        _garantir_coluna_ativo_condominio()
        _seed_condominio_transicao()
        _migrar_sindico_agrupamentos()
        _garantir_colunas_unidades()
        _garantir_colunas_pessoas()
        _garantir_colunas_reservas()
        _garantir_coluna_condominio_espacos_comuns()
        _garantir_colunas_parceiros()
        _garantir_colunas_cupom()
        _garantir_tabela_agendamentos_mudanca()
        _garantir_colunas_registros_acesso()
        _garantir_colunas_encomendas()

    _garantir_tabelas_parceiros(app)

    return app
