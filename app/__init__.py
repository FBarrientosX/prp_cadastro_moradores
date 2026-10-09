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
    if "senha_atualizada_em" not in colunas:
        db.session.execute(
            text("ALTER TABLE usuarios ADD COLUMN senha_atualizada_em DATETIME")
        )
        db.session.commit()
        colunas.add("senha_atualizada_em")

    alteracoes = []
    if "blocos_escopo" not in colunas:
        alteracoes.append(
            "ALTER TABLE usuarios ADD COLUMN blocos_escopo VARCHAR(120)"
        )
    if "perm_portaria" not in colunas:
        alteracoes.append(
            "ALTER TABLE usuarios ADD COLUMN perm_portaria BOOLEAN NOT NULL DEFAULT 0"
        )
    if "perm_reservas_geral" not in colunas:
        alteracoes.append(
            "ALTER TABLE usuarios ADD COLUMN perm_reservas_geral "
            "BOOLEAN NOT NULL DEFAULT 0"
        )
    if "perm_configuracoes" not in colunas:
        alteracoes.append(
            "ALTER TABLE usuarios ADD COLUMN perm_configuracoes "
            "BOOLEAN NOT NULL DEFAULT 0"
        )
    for sql in alteracoes:
        db.session.execute(text(sql))
    if alteracoes:
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
    if "atualizacao_pendente" not in colunas:
        alteracoes.append(
            "ALTER TABLE unidades ADD COLUMN atualizacao_pendente "
            "BOOLEAN NOT NULL DEFAULT 0"
        )
    if "documento_drive_id" not in colunas:
        alteracoes.append(
            "ALTER TABLE unidades ADD COLUMN documento_drive_id VARCHAR(100)"
        )
    if "documento_url" not in colunas:
        alteracoes.append(
            "ALTER TABLE unidades ADD COLUMN documento_url VARCHAR(500)"
        )
    if "documento2_drive_id" not in colunas:
        alteracoes.append(
            "ALTER TABLE unidades ADD COLUMN documento2_drive_id VARCHAR(100)"
        )
    if "documento2_url" not in colunas:
        alteracoes.append(
            "ALTER TABLE unidades ADD COLUMN documento2_url VARCHAR(500)"
        )
    if "documento_status" not in colunas:
        alteracoes.append(
            "ALTER TABLE unidades ADD COLUMN documento_status "
            "VARCHAR(20) NOT NULL DEFAULT 'Pendente'"
        )
    if "eh_setor_interno" not in colunas:
        alteracoes.append(
            "ALTER TABLE unidades ADD COLUMN eh_setor_interno "
            "BOOLEAN NOT NULL DEFAULT 0"
        )

    for alteracao in alteracoes:
        db.session.execute(text(alteracao))
    if alteracoes:
        db.session.commit()


def _garantir_unicidade_unidade_por_tenant():
    """Um mesmo bloco/apto pode existir em condomínios diferentes.

    O índice antigo era só (bloco, apartamento), gravado no CREATE TABLE.
    No SQLite isso exige recriar a tabela; no MySQL basta remover o índice.
    """
    inspetor = inspect(db.engine)
    if "unidades" not in inspetor.get_table_names():
        return

    if db.engine.dialect.name == "mysql":
        unicos = {
            item["name"] for item in inspetor.get_unique_constraints("unidades")
        }
        indexes = {item["name"] for item in inspetor.get_indexes("unidades")}
        if "uq_bloco_apartamento" in unicos or "uq_bloco_apartamento" in indexes:
            db.session.execute(text("ALTER TABLE unidades DROP INDEX uq_bloco_apartamento"))
            db.session.commit()
            inspetor = inspect(db.engine)
        if not any(
            set(item.get("column_names") or [])
            == {"condominio_id", "bloco", "apartamento"}
            for item in inspetor.get_indexes("unidades")
        ):
            db.session.execute(
                text(
                    "CREATE UNIQUE INDEX uq_condominio_bloco_apartamento "
                    "ON unidades (condominio_id, bloco, apartamento)"
                )
            )
            db.session.commit()
        return

    if "unidades_legadas" in inspetor.get_table_names():
        colunas_novas = [
            coluna["name"] for coluna in inspetor.get_columns("unidades")
        ]
        lista = ", ".join(colunas_novas)
        with db.engine.begin() as conexao:
            conexao.execute(text("PRAGMA foreign_keys=OFF"))
            conexao.execute(
                text(
                    f"INSERT INTO unidades ({lista}) "
                    f"SELECT {lista} FROM unidades_legadas"
                )
            )
            conexao.execute(text("DROP TABLE unidades_legadas"))
            for indice, coluna in (
                ("ix_unidades_bloco", "bloco"),
                ("ix_unidades_apartamento", "apartamento"),
                ("ix_unidades_condominio_id", "condominio_id"),
            ):
                conexao.execute(
                    text(
                        f"CREATE INDEX IF NOT EXISTS {indice} "
                        f"ON unidades ({coluna})"
                    )
                )
        return

    definicao = db.session.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name='unidades'")
    ).scalar()
    if not definicao or "uq_bloco_apartamento" not in definicao:
        return

    from app.models import Unidade

    colunas = [coluna["name"] for coluna in inspetor.get_columns("unidades")]
    lista = ", ".join(colunas)
    with db.engine.begin() as conexao:
        conexao.execute(text("PRAGMA foreign_keys=OFF"))
        for indice in (
            "ix_unidades_bloco",
            "ix_unidades_apartamento",
            "ix_unidades_condominio_id",
            "uq_condominio_bloco_apartamento",
            "uq_bloco_apartamento",
        ):
            conexao.execute(text(f"DROP INDEX IF EXISTS {indice}"))
        conexao.execute(text("ALTER TABLE unidades RENAME TO unidades_legadas"))
        Unidade.__table__.create(bind=conexao)
        conexao.execute(
            text(
                f"INSERT INTO unidades ({lista}) "
                f"SELECT {lista} FROM unidades_legadas"
            )
        )
        conexao.execute(text("DROP TABLE unidades_legadas"))


def garantir_setor_administracao(condominio):
    """Cria a unidade interna Administração se o condomínio ainda não tiver.

    Não grava contato inventado: usuário da equipe não tem telefone nem CPF.
    A unidade fica pronta para receber pessoas com eh_morador e interfone.
    """
    from secrets import token_urlsafe

    from werkzeug.security import generate_password_hash

    from app.models import StatusUnidade, Unidade
    from app.utils import BLOCO_SETORES, SETOR_ADMINISTRACAO

    if condominio is None or not condominio.id:
        return None
    setor = Unidade.query.filter_by(
        condominio_id=condominio.id,
        bloco=BLOCO_SETORES,
        apartamento=SETOR_ADMINISTRACAO,
    ).first()
    if setor is not None:
        if not setor.eh_setor_interno:
            setor.eh_setor_interno = True
        return setor
    setor = Unidade(
        condominio_id=condominio.id,
        bloco=BLOCO_SETORES,
        apartamento=SETOR_ADMINISTRACAO,
        password_hash=generate_password_hash(token_urlsafe(32)),
        status=StatusUnidade.APROVADA,
        eh_setor_interno=True,
    )
    db.session.add(setor)
    db.session.flush()
    return setor


def _garantir_setores_internos():
    from app.models import Condominio

    for condominio in Condominio.query.all():
        garantir_setor_administracao(condominio)
    db.session.commit()


def _backfill_notificacoes_ocorrencias_abertas():
    """Avisa a gestão sobre chamados já abertos que ainda não têm alerta."""
    from app.models import Notificacao, Ocorrencia, StatusOcorrencia
    from app.routes import _sinalizar_ocorrencia_para_gestao

    if "ocorrencias" not in inspect(db.engine).get_table_names():
        return
    if "notificacoes" not in inspect(db.engine).get_table_names():
        return
    abertas = Ocorrencia.query.filter_by(status=StatusOcorrencia.ABERTO).all()
    criou = False
    for ocorrencia in abertas:
        link = f"/admin/ocorrencias?foco={ocorrencia.id}"
        ja_existe = Notificacao.query.filter_by(
            condominio_id=ocorrencia.condominio_id,
            tipo="OCORRENCIA",
            link_destino=link,
        ).first()
        if ja_existe is not None:
            continue
        _sinalizar_ocorrencia_para_gestao(ocorrencia, nova=True)
        criou = True
    if criou:
        db.session.commit()


def _garantir_colunas_ocorrencias():
    """Parecer e competência em bancos já criados (create_all não altera tabela)."""
    inspetor = inspect(db.engine)
    if "ocorrencias" not in inspetor.get_table_names():
        return
    colunas = {coluna["name"] for coluna in inspetor.get_columns("ocorrencias")}
    alteracoes = []
    if "resposta" not in colunas:
        alteracoes.append("ALTER TABLE ocorrencias ADD COLUMN resposta TEXT")
    if "respondida_em" not in colunas:
        alteracoes.append("ALTER TABLE ocorrencias ADD COLUMN respondida_em DATETIME")
    if "respondida_por_id" not in colunas:
        alteracoes.append(
            "ALTER TABLE ocorrencias ADD COLUMN respondida_por_id INTEGER"
        )
    if "competencia" not in colunas:
        alteracoes.append(
            "ALTER TABLE ocorrencias ADD COLUMN competencia "
            "VARCHAR(20) NOT NULL DEFAULT 'bloco'"
        )
    adicionou_leitura = "lida_pela_gestao" not in colunas
    if adicionou_leitura:
        alteracoes.append(
            "ALTER TABLE ocorrencias ADD COLUMN lida_pela_gestao "
            "BOOLEAN NOT NULL DEFAULT 0"
        )
    if "ultima_interacao_em" not in colunas:
        alteracoes.append(
            "ALTER TABLE ocorrencias ADD COLUMN ultima_interacao_em DATETIME"
        )
    if "ultima_interacao_por" not in colunas:
        alteracoes.append(
            "ALTER TABLE ocorrencias ADD COLUMN ultima_interacao_por VARCHAR(20)"
        )
    for alteracao in alteracoes:
        db.session.execute(text(alteracao))
    if alteracoes:
        db.session.commit()
    if adicionou_leitura:
        db.session.execute(
            text(
                "UPDATE ocorrencias SET lida_pela_gestao = 0, "
                "ultima_interacao_por = 'MORADOR' "
                "WHERE status = 'Aberto'"
            )
        )
        db.session.execute(
            text(
                "UPDATE ocorrencias SET lida_pela_gestao = 1, "
                "ultima_interacao_por = 'GESTAO' "
                "WHERE status != 'Aberto'"
            )
        )
        db.session.commit()
    if "notificacoes" in inspetor.get_table_names():
        colunas_notif = {coluna["name"] for coluna in inspetor.get_columns("notificacoes")}
        alteracoes_notif = []
        if "link_destino" not in colunas_notif:
            alteracoes_notif.append(
                "ALTER TABLE notificacoes ADD COLUMN link_destino VARCHAR(255)"
            )
        if "tipo" not in colunas_notif:
            alteracoes_notif.append(
                "ALTER TABLE notificacoes ADD COLUMN tipo "
                "VARCHAR(30) NOT NULL DEFAULT 'GERAL'"
            )
        for alteracao in alteracoes_notif:
            db.session.execute(text(alteracao))
        if alteracoes_notif:
            db.session.commit()
    if hasattr(inspetor, "clear_cache"):
        inspetor.clear_cache()
    _backfill_notificacoes_ocorrencias_abertas()


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
    adicionou_status = False
    if "status" not in colunas:
        alteracoes.append(
            "ALTER TABLE pessoas ADD COLUMN status "
            "VARCHAR(20) NOT NULL DEFAULT 'Pendente'"
        )
        adicionou_status = True
    adicionou_eh_proprietario = "eh_proprietario" not in colunas
    if adicionou_eh_proprietario:
        alteracoes.append(
            "ALTER TABLE pessoas ADD COLUMN eh_proprietario "
            "BOOLEAN NOT NULL DEFAULT 0"
        )
    adicionou_eh_morador = "eh_morador" not in colunas
    if adicionou_eh_morador:
        alteracoes.append(
            "ALTER TABLE pessoas ADD COLUMN eh_morador BOOLEAN NOT NULL DEFAULT 1"
        )
    if "foto_perfil" not in colunas:
        alteracoes.append("ALTER TABLE pessoas ADD COLUMN foto_perfil VARCHAR(255)")
    if "foto_facial" not in colunas:
        alteracoes.append("ALTER TABLE pessoas ADD COLUMN foto_facial VARCHAR(255)")
    if "foto_atualizada_em" not in colunas:
        alteracoes.append(
            "ALTER TABLE pessoas ADD COLUMN foto_atualizada_em DATETIME"
        )

    for alteracao in alteracoes:
        db.session.execute(text(alteracao))
    if alteracoes:
        db.session.commit()

    if adicionou_eh_proprietario:
        # Quem já ocupava o imóvel com vínculo de dono continua morador
        # e passa a ser reconhecido também como proprietário.
        db.session.execute(
            text(
                """
                UPDATE pessoas
                SET eh_proprietario = 1
                WHERE vinculo = 'Proprietário'
                """
            )
        )
        db.session.commit()

    # Dono legal é quem está na lista de proprietários (eh_proprietario).
    # Vínculo "Proprietário" sem essa flag vira ocupante familiar.
    db.session.execute(
        text(
            """
            UPDATE pessoas
            SET vinculo = 'Morador'
            WHERE eh_proprietario = 0
              AND vinculo = 'Proprietário'
            """
        )
    )
    db.session.commit()

    if adicionou_status:
        # Moradores já existentes em unidades aprovadas/registradas
        # são tratados como aprovados; pendentes de cadastro ficam Pendente.
        db.session.execute(
            text(
                """
                UPDATE pessoas
                SET status = 'Aprovado'
                WHERE unidade_id IN (
                    SELECT id FROM unidades
                    WHERE status IN ('Aprovada', 'Registrada')
                )
                """
            )
        )
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
    if "lista_convidados" not in colunas:
        alteracoes.append("ALTER TABLE reservas ADD COLUMN lista_convidados TEXT")
    if "chaves_entregue_em" not in colunas:
        alteracoes.append("ALTER TABLE reservas ADD COLUMN chaves_entregue_em DATETIME")
    if "chaves_devolvida_em" not in colunas:
        alteracoes.append(
            "ALTER TABLE reservas ADD COLUMN chaves_devolvida_em DATETIME"
        )
    if "porteiro_entrega_chaves_id" not in colunas:
        alteracoes.append(
            "ALTER TABLE reservas ADD COLUMN porteiro_entrega_chaves_id INTEGER"
        )
    if "porteiro_devolucao_chaves_id" not in colunas:
        alteracoes.append(
            "ALTER TABLE reservas ADD COLUMN porteiro_devolucao_chaves_id INTEGER"
        )

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

    # MySQL não suporta índices parciais (CREATE UNIQUE INDEX ... WHERE).
    # ux_reserva_espaco_data_ativa ficava: UNIQUE (espaco_id, data_reserva)
    # WHERE status IN ('Pendente', 'Aprovada') — válido só no SQLite.
    # A exclusão de duplo-booking do módulo antigo ficava na aplicação.
    # A interface web dessa tabela foi removida; as tabelas reservas e
    # espacos_comuns permanecem no banco.
    colunas_finais = {
        coluna["name"] for coluna in inspect(db.engine).get_columns("reservas")
    }
    extras = []
    if "lista_convidados" not in colunas_finais:
        extras.append("ALTER TABLE reservas ADD COLUMN lista_convidados TEXT")
    if "chaves_entregue_em" not in colunas_finais:
        extras.append("ALTER TABLE reservas ADD COLUMN chaves_entregue_em DATETIME")
    if "chaves_devolvida_em" not in colunas_finais:
        extras.append("ALTER TABLE reservas ADD COLUMN chaves_devolvida_em DATETIME")
    if "porteiro_entrega_chaves_id" not in colunas_finais:
        extras.append(
            "ALTER TABLE reservas ADD COLUMN porteiro_entrega_chaves_id INTEGER"
        )
    if "porteiro_devolucao_chaves_id" not in colunas_finais:
        extras.append(
            "ALTER TABLE reservas ADD COLUMN porteiro_devolucao_chaves_id INTEGER"
        )
    for sql in extras:
        db.session.execute(text(sql))
    if extras:
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


def _garantir_coluna_ativo_espacos_comuns():
    """Soft disable: garante coluna ativo em espacos_comuns (bancos já existentes)."""
    inspetor = inspect(db.engine)
    if "espacos_comuns" not in inspetor.get_table_names():
        return

    colunas = {coluna["name"] for coluna in inspetor.get_columns("espacos_comuns")}
    if "ativo" in colunas:
        return

    db.session.execute(
        text(
            "ALTER TABLE espacos_comuns ADD COLUMN ativo BOOLEAN NOT NULL DEFAULT 1"
        )
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
    status_novo = "status" not in colunas
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
    if "descricao_vantagem" not in colunas:
        alteracoes.append("ALTER TABLE parceiro ADD COLUMN descricao_vantagem TEXT")
    if "cupom" not in colunas:
        alteracoes.append("ALTER TABLE parceiro ADD COLUMN cupom VARCHAR(80)")
    if "logo_drive_id" not in colunas:
        alteracoes.append(
            "ALTER TABLE parceiro ADD COLUMN logo_drive_id VARCHAR(100)"
        )
    if "logo_url" not in colunas:
        alteracoes.append("ALTER TABLE parceiro ADD COLUMN logo_url VARCHAR(500)")
    if "categoria_id" not in colunas:
        alteracoes.append("ALTER TABLE parceiro ADD COLUMN categoria_id INTEGER")
    if "link_catalogo_externo" not in colunas:
        alteracoes.append(
            "ALTER TABLE parceiro ADD COLUMN link_catalogo_externo VARCHAR(500)"
        )

    for alteracao in alteracoes:
        db.session.execute(text(alteracao))
    if alteracoes:
        db.session.commit()
    if status_novo:
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


def _garantir_colunas_produto_parceiro():
    """Foto do produto no Drive, em catálogos já criados."""
    inspetor = inspect(db.engine)
    if "produto_parceiro" not in inspetor.get_table_names():
        return
    colunas = {coluna["name"] for coluna in inspetor.get_columns("produto_parceiro")}
    if "imagem_drive_id" not in colunas:
        db.session.execute(
            text("ALTER TABLE produto_parceiro ADD COLUMN imagem_drive_id VARCHAR(100)")
        )
        db.session.commit()
    if "imagem_url" not in colunas:
        db.session.execute(
            text("ALTER TABLE produto_parceiro ADD COLUMN imagem_url VARCHAR(500)")
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
    if "data_termino" not in colunas:
        alteracoes.append(
            "ALTER TABLE agendamentos_mudanca ADD COLUMN data_termino DATETIME"
        )
    if "observacao_portaria" not in colunas:
        alteracoes.append(
            "ALTER TABLE agendamentos_mudanca ADD COLUMN observacao_portaria TEXT"
        )
    if "porteiro_termino_id" not in colunas:
        alteracoes.append(
            "ALTER TABLE agendamentos_mudanca ADD COLUMN porteiro_termino_id INTEGER"
        )

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
    """Garante colunas extras em encomendas (bancos já existentes)."""
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
    if "foto_entrega" not in colunas:
        alteracoes.append(
            "ALTER TABLE encomendas ADD COLUMN foto_entrega VARCHAR(255)"
        )
    if "data_entrega" not in colunas:
        alteracoes.append(
            "ALTER TABLE encomendas ADD COLUMN data_entrega DATETIME"
        )
    if "entregue_para" not in colunas:
        alteracoes.append(
            "ALTER TABLE encomendas ADD COLUMN entregue_para VARCHAR(200)"
        )
    if "tentativas_contato" not in colunas:
        alteracoes.append(
            "ALTER TABLE encomendas ADD COLUMN tentativas_contato "
            "INTEGER NOT NULL DEFAULT 1"
        )
    if "destinatario_telefone" not in colunas:
        alteracoes.append(
            "ALTER TABLE encomendas ADD COLUMN destinatario_telefone VARCHAR(20)"
        )
    if "whatsapp_notificado" not in colunas:
        alteracoes.append(
            "ALTER TABLE encomendas ADD COLUMN whatsapp_notificado "
            "BOOLEAN NOT NULL DEFAULT 0"
        )
    if "whatsapp_notificado_em" not in colunas:
        alteracoes.append(
            "ALTER TABLE encomendas ADD COLUMN whatsapp_notificado_em DATETIME"
        )
    if "whatsapp_modo_envio" not in colunas:
        alteracoes.append(
            "ALTER TABLE encomendas ADD COLUMN whatsapp_modo_envio VARCHAR(20)"
        )

    for alteracao in alteracoes:
        db.session.execute(text(alteracao))
    if alteracoes:
        db.session.commit()


def _garantir_colunas_registros_acesso():
    """Garante porteiro_saida_id e placa_veiculo em registros_acesso (legado)."""
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

    colunas = {coluna["name"] for coluna in inspetor.get_columns("registros_acesso")}
    if "placa_veiculo" not in colunas:
        db.session.execute(
            text(
                "ALTER TABLE registros_acesso ADD COLUMN placa_veiculo VARCHAR(10)"
            )
        )
        db.session.commit()

    # MySQL não suporta índices parciais (CREATE UNIQUE INDEX ... WHERE).
    # ux_registro_acesso_aberto ficava: UNIQUE (visitante_id) WHERE data_saida IS NULL
    # — válido só no SQLite. A trava de "uma entrada aberta por visitante"
    # é feita na aplicação antes do commit (portaria_acesso_entrada /
    # portaria_acesso_autorizada).
    pass


def _garantir_colunas_autorizacoes_acesso():
    """Garante placa_veiculo em autorizacoes_acesso (bancos já existentes)."""
    inspetor = inspect(db.engine)
    if "autorizacoes_acesso" not in inspetor.get_table_names():
        return

    colunas = {
        coluna["name"] for coluna in inspetor.get_columns("autorizacoes_acesso")
    }
    if "placa_veiculo" in colunas:
        return

    db.session.execute(
        text(
            "ALTER TABLE autorizacoes_acesso ADD COLUMN placa_veiculo VARCHAR(10)"
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


def _garantir_coluna_api_key_condominio():
    """Chave de API dos equipamentos, única por condomínio."""
    inspetor = inspect(db.engine)
    if "condominio" not in inspetor.get_table_names():
        return

    colunas = {coluna["name"] for coluna in inspetor.get_columns("condominio")}
    if "api_key" not in colunas:
        db.session.execute(
            text("ALTER TABLE condominio ADD COLUMN api_key VARCHAR(64)")
        )
        db.session.commit()
        inspetor.clear_cache()

    indices = {indice["name"] for indice in inspetor.get_indexes("condominio")}
    if "uq_condominio_api_key" not in indices:
        db.session.execute(
            text(
                "CREATE UNIQUE INDEX uq_condominio_api_key ON condominio (api_key)"
            )
        )
        db.session.commit()


def _garantir_colunas_agente_acesso():
    """Token do agente e tabela de controladoras em bancos já existentes."""
    from secrets import token_hex

    inspetor = inspect(db.engine)
    if "condominio" not in inspetor.get_table_names():
        return

    colunas = {coluna["name"] for coluna in inspetor.get_columns("condominio")}
    alterou = False
    if "agent_api_token" not in colunas:
        db.session.execute(
            text("ALTER TABLE condominio ADD COLUMN agent_api_token VARCHAR(64)")
        )
        alterou = True
    if "agent_ultimo_ping" not in colunas:
        db.session.execute(
            text("ALTER TABLE condominio ADD COLUMN agent_ultimo_ping DATETIME")
        )
        alterou = True
    if alterou:
        db.session.commit()

    faltando = db.session.execute(
        text(
            "SELECT id FROM condominio "
            "WHERE agent_api_token IS NULL OR agent_api_token = ''"
        )
    ).fetchall()
    for (condominio_id,) in faltando:
        db.session.execute(
            text(
                "UPDATE condominio SET agent_api_token = :token "
                "WHERE id = :id AND (agent_api_token IS NULL OR agent_api_token = '')"
            ),
            {"token": token_hex(24), "id": condominio_id},
        )
    if faltando:
        db.session.commit()

    inspetor = inspect(db.engine)
    indices = {indice["name"] for indice in inspetor.get_indexes("condominio")}
    if "uq_condominio_agent_api_token" not in indices:
        db.session.execute(
            text(
                "CREATE UNIQUE INDEX uq_condominio_agent_api_token "
                "ON condominio (agent_api_token)"
            )
        )
        db.session.commit()


def _garantir_colunas_livro_servico():
    """Colunas novas do livro de serviço em bancos já existentes."""
    inspetor = inspect(db.engine)
    tabelas = set(inspetor.get_table_names())
    alteracoes = []

    if "condominio" in tabelas:
        colunas = {coluna["name"] for coluna in inspetor.get_columns("condominio")}
        if "permitir_apoio" not in colunas:
            alteracoes.append(
                "ALTER TABLE condominio ADD COLUMN permitir_apoio "
                "BOOLEAN NOT NULL DEFAULT 0"
            )
        if "permitir_ronda" not in colunas:
            alteracoes.append(
                "ALTER TABLE condominio ADD COLUMN permitir_ronda "
                "BOOLEAN NOT NULL DEFAULT 0"
            )

    if "plantoes" in tabelas:
        colunas = {coluna["name"] for coluna in inspetor.get_columns("plantoes")}
        if "apoio_id" not in colunas:
            alteracoes.append(
                "ALTER TABLE plantoes ADD COLUMN apoio_id INTEGER"
            )
        if "ronda_id" not in colunas:
            alteracoes.append(
                "ALTER TABLE plantoes ADD COLUMN ronda_id INTEGER"
            )

    if "itens_checklist" in tabelas:
        colunas = {
            coluna["name"] for coluna in inspetor.get_columns("itens_checklist")
        }
        if "guarita_id" not in colunas:
            alteracoes.append(
                "ALTER TABLE itens_checklist ADD COLUMN guarita_id INTEGER"
            )

    for alteracao in alteracoes:
        db.session.execute(text(alteracao))
    if alteracoes:
        db.session.commit()


def _garantir_tabelas_areas_infracoes():
    """Cria as tabelas do módulo novo antes de qualquer consulta.

    Não mexe em `espacos_comuns` nem em `reservas`, que seguem em operação.
    """
    from app.models import AreaComum, ConvidadoReserva, Infracao, ReservaArea

    for modelo in (AreaComum, ReservaArea, ConvidadoReserva, Infracao):
        modelo.__table__.create(bind=db.engine, checkfirst=True)


def _garantir_colunas_dados_condominio():
    """Colunas fiscais, contato, endereço e governança em condomínios já existentes."""
    inspetor = inspect(db.engine)
    if "condominio" not in inspetor.get_table_names():
        return
    colunas = {coluna["name"] for coluna in inspetor.get_columns("condominio")}
    definicoes = (
        ("razao_social", "VARCHAR(200)"),
        ("plano", "VARCHAR(40) NOT NULL DEFAULT 'Profissional'"),
        ("fuso_horario", "VARCHAR(64) NOT NULL DEFAULT 'America/Sao_Paulo'"),
        ("criado_em", "DATETIME"),
        ("telefone_fixo", "VARCHAR(30)"),
        ("telefone_whatsapp", "VARCHAR(30)"),
        ("whatsapp_api_url", "VARCHAR(255)"),
        ("whatsapp_api_token", "VARCHAR(255)"),
        ("whatsapp_auto_encomendas", "BOOLEAN NOT NULL DEFAULT 1"),
        ("email_contato", "VARCHAR(120)"),
        ("cep", "VARCHAR(9)"),
        ("logradouro", "VARCHAR(200)"),
        ("numero", "VARCHAR(20)"),
        ("complemento", "VARCHAR(120)"),
        ("bairro", "VARCHAR(120)"),
        ("cidade", "VARCHAR(120)"),
        ("uf", "VARCHAR(2)"),
        ("tipo_divisao", "VARCHAR(20) NOT NULL DEFAULT 'bloco_apto'"),
        ("total_unidades_previsto", "INTEGER"),
        ("nome_responsavel_gestao", "VARCHAR(200)"),
        ("fim_mandato", "DATE"),
        ("horario_mudancas", "VARCHAR(200)"),
        ("logo_filename", "VARCHAR(255)"),
        ("regimento_filename", "VARCHAR(255)"),
        ("convencao_filename", "VARCHAR(255)"),
        ("exigir_cpf_convidados", "BOOLEAN NOT NULL DEFAULT 0"),
        ("dias_padrao_defesa_multa", "INTEGER NOT NULL DEFAULT 15"),
        ("aprovacao_automatica_reservas", "BOOLEAN NOT NULL DEFAULT 0"),
    )
    for nome, tipo in definicoes:
        if nome in colunas:
            continue
        db.session.execute(text(f"ALTER TABLE condominio ADD COLUMN {nome} {tipo}"))
    db.session.commit()
    db.session.execute(
        text(
            "UPDATE condominio SET criado_em = data_cadastro "
            "WHERE criado_em IS NULL"
        )
    )
    db.session.commit()


def _seed_dados_condominio_prp():
    """Preenche o cliente prp só nos campos ainda vazios."""
    from app.models import Condominio

    prp = Condominio.query.filter_by(slug="prp").first()
    if prp is None:
        return
    conhecidos = {
        "razao_social": "PARQUE RESIDENCIAL PIRAQUARA",
        "cnpj": "00.915.409/0001-38",
        "telefone_fixo": "(21) 2402-0202",
        "telefone_whatsapp": "(21) 99533-7518",
        "email_contato": "prpcondominioparqueresidencial@gmail.com",
        "cep": "21755-270",
        "logradouro": "R. Piraquara",
        "numero": "593",
        "bairro": "Realengo",
        "cidade": "Rio de Janeiro",
        "uf": "RJ",
    }
    alterou = False
    for campo, valor in conhecidos.items():
        atual = getattr(prp, campo)
        if atual is None or (isinstance(atual, str) and not atual.strip()):
            setattr(prp, campo, valor)
            alterou = True
    if alterou:
        db.session.commit()


def _garantir_colunas_financeiro():
    """Colunas financeiras do condomínio. Roda antes de qualquer Condominio.query."""
    inspetor = inspect(db.engine)
    if "condominio" not in inspetor.get_table_names():
        return
    colunas = {coluna["name"] for coluna in inspetor.get_columns("condominio")}
    definicoes = (
        ("fin_repasses_ativos", "BOOLEAN NOT NULL DEFAULT 1"),
        ("fin_multa_percentual", "FLOAT NOT NULL DEFAULT 2.0"),
        ("fin_juros_mensal", "FLOAT NOT NULL DEFAULT 1.0"),
        ("fin_indice_correcao", "VARCHAR(20) NOT NULL DEFAULT 'UFIR-RJ'"),
    )
    alterou = False
    for nome, tipo in definicoes:
        if nome in colunas:
            continue
        db.session.execute(text(f"ALTER TABLE condominio ADD COLUMN {nome} {tipo}"))
        alterou = True
    if alterou:
        db.session.commit()
    if hasattr(inspetor, "clear_cache"):
        inspetor.clear_cache()
    if "cobranca_unidade" not in inspetor.get_table_names():
        return
    colunas_cobranca = {
        coluna["name"] for coluna in inspetor.get_columns("cobranca_unidade")
    }
    definicoes_cobranca = (
        ("remessa_lote_id", "INTEGER"),
        ("status_banco", "VARCHAR(30) NOT NULL DEFAULT 'Nao Enviado'"),
        ("codigo_ocorrencia_banco", "VARCHAR(10)"),
        ("destinatario_tipo", "VARCHAR(40) NOT NULL DEFAULT 'Proprietário'"),
        ("situacao_juridica", "VARCHAR(40) NOT NULL DEFAULT 'Normal'"),
        ("notificacoes_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("anexos_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("retorno_cnab_id", "INTEGER"),
    )
    alterou_cobranca = False
    for nome, tipo in definicoes_cobranca:
        if nome in colunas_cobranca:
            continue
        db.session.execute(
            text(f"ALTER TABLE cobranca_unidade ADD COLUMN {nome} {tipo}")
        )
        alterou_cobranca = True
    if alterou_cobranca:
        db.session.commit()
    if "rateio_condominio" not in inspetor.get_table_names():
        return
    colunas_rateio = {
        coluna["name"] for coluna in inspetor.get_columns("rateio_condominio")
    }
    definicoes_rateio = (
        ("grupo_fracao_id", "INTEGER"),
        ("modo_rateio", "VARCHAR(20) NOT NULL DEFAULT 'VALOR_UNITARIO'"),
    )
    alterou_rateio = False
    for nome, tipo in definicoes_rateio:
        if nome in colunas_rateio:
            continue
        db.session.execute(text(f"ALTER TABLE rateio_condominio ADD COLUMN {nome} {tipo}"))
        alterou_rateio = True
    if alterou_rateio:
        db.session.commit()
    if hasattr(inspetor, "clear_cache"):
        inspetor.clear_cache()


def _garantir_colunas_planta_unidades():
    """Colunas da planta física. Roda antes de qualquer Unidade.query."""
    inspetor = inspect(db.engine)
    if "unidades" not in inspetor.get_table_names():
        return
    colunas = {coluna["name"] for coluna in inspetor.get_columns("unidades")}
    definicoes = (
        ("criada_pela_admin", "BOOLEAN NOT NULL DEFAULT 1"),
        ("conta_reivindicada", "BOOLEAN NOT NULL DEFAULT 0"),
        ("cpf_pre_autorizado", "VARCHAR(20)"),
    )
    coluna_nova = "conta_reivindicada" not in colunas
    alterou = False
    for nome, tipo in definicoes:
        if nome in colunas:
            continue
        db.session.execute(text(f"ALTER TABLE unidades ADD COLUMN {nome} {tipo}"))
        alterou = True
    if alterou:
        db.session.commit()
    if hasattr(inspetor, "clear_cache"):
        inspetor.clear_cache()
    if not coluna_nova:
        return
    db.session.execute(
        text(
            "UPDATE unidades SET criada_pela_admin = 1, conta_reivindicada = 1 "
            "WHERE IFNULL(eh_setor_interno, 0) = 0 "
            "AND IFNULL(status, '') != 'Pré-Cadastro Admin'"
        )
    )
    db.session.execute(
        text(
            "UPDATE unidades SET criada_pela_admin = 1, conta_reivindicada = 1 "
            "WHERE IFNULL(eh_setor_interno, 0) = 1"
        )
    )
    db.session.commit()


def _seed_financeiro_prp():
    """Conta, fundos e plano de contas do PRP, só quando os dois ainda não existem."""
    from app.models import (
        Condominio,
        ContaBancaria,
        EscopoRepasse,
        FundoFinanceiro,
        PlanoConta,
        TipoPlanoConta,
    )

    inspetor = inspect(db.engine)
    tabelas = set(inspetor.get_table_names())
    if "conta_bancaria" not in tabelas or "plano_conta" not in tabelas:
        return
    if "fundo_financeiro" not in tabelas:
        return

    prp = Condominio.query.filter_by(slug="prp").first()
    if prp is None:
        return
    tem_conta = (
        ContaBancaria.query.filter_by(condominio_id=prp.id).first() is not None
    )
    tem_plano = PlanoConta.query.filter_by(condominio_id=prp.id).first() is not None
    if tem_conta or tem_plano:
        return

    conta = ContaBancaria(
        condominio_id=prp.id,
        nome_banco="Itaú",
        codigo_banco="341",
        agencia="0358",
        conta="43029",
        conta_dv="6",
        carteira="109",
        saldo_inicial=0.0,
        saldo_atual=0.0,
        principal=True,
        ativa=True,
    )
    db.session.add(conta)
    fundos = {
        "1": FundoFinanceiro(
            condominio_id=prp.id, codigo="1", nome="1 - CAIXA"
        ),
        "2": FundoFinanceiro(
            condominio_id=prp.id, codigo="2", nome="2 - FUNDO DE RESERVA"
        ),
        "3": FundoFinanceiro(
            condominio_id=prp.id, codigo="3", nome="3 - FUNDO DE OBRAS"
        ),
    }
    for fundo in fundos.values():
        db.session.add(fundo)
    db.session.flush()

    planos = (
        ("1.1.1.9", "TAXA ADM", TipoPlanoConta.RECEITA, "1", EscopoRepasse.ADM_GERAL),
        ("1.1.1.10", "TAXA EXTRA", TipoPlanoConta.RECEITA, "3", EscopoRepasse.BLOCO),
        ("1.1.1.11", "TAXA BLOCO", TipoPlanoConta.RECEITA, "1", EscopoRepasse.BLOCO),
        ("1.2.2", "Acordos", TipoPlanoConta.RECEITA, "1", EscopoRepasse.ADM_GERAL),
        (
            "2.1.1",
            "Despesas Operacionais / Fornecedores",
            TipoPlanoConta.DESPESA,
            "1",
            EscopoRepasse.ADM_GERAL,
        ),
        (
            "2.1.2",
            "Repasse / Despesas de Bloco",
            TipoPlanoConta.DESPESA,
            "1",
            EscopoRepasse.BLOCO,
        ),
    )
    for codigo, nome, tipo, fundo_codigo, escopo in planos:
        db.session.add(
            PlanoConta(
                condominio_id=prp.id,
                codigo=codigo,
                nome=nome,
                tipo=tipo,
                fundo_id=fundos[fundo_codigo].id,
                escopo_repasse=escopo,
            )
        )
    db.session.commit()


def _seed_guaritas_padrao():
    """Garante ao menos uma guarita ativa por condomínio (Portaria Principal)."""
    from app.models import Condominio, Guarita

    inspetor = inspect(db.engine)
    if "guaritas" not in inspetor.get_table_names():
        return

    for condominio in Condominio.query.filter_by(ativo=True).all():
        existe = Guarita.query.filter_by(condominio_id=condominio.id).first()
        if existe:
            continue
        db.session.add(
            Guarita(
                nome="Portaria Principal",
                condominio_id=condominio.id,
                ativa=True,
            )
        )
    db.session.commit()


def _seed_condominio_transicao():
    """
    Seed de transição multi-tenant:
    cria o Cliente Nº 1 se ainda não existir e faz backfill de condominio_id.
    """
    from app.models import Condominio, ConfiguracaoCondominio
    from app.utils import gerar_api_key

    condominio = Condominio.query.order_by(Condominio.id).first()
    if condominio is None:
        condominio = Condominio(
            nome="PRP Condomínio",
            slug="prp",
            api_key=gerar_api_key(),
        )
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


def _backfill_blocos_escopo_sindico():
    """Preenche blocos_escopo vazio a partir dos agrupamentos ou do bloco legado.

    Não sobrescreve um escopo já gravado. Flags de permissão nascem False
    no ALTER e não são alteradas aqui.
    """
    from app.utils import get_blocos, normalizar_bloco_codigo

    inspetor = inspect(db.engine)
    tabelas = set(inspetor.get_table_names())
    if "usuarios" not in tabelas:
        return
    colunas = {coluna["name"] for coluna in inspetor.get_columns("usuarios")}
    if "blocos_escopo" not in colunas:
        return

    validos = set(get_blocos())

    def _codigos(texto):
        saida = []
        for parte in str(texto or "").replace(";", ",").split(","):
            codigo = normalizar_bloco_codigo(parte.strip())
            if codigo in validos and codigo not in saida:
                saida.append(codigo)
        return saida

    por_usuario = {}
    if "sindico_agrupamento" in tabelas:
        linhas = db.session.execute(
            text(
                "SELECT usuario_id, nome_agrupamento FROM sindico_agrupamento "
                "ORDER BY id"
            )
        ).fetchall()
        for usuario_id, nome in linhas:
            por_usuario.setdefault(usuario_id, [])
            for codigo in _codigos(nome):
                if codigo not in por_usuario[usuario_id]:
                    por_usuario[usuario_id].append(codigo)

    for usuario_id, codigos in por_usuario.items():
        if not codigos:
            continue
        db.session.execute(
            text(
                "UPDATE usuarios SET blocos_escopo = :escopo "
                "WHERE id = :id AND role = 'sindico' "
                "AND (blocos_escopo IS NULL OR blocos_escopo = '')"
            ),
            {"escopo": ",".join(codigos), "id": usuario_id},
        )

    for coluna_legada in ("bloco_responsavel", "bloco"):
        if coluna_legada not in colunas:
            continue
        linhas = db.session.execute(
            text(
                "SELECT id, "
                + coluna_legada
                + " FROM usuarios "
                "WHERE role = 'sindico' "
                "AND (blocos_escopo IS NULL OR blocos_escopo = '') "
                "AND "
                + coluna_legada
                + " IS NOT NULL AND "
                + coluna_legada
                + " != ''"
            )
        ).fetchall()
        for usuario_id, bloco in linhas:
            codigos = _codigos(bloco)
            if not codigos:
                continue
            db.session.execute(
                text(
                    "UPDATE usuarios SET blocos_escopo = :escopo "
                    "WHERE id = :id AND (blocos_escopo IS NULL OR blocos_escopo = '')"
                ),
                {"escopo": ",".join(codigos), "id": usuario_id},
            )

    db.session.commit()


def _garantir_tabelas_caixa():
    """Livro-caixa avulso e fechamento mensal. Roda no boot antes das consultas."""
    existentes = set(inspect(db.engine).get_table_names())
    if "lancamento_caixa_avulso" not in existentes:
        db.session.execute(
            text(
                """
                CREATE TABLE lancamento_caixa_avulso (
                    id INTEGER PRIMARY KEY,
                    condominio_id INTEGER NOT NULL,
                    conta_bancaria_id INTEGER NOT NULL,
                    plano_conta_id INTEGER,
                    fundo_id INTEGER NOT NULL,
                    tipo VARCHAR(30) NOT NULL,
                    fundo_destino_id INTEGER,
                    competencia VARCHAR(7) NOT NULL,
                    data_lancamento DATE NOT NULL,
                    descricao VARCHAR(200) NOT NULL,
                    bloco_escopo VARCHAR(20) NOT NULL DEFAULT 'GERAL',
                    valor FLOAT NOT NULL DEFAULT 0.0,
                    criado_por VARCHAR(80) NOT NULL DEFAULT '',
                    criado_em DATETIME
                )
                """
            )
        )
        db.session.commit()
    if "fechamento_mensal" not in set(inspect(db.engine).get_table_names()):
        db.session.execute(
            text(
                """
                CREATE TABLE fechamento_mensal (
                    id INTEGER PRIMARY KEY,
                    condominio_id INTEGER NOT NULL,
                    competencia VARCHAR(7) NOT NULL,
                    fechado BOOLEAN NOT NULL DEFAULT 1,
                    saldo_inicial_mes FLOAT NOT NULL DEFAULT 0.0,
                    total_receitas FLOAT NOT NULL DEFAULT 0.0,
                    total_despesas FLOAT NOT NULL DEFAULT 0.0,
                    total_repasses FLOAT NOT NULL DEFAULT 0.0,
                    saldo_final_mes FLOAT NOT NULL DEFAULT 0.0,
                    resumo_snapshot_json TEXT,
                    fechado_por VARCHAR(80) NOT NULL DEFAULT '',
                    fechado_em DATETIME,
                    motivo_reabertura VARCHAR(300),
                    CONSTRAINT uq_fechamento_competencia_tenant
                        UNIQUE (condominio_id, competencia)
                )
                """
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


def _garantir_tabelas_medidores():
    """Tabelas de medidores. Roda no boot antes das consultas de negócio."""
    inspetor = inspect(db.engine)
    existentes = set(inspetor.get_table_names())
    enunciados = []
    if "medidor_config" not in existentes:
        enunciados.append(
            """
            CREATE TABLE medidor_config (
                id INTEGER PRIMARY KEY,
                condominio_id INTEGER NOT NULL,
                titulo VARCHAR(160) NOT NULL,
                tipo_recurso VARCHAR(20) NOT NULL DEFAULT 'AGUA',
                unidade_medida VARCHAR(10) NOT NULL DEFAULT 'm³',
                nivel_medicao VARCHAR(20) NOT NULL DEFAULT 'POR_UNIDADE',
                bloco_vinculado VARCHAR(20) NOT NULL DEFAULT 'GERAL',
                modo_calculo VARCHAR(30) NOT NULL DEFAULT 'METRAGEM',
                tarifa_unitaria FLOAT NOT NULL DEFAULT 0.0,
                taxa_fixa_minima FLOAT NOT NULL DEFAULT 0.0,
                faixas_json TEXT,
                plano_conta_id INTEGER,
                fundo_id INTEGER,
                permitir_leitura_morador BOOLEAN NOT NULL DEFAULT 0,
                embutido_taxa_ordinaria BOOLEAN NOT NULL DEFAULT 1,
                ativo BOOLEAN NOT NULL DEFAULT 1,
                criado_em DATETIME
            )
            """
        )
    if "participante_medidor" not in existentes:
        enunciados.append(
            """
            CREATE TABLE participante_medidor (
                id INTEGER PRIMARY KEY,
                medidor_id INTEGER NOT NULL,
                unidade_id INTEGER,
                identificador_ponto VARCHAR(160) NOT NULL,
                numero_serie_relogio VARCHAR(60),
                leitura_inicial FLOAT NOT NULL DEFAULT 0.0,
                credito_acumulado FLOAT NOT NULL DEFAULT 0.0,
                ativo BOOLEAN NOT NULL DEFAULT 1
            )
            """
        )
    if "ciclo_leitura_medidor" not in existentes:
        enunciados.append(
            """
            CREATE TABLE ciclo_leitura_medidor (
                id INTEGER PRIMARY KEY,
                condominio_id INTEGER NOT NULL,
                medidor_id INTEGER NOT NULL,
                competencia VARCHAR(7) NOT NULL,
                data_leitura DATE NOT NULL,
                valor_fatura_concessionaria FLOAT NOT NULL DEFAULT 0.0,
                consumo_total FLOAT NOT NULL DEFAULT 0.0,
                valor_total_apurado FLOAT NOT NULL DEFAULT 0.0,
                status VARCHAR(20) NOT NULL DEFAULT 'Em Aberto',
                observacoes TEXT,
                criado_em DATETIME,
                CONSTRAINT uq_ciclo_medidor_competencia UNIQUE (medidor_id, competencia)
            )
            """
        )
    if "item_leitura_medidor" not in existentes:
        enunciados.append(
            """
            CREATE TABLE item_leitura_medidor (
                id INTEGER PRIMARY KEY,
                ciclo_id INTEGER NOT NULL,
                participante_id INTEGER NOT NULL,
                unidade_id INTEGER,
                leitura_anterior FLOAT NOT NULL DEFAULT 0.0,
                leitura_atual FLOAT,
                reiniciada BOOLEAN NOT NULL DEFAULT 0,
                consumo_apurado FLOAT NOT NULL DEFAULT 0.0,
                credito_abatido FLOAT NOT NULL DEFAULT 0.0,
                consumo_final FLOAT NOT NULL DEFAULT 0.0,
                valor_calculado FLOAT NOT NULL DEFAULT 0.0,
                foto_relogio VARCHAR(120),
                enviado_pelo_morador BOOLEAN NOT NULL DEFAULT 0,
                data_envio_morador DATETIME,
                alerta_anomalia BOOLEAN NOT NULL DEFAULT 0,
                lancamento_gerado BOOLEAN NOT NULL DEFAULT 0,
                cobranca_id INTEGER
            )
            """
        )
    if enunciados:
        for enunciado in enunciados:
            db.session.execute(text(enunciado))
        db.session.commit()
        if hasattr(inspetor, "clear_cache"):
            inspetor.clear_cache()
    if "medidor_config" not in set(inspect(db.engine).get_table_names()):
        return
    colunas = {coluna["name"] for coluna in inspect(db.engine).get_columns("medidor_config")}
    if "embutido_taxa_ordinaria" in colunas:
        return
    db.session.execute(
        text(
            "ALTER TABLE medidor_config ADD COLUMN "
            "embutido_taxa_ordinaria BOOLEAN NOT NULL DEFAULT 1"
        )
    )
    db.session.commit()


def _garantir_tabela_orcamento():
    """Previsão orçamentária. Roda no boot antes das consultas de negócio."""
    if "previsao_orcamentaria" in set(inspect(db.engine).get_table_names()):
        return
    db.session.execute(
        text(
            """
            CREATE TABLE previsao_orcamentaria (
                id INTEGER PRIMARY KEY,
                condominio_id INTEGER NOT NULL,
                ano INTEGER NOT NULL,
                plano_conta_id INTEGER NOT NULL,
                bloco_escopo VARCHAR(20) NOT NULL DEFAULT 'GERAL',
                valores_mensais_json TEXT,
                valor_anual_total FLOAT NOT NULL DEFAULT 0.0,
                observacoes VARCHAR(300),
                atualizado_em DATETIME,
                CONSTRAINT uq_previsao_conta_escopo
                    UNIQUE (condominio_id, ano, plano_conta_id, bloco_escopo)
            )
            """
        )
    )
    db.session.commit()


def create_app(config=None):
    app = Flask(__name__)

    upload_logos = os.path.join(app.root_path, "static", "uploads", "logos")
    upload_parceiros = os.path.join(app.root_path, "static", "uploads", "parceiros")
    upload_ocorrencias = os.path.join(app.root_path, "static", "uploads", "ocorrencias")
    upload_encomendas = os.path.join(app.root_path, "static", "uploads", "encomendas")
    upload_documentos = os.path.join(app.root_path, "static", "uploads", "documentos")
    upload_faciais = os.path.join(app.root_path, "static", "uploads", "faciais")
    upload_financeiro = os.path.join(app.root_path, "static", "uploads", "financeiro")
    upload_medidores = os.path.join(app.root_path, "static", "uploads", "medidores")
    upload_infracoes = os.path.join(app.root_path, "static", "uploads", "infracoes")

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
        SQLALCHEMY_ENGINE_OPTIONS={"pool_recycle": 280, "pool_pre_ping": True},
        MAX_CONTENT_LENGTH=10 * 1024 * 1024, 
        UPLOAD_LOGOS_FOLDER=upload_logos,
        UPLOAD_PARCEIROS_FOLDER=upload_parceiros,
        UPLOAD_OCORRENCIAS_FOLDER=upload_ocorrencias,
        UPLOAD_ENCOMENDAS_FOLDER=upload_encomendas,
        UPLOAD_DOCUMENTOS_FOLDER=upload_documentos,
        UPLOAD_FACIAIS_FOLDER=upload_faciais,
        UPLOAD_FINANCEIRO_FOLDER=upload_financeiro,
        UPLOAD_MEDIDORES_FOLDER=upload_medidores,
        UPLOAD_INFRACOES_FOLDER=upload_infracoes,
    )

    if config:
        app.config.update(config)

    os.makedirs(app.config["UPLOAD_DOCUMENTOS_FOLDER"], exist_ok=True)
    os.makedirs(app.config["UPLOAD_LOGOS_FOLDER"], exist_ok=True)
    os.makedirs(app.config["UPLOAD_FACIAIS_FOLDER"], exist_ok=True)
    os.makedirs(app.config["UPLOAD_FINANCEIRO_FOLDER"], exist_ok=True)
    os.makedirs(app.config["UPLOAD_MEDIDORES_FOLDER"], exist_ok=True)
    os.makedirs(app.config["UPLOAD_INFRACOES_FOLDER"], exist_ok=True)

    @app.template_global()
    def foto_facial_url(pessoa):
        """URL da foto facial já padronizada, ou None se o morador ainda não enviou."""
        from flask import url_for
        from app.utils import nome_foto_facial_seguro

        nome = nome_foto_facial_seguro(getattr(pessoa, "foto_facial", None))
        if not nome:
            return None
        return url_for("static", filename=f"uploads/faciais/{nome}")

    @app.template_global()
    def logo_publica(condominio):
        """Nome seguro da logo do condomínio, com fallback para o white-label."""
        nome = None
        if condominio is not None:
            proprio = getattr(condominio, "logo_filename", None)
            if proprio:
                nome = proprio
            else:
                config_condo = getattr(condominio, "configuracao", None)
                if config_condo is not None:
                    nome = getattr(config_condo, "logo_filename", None)
        if not isinstance(nome, str):
            return None
        base = os.path.basename(nome)
        if not base or base != nome or ".." in base:
            return None
        return base

    db.init_app(app)

    from app.utils import tempo_relativo

    app.add_template_filter(tempo_relativo, "tempo_relativo")

    @app.context_processor
    def inject_nav_context():
        from app.auth import get_current_user, get_unidade_logada
        from app.models import (
            Condominio,
            Encomenda,
            Notificacao,
            PerfilDestinoNotificacao,
            Role,
            StatusEncomenda,
        )

        usuario = get_current_user()
        unidade = get_unidade_logada()
        encomendas_pendentes_count = 0
        condominio_ctx = None
        notificacoes_nao_lidas = 0
        notificacoes_habilitadas = False

        if usuario:
            if usuario.condominio_id:
                condominio_ctx = usuario.condominio

            if usuario.role == Role.ADMIN and usuario.condominio_id:
                from app.routes import _query_notificacoes

                notificacoes_habilitadas = True
                notificacoes_nao_lidas = (
                    _query_notificacoes(
                        PerfilDestinoNotificacao.ADMIN,
                        usuario.condominio_id,
                        None,
                        usuario,
                    )
                    .filter(Notificacao.lida.is_(False))
                    .count()
                )
            elif usuario.role == Role.SINDICO and usuario.condominio_id:
                from app.routes import _query_notificacoes

                notificacoes_habilitadas = True
                notificacoes_nao_lidas = (
                    _query_notificacoes(
                        PerfilDestinoNotificacao.SINDICO,
                        usuario.condominio_id,
                        None,
                        usuario,
                    )
                    .filter(Notificacao.lida.is_(False))
                    .count()
                )
            elif usuario.role in (Role.PORTEIRO, Role.SUPERADMIN):
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
            encomendas_pendentes_count = Encomenda.query.filter_by(
                unidade_id=unidade.id,
                condominio_id=unidade.condominio_id,
                status=StatusEncomenda.PENDENTE,
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

        leituras_medidor_abertas = 0
        if unidade and not usuario:
            from app.blueprints.financeiro_medidores import medidores_abertos_unidade

            leituras_medidor_abertas = medidores_abertos_unidade(unidade)

        qtd_ocorrencias_abertas = 0
        qtd_ocorrencias_nao_lidas = 0
        qtd_cadastros_pendentes = 0
        qtd_pendencias_condominio = 0
        ocorrencias_alerta = []
        if (
            usuario
            and usuario.condominio_id
            and usuario.role in (Role.ADMIN, Role.SINDICO)
        ):
            from sqlalchemy import or_

            from app.models import Ocorrencia, StatusOcorrencia, StatusUnidade, Unidade
            from app.routes import _blocos_codigo_sindico

            base_ocorrencias = Ocorrencia.query.join(
                Unidade, Ocorrencia.unidade_id == Unidade.id
            ).filter(
                Ocorrencia.condominio_id == usuario.condominio_id,
                Unidade.condominio_id == usuario.condominio_id,
                Unidade.eh_setor_interno.is_(False),
            )
            pendentes = Unidade.query.filter(
                Unidade.condominio_id == usuario.condominio_id,
                Unidade.status == StatusUnidade.PENDENTE,
                Unidade.eh_setor_interno.is_(False),
            )
            if usuario.role == Role.SINDICO:
                blocos = _blocos_codigo_sindico(usuario) or [""]
                base_ocorrencias = base_ocorrencias.filter(Unidade.bloco.in_(blocos))
                pendentes = pendentes.filter(Unidade.bloco.in_(blocos))
            qtd_ocorrencias_abertas = base_ocorrencias.filter(
                Ocorrencia.status == StatusOcorrencia.ABERTO
            ).count()
            qtd_ocorrencias_nao_lidas = base_ocorrencias.filter(
                or_(
                    Ocorrencia.status == StatusOcorrencia.ABERTO,
                    Ocorrencia.lida_pela_gestao.is_(False),
                )
            ).count()
            qtd_cadastros_pendentes = pendentes.count()
            qtd_pendencias_condominio = (
                qtd_ocorrencias_nao_lidas + qtd_cadastros_pendentes
            )
            resumo = (
                base_ocorrencias.filter(Ocorrencia.status == StatusOcorrencia.ABERTO)
                .order_by(Ocorrencia.created_at.desc())
                .limit(5)
                .all()
            )
            ocorrencias_alerta = [
                {
                    "unidade": item.unidade.identificador,
                    "titulo": item.titulo,
                    "categoria": item.categoria,
                    "quando": item.created_at,
                }
                for item in resumo
            ]

        return {
            "sidebar_user": usuario,
            "sidebar_unidade": unidade,
            "encomendas_pendentes_count": encomendas_pendentes_count,
            "condominio": condominio_ctx,
            "cor_primaria_rgb": _hex_para_rgb(cor_primaria),
            "notificacoes_nao_lidas": notificacoes_nao_lidas,
            "notificacoes_habilitadas": notificacoes_habilitadas,
            "leituras_medidor_abertas": leituras_medidor_abertas,
            "qtd_ocorrencias_abertas": qtd_ocorrencias_abertas,
            "qtd_ocorrencias_nao_lidas": qtd_ocorrencias_nao_lidas,
            "qtd_cadastros_pendentes": qtd_cadastros_pendentes,
            "qtd_pendencias_condominio": qtd_pendencias_condominio,
            "ocorrencias_alerta": ocorrencias_alerta,
        }

    from app import routes

    routes.init_app(app)

    with app.app_context():
        from app import models  # noqa: F401

        db.create_all()
        _garantir_tabelas_areas_infracoes()
        # Garante condominio_id em bancos SQLite legados antes do backfill.
        _garantir_colunas_multi_tenant()
        _garantir_colunas_usuarios()
        _garantir_coluna_slug_condominio()
        _garantir_colunas_whitelabel()
        _garantir_coluna_ativo_condominio()
        _garantir_coluna_api_key_condominio()
        _garantir_colunas_agente_acesso()
        _garantir_colunas_livro_servico()
        _garantir_colunas_dados_condominio()
        _garantir_colunas_financeiro()
        _garantir_tabelas_medidores()
        _garantir_tabela_orcamento()
        _garantir_tabelas_caixa()
        _garantir_colunas_planta_unidades()
        _seed_condominio_transicao()
        _seed_dados_condominio_prp()
        _seed_financeiro_prp()
        from app.financeiro_correcao import garantir_series_indices
        from app.financeiro_fracao import garantir_fracao_igualitaria
        from app.planta_unidades import completar_planta_prp

        garantir_series_indices()
        garantir_fracao_igualitaria()
        completar_planta_prp()
        _migrar_sindico_agrupamentos()
        _backfill_blocos_escopo_sindico()
        _garantir_colunas_unidades()
        _garantir_unicidade_unidade_por_tenant()
        _garantir_setores_internos()
        _garantir_colunas_pessoas()
        _garantir_colunas_ocorrencias()
        _garantir_colunas_reservas()
        _garantir_coluna_condominio_espacos_comuns()
        _garantir_coluna_ativo_espacos_comuns()
        _garantir_colunas_parceiros()
        _garantir_colunas_produto_parceiro()
        _garantir_colunas_cupom()
        _garantir_tabela_agendamentos_mudanca()
        _garantir_colunas_registros_acesso()
        _garantir_colunas_autorizacoes_acesso()
        _garantir_colunas_encomendas()
        _seed_guaritas_padrao()

    _garantir_tabelas_parceiros(app)

    return app
