# gestão de condomínios & clube de vantagens — documentação de arquitetura e produto

---

Este documento consolida, em uma única referência, a documentação técnica e de produto do SaaS multi-tenant de gestão de condomínios (Flask + SQLAlchemy + SQLite), incluindo seu módulo de clube de vantagens. Reúne visão de produto, arquitetura, modelo de dados, fluxos de negócio por papel de usuário, o funcionamento do clube de vantagens e um levantamento de riscos técnicos confirmados por verificação adversarial do código — com o status pós-sprint de segurança (itens **[CORRIGIDO]** e pendências remanescentes). Destina-se ao time interno como material de referência de arquitetura e produto.

## sumário

- [visão geral do produto e modelo de negócio](#visao-geral-produto)
- [arquitetura técnica](#arquitetura-tecnica)
- [modelo de dados](#modelo-de-dados)
- [fluxos de negócio](#fluxos-de-negocio)
- [clube de vantagens](#clube-de-vantagens)
- [integração com serviços externos e configuração de ambiente](#integracoes-operacao-limitacoes)
- [achados técnicos e riscos identificados](#achados-tecnicos-riscos)

---

<a id="visao-geral-produto"></a>

## gestão de condomínios com clube de vantagens — visão geral do produto

### o que o produto faz hoje

A aplicação é um SaaS multi-tenant de **gestão de condomínio**, com um segundo módulo, mais novo e menor, de **clube de vantagens** ligando o condomínio a parceiros comerciais locais. O tenant raiz é `Condominio` (`app/models.py`), identificado por slug único (`/c/<slug>/login`), com soft-delete (campo `ativo`) e configuração própria por cliente em `ConfiguracaoCondominio` — cor e logo (white-label), nomenclatura de bloco/unidade (`label_agrupamento`/`label_unidade`), se o condomínio usa agrupamentos e subsíndicos, e se o fluxo de aprovação de mudança é "Simples" ou "Dupla" (síndico + administração).

O núcleo funcional, hoje, cobre:

- **cadastro e ciclo de vida da unidade** — moradores se cadastram por bloco/apartamento, vinculam pessoas (proprietário, locatário, morador, com CPF/telefone/e-mail e possibilidade de menor de idade), veículos, e enviam documentos (comprovante de propriedade, contrato de locação) com upload e validação, incluindo integração com Google Drive (`app/drive_api.py`)

- **fluxo de aprovação** — unidade e moradores passam por estados (`Pendente` → `Aprovada`/`Registrada`/`Reprovada`), aprovados por síndico e/ou administração conforme a configuração do condomínio

- **reservas de área comum** — espaços (`EspacoComum`) com regras de dias de funcionamento, valor e gestão por bloco ou geral, e reservas (`Reserva`) com aprovação e controle de pagamento

- **agendamento de mudanças** — entrada/saída de unidade (`AgendamentoMudanca`) com fluxo de aprovação simples ou dupla e confirmação de chegada pela portaria

- **portaria** — controle de acesso de visitantes e prestadores com registro de entrada/saída imutável (`RegistroAcesso`), gestão de encomendas (recebimento, entrega, notificação ao morador) e confirmação de chegada de mudanças

- **autorizações de acesso** — o morador pré-autoriza um visitante esperado; a portaria dá baixa na chegada

- **ocorrências** — chamados no estilo helpdesk/kanban (manutenção, reclamação, sugestão, outros), tratados por síndico e/ou administração

- **notificações internas** — alertas bidirecionais entre portaria e morador

- **auditoria** — log de ações administrativas por condomínio/usuário (`LogAuditoria`)

- **clube de vantagens** — parceiros comerciais cadastram cupons; o morador resgata por unidade; parceiro, admin do condomínio e super admin acompanham métricas de resgate/validação

Vale registrar o estado de transição multi-tenant do banco: tabelas mais antigas (`Usuario`, `Unidade`, `EspacoComum`, `LogAuditoria`, `AgendamentoMudanca`) ainda têm `condominio_id` nullable, herança de um sistema legado single-tenant (o "PRP"); os módulos construídos já pensando em múltiplos condomínios (`Visitante`, `RegistroAcesso`, `Encomenda`, `AutorizacaoAcesso`, `Notificacao`, `Ocorrencia`) já nascem com `condominio_id` obrigatório.

### para quem

- **condomínios e suas administradoras** — o comprador/gestor do produto, que ganha um painel de administração por condomínio (documentos, unidades, usuários, ocorrências, mudanças)

- **síndicos** — atuam dentro do condomínio, com jurisdição que pode ser recortada por bloco/agrupamento (`SindicoAgrupamento`), aprovando cadastros e mudanças

- **moradores** — usuários finais que se autoatendem: cadastro, reservas, mudanças, autorizações de acesso, ocorrências, clube de vantagens

- **equipe de portaria** — operação diária de entrada/saída, encomendas e notificações

- **comércio local (parceiros comerciais)** — público-alvo secundário e mais incipiente: empresas que oferecem cupons de desconto aos moradores através do clube de vantagens

O produto está hoje centrado no condomínio como cliente; o comércio local é atendido por uma camada anexa, não o carro-chefe.

### os seis papéis de usuário

Os papéis são definidos em `Role` (`app/models.py`) e aplicados via decorators em `app/auth.py`; o morador não é um `Usuario` — ele autentica a **unidade** (`Unidade.password_hash`), um mecanismo de login separado.

- **super admin da plataforma** — dono da operação SaaS, sem `condominio_id` fixo. Opera fora do escopo de um tenant específico: cria condomínios, cadastra o primeiro admin de cada um, define white-label, ativa/desativa clientes (soft delete) e — no lado do clube de vantagens — é quem cadastra, edita, bloqueia e ativa parceiros comerciais na plataforma (`app/blueprints/superadmin.py`). É o único papel com visão cross-tenant.

- **admin do condomínio** — dono operacional de um condomínio (`condominio_id` obrigatório). Gerencia usuários da equipe, valida documentos e contratos de locação, define senha de síndico, acompanha e trata ocorrências (junto com o síndico) e mudanças (junto com o assistente), e é quem enxerga o clube de vantagens e seus indicadores do lado do condomínio (`admin_clube_vantagens`, `admin_clube_vantagens_analytics`).

- **assistente** — papel operacional mais restrito que o admin, também preso a um `condominio_id`. Nos routes protegidos por `admin_or_assistente_required`, cobre tarefas do dia a dia (registrar morador, alterar senha de unidade, excluir unidade, tratar mudanças) mas não acessa validação de documentos, gestão de usuários, senha de síndico nem o clube de vantagens — essas ficam exclusivas de `admin_required`.

- **síndico** — escopo dentro do condomínio, podendo ser limitado a um ou mais agrupamentos/blocos específicos via `SindicoAgrupamento` (arquitetura pensada para condomínios com subsíndicos). Aprova/reprova unidades e moradores do seu bloco, é a primeira instância do fluxo de aprovação dupla de mudanças, e trata ocorrências em conjunto com o admin (`admin_or_sindico_required`).

- **porteiro** — vinculado a um `condominio_id`, opera exclusivamente a portaria: entrada/saída de visitantes e prestadores, encomendas (receber, entregar, notificar morador) e confirmação de chegada de mudanças agendadas. Não acessa telas administrativas.

- **morador** — não é um `Usuario`, autentica a `Unidade` (bloco + apartamento + senha). Faz o próprio cadastro inicial, mantém pessoas/veículos da unidade, solicita reservas de área comum, agenda mudanças, cria autorizações de acesso para visitantes esperados, abre ocorrências, recebe notificações e resgata cupons do clube de vantagens.

Fora dos seis papéis "de condomínio" existe ainda o **parceiro comercial**, com sessão própria (`parceiro_id`) e login isolado (`parceiro_login`), sem relação com `condominio_id`/tenant — é o ator do lado comercial, não um papel do condomínio em si.

### a camada de parceiros comerciais: hoje e para onde pode evoluir

Hoje, `Parceiro` e `Cupom` são explicitamente de **escopo global** (comentário no próprio modelo: "sem condominio_id") — um único catálogo de parceiros e cupons é compartilhado por todos os condomínios da plataforma, não existe um marketplace segmentado por cliente. O isolamento por tenant só aparece de forma indireta e a posteriori: `ResgateCupom` se liga a `unidade_id`, e é a unidade que carrega o `condominio_id`, permitindo cortar métricas de resgate por condomínio depois do fato — mas não controlar, na entrada, quais parceiros um condomínio específico vê.

O ciclo de vida do parceiro também reflete esse estágio inicial: quem cadastra, aprova, bloqueia e reativa parceiros é exclusivamente o super admin da plataforma (não há autosserviço de onboarding comercial); o parceiro, uma vez ativo, loga em portal próprio para manter seu catálogo de cupons (título, descrição, prefixo de código, validade, limite total e por unidade) e ver as próprias métricas de resgate/validação. Do lado do condomínio, o admin tem uma tela de visualização e analytics do clube de vantagens; do lado do morador, existe uma página de resgate ligada à própria unidade.

Dado que hoje o fundador do produto descreve a gestão de condomínio como o foco imediato e o clube de vantagens como uma segunda frente ainda incipiente, os pontos de evolução mais evidentes na própria arquitetura atual (não implementados, mas coerentes com o desenho existente) seriam: dar a `Parceiro`/`Cupom` um recorte opcional por `condominio_id` (permitindo curadoria ou exclusividade por cliente, hoje impossível porque o modelo é global); abrir um fluxo de autosserviço/onboarding para parceiros (hoje dependente do super admin); e amadurecer os indicadores hoje expostos ao admin do condomínio em algo que sustente a venda do clube de vantagens como benefício percebido pelo síndico/morador, e não apenas um catálogo de cupons anexo ao produto principal.

---

<a id="arquitetura-tecnica"></a>

## arquitetura técnica

### stack

- **flask** como framework web — sem uso da classe `Blueprint` nativa (ver adiante)
- **sqlalchemy** (via `flask_sqlalchemy`) como orm, com `db = SQLAlchemy()` instanciado uma única vez em `app/__init__.py` e reaproveitado por todos os módulos
- **sqlite** como banco de dados (`instance/condominio.db` em produção), configurado por `SQLALCHEMY_DATABASE_URI` com fallback para `sqlite:///condominio.db` — preparado para trocar de engine via variável de ambiente `DATABASE_URL`, mas hoje 100% sqlite
- integrações externas síncronas: `smtplib` direto para e-mail (`app/email_service.py`, sem fila) e oauth "installed app" para o google drive (`app/drive_api.py`, com `token.json` local)
- jinja2 para os templates, com um `context_processor` global (`inject_nav_context`, registrado em `create_app`) que injeta em toda página o usuário logado, a unidade logada, o condomínio ativo e contadores de notificação/reservas pendentes

### app factory (`app/__init__.py`)

A aplicação é montada por `create_app(config=None)`, o padrão de app factory do flask: cria o `Flask(__name__)`, aplica a config (com override opcional via parâmetro `config`, usado em testes), chama `db.init_app(app)`, registra o `context_processor` de navegação, importa `app.routes` e chama `routes.init_app(app)` — que por sua vez registra o núcleo de rotas e os cinco módulos de `app/blueprints/`.

Depois disso, ainda dentro de `create_app`, roda a sequência de bootstrap dentro de um `app.app_context()`:

```python
db.create_all()
_garantir_colunas_multi_tenant()
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
```

`db.create_all()` cria as tabelas que ainda não existem a partir dos models — cobre bancos novos. Em seguida, cada `_garantir_colunas_*`/`_garantir_coluna_*` cuida de bancos **já existentes** (o `instance/condominio.db` do cliente real), ajustando o schema para o que o código atual espera.

### "migração manual" via `_garantir_colunas_*`

Cada uma dessas funções segue o mesmo formato: usa `sqlalchemy.inspect(db.engine)` para checar se a tabela existe e quais colunas ela já tem, monta uma lista de `ALTER TABLE ... ADD COLUMN ...` só para o que falta, executa com `text(...)` e dá `commit`. Exemplo em `_garantir_colunas_unidades`:

```python
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
    ...
    for alteracao in alteracoes:
        db.session.execute(text(alteracao))
    if alteracoes:
        db.session.commit()
```

Isso roda **a cada boot da aplicação**, não apenas uma vez — cada função é idempotente por construção (o `if coluna not in colunas` faz o próprio `ALTER TABLE` funcionar como guarda). Algumas indo além de simples `ADD COLUMN`: `_garantir_colunas_reservas` recria a tabela inteira (`RENAME TO ... _old` → `CREATE TABLE` novo sem a constraint `NOT NULL` antiga → `INSERT INTO ... SELECT` → `DROP TABLE ..._old`) quando precisa afrouxar uma coluna que era obrigatória, porque o sqlite não suporta `ALTER COLUMN` para relaxar `NOT NULL`. Funções como `_seed_condominio_transicao`, `_garantir_coluna_condominio_espacos_comuns` e `_migrar_sindico_agrupamentos` já misturam SQL bruto com fallback/backfill de dados (ex.: criar o "Cliente Nº 1" — o condomínio legado — se ainda não existir, e popular `condominio_id` nas linhas antigas que estavam `NULL`).

**Por que existe esse padrão em vez de Alembic.** O sistema nasceu single-tenant (um único condomínio, sem conceito de `Condominio`/tenant) e foi evoluindo em produção, com um banco sqlite real do cliente já em uso, para o modelo multi-tenant atual (`Condominio` como tenant raiz, `condominio_id` espalhado pelas tabelas). Não há um histórico de migrações versionado desde o início — o schema evoluiu organicamente junto com o código, e cada nova feature que exigia uma coluna nova ganhou sua própria função `_garantir_colunas_*` chamada no boot, em vez de uma migration formal. É, na prática, uma "migração" ad-hoc feita à mão, comentada em português explicando a intenção (ex.: `"""Isolamento multi-tenant: condominio_id em áreas comuns + backfill no cliente legado."""`).

O trade-off é explícito:
- **vantagem**: zero fricção de setup — não há Alembic para configurar, gerar revisões ou aplicar (`flask db upgrade`) em cada deploy; o próprio boot do app já deixa o schema em dia, o que importa muito num projeto pequeno com um único banco sqlite de produção e sem pipeline de deploy elaborado
- **custo**: não existe histórico de versões do schema (não dá para saber, olhando só o banco, "em que migration ele está"), não há `downgrade`, cada função precisa reimplementar manualmente a lógica de idempotência e de backfill que o Alembic dá de graça, e o código de bootstrap (`app/__init__.py`) cresce a cada mudança de schema, misturando definição de schema com lógica de dado (seeds, backfills) — o arquivo já concentra mais de uma dúzia dessas funções e tende a continuar crescendo enquanto o app não migrar para uma ferramenta de migração real

### estrutura de módulos pós-refatoração

Nesta sessão, `app/routes.py` (que concentrava todas as rotas) foi dividido: o núcleo compartilhado permanece em `app/routes.py` (~2325 linhas — cadastro de morador, login unificado morador+equipe via `tenant_login`, reservas de área comum, clube de vantagens do lado morador, mudanças do morador, autorizações de acesso, ocorrências do morador, notificações, e a maior parte dos helpers privados `_algo` usados por todos os módulos), e cinco módulos de rota foram extraídos para `app/blueprints/`, um por perfil de usuário:

```
app/
├── __init__.py            # app factory + _garantir_colunas_* + seeds
├── auth.py                 # sessão, decorators de autorização, resolução de condominio_id
├── models.py                # models SQLAlchemy (Condominio = tenant raiz)
├── routes.py                # núcleo compartilhado + init_app() + helpers privados _*
├── utils.py                  # slug, estrutura de blocos/apto, tokens, sanitização html, upload de logo
├── email_service.py          # smtplib síncrono (Gmail)
├── drive_api.py               # OAuth Google Drive (installed app + token.json)
├── blueprints/
│   ├── __init__.py            # vazio
│   ├── parceiro.py             # portal do parceiro comercial (register(app))
│   ├── superadmin.py            # painel do super admin da plataforma (register(app))
│   ├── sindico.py                # painel do síndico (register(app))
│   ├── admin.py                   # painel do admin/assistente local (register(app))
│   └── portaria.py                 # painel da portaria (register(app))
├── static/
│   ├── css/, js/
│   └── uploads/
│       ├── logos/, parceiros/, ocorrencias/, encomendas/
└── templates/
    ├── base.html, auth_base.html, login.html, tenant_login.html, ...
    ├── admin/                   # ex.: ocorrencias_kanban.html
    ├── morador/                  # ex.: ocorrencias.html
    ├── portaria/                  # ex.: acesso.html, dashboard.html, encomendas.html
    └── includes/                   # parciais reutilizáveis (ex.: sino_notificacoes.html)
```

Cada arquivo em `app/blueprints/` expõe uma função `register(app)` que chama `app.add_url_rule(...)` diretamente para cada rota, em vez de instanciar `flask.Blueprint` e usar `@bp.route(...)`. A escolha é deliberada e está documentada no docstring do próprio `admin.py`: "sem a classe Blueprint do Flask, apenas `register(app)` chamando `app.add_url_rule` para preservar os endpoints originais". O motivo concreto é que `Blueprint.route()` sempre registra o endpoint com o prefixo do nome do blueprint (`nome_do_blueprint.nome_da_view`), enquanto `app.add_url_rule(regra, "endpoint_sem_prefixo", view)` permite escolher o nome do endpoint livremente. Como as rotas foram apenas *movidas* de `routes.py` para os módulos — não reescritas — usar `Blueprint` mudaria o nome de endpoint de, por exemplo, `admin_ocorrencias_atualizar_status` para `admin.admin_ocorrencias_atualizar_status`, e isso quebraria todas as chamadas `url_for("admin_ocorrencias_atualizar_status", ...)` já espalhadas pelos templates (ex.: `app/templates/admin/ocorrencias_kanban.html` chama `url_for('admin_ocorrencias_atualizar_status', id=item.id)`; `app/templates/portaria/acesso.html` chama `url_for('portaria_acesso_autorizada', auth_id=item.id)`). Em `app/blueprints/parceiro.py`, o `register(app)` ilustra o padrão:

```python
def register(app):
    """Registra as rotas do Portal do Parceiro preservando os endpoints legados."""
    app.add_url_rule(
        "/parceiro", "parceiro_login", parceiro_login, methods=["GET", "POST"]
    )
    app.add_url_rule(
        "/parceiro/dashboard", "parceiro_dashboard", parceiro_dashboard, methods=["GET"]
    )
    ...
```

E o `init_app(app)` em `routes.py` importa e chama o `register` de cada módulo:

```python
def init_app(app):
    from app.blueprints import admin as admin_routes
    from app.blueprints import parceiro as parceiro_routes
    from app.blueprints import portaria as portaria_routes
    from app.blueprints import sindico as sindico_routes
    from app.blueprints import superadmin as superadmin_routes

    parceiro_routes.register(app)
    superadmin_routes.register(app)
    sindico_routes.register(app)
    admin_routes.register(app)
    portaria_routes.register(app)
```

### helpers privados compartilhados, importados sob demanda

Vários helpers com prefixo `_` continuam definidos em `app/routes.py` porque são usados por mais de um módulo (inclusive por partes do próprio `routes.py` que ainda não foram extraídas). Em vez de subir esses helpers para um módulo comum no topo da árvore, cada blueprint faz `from app.routes import _algo` **dentro da própria view function**, não no topo do arquivo. Isso evita import circular: `app/routes.py` só termina de definir tudo (incluindo `init_app`) quando `routes.init_app(app)` é chamado a partir de `create_app`, e é exatamente dentro desse `init_app` que os módulos de `app/blueprints/` são importados pela primeira vez — se o import de `_algo` estivesse no topo de `app/blueprints/admin.py`, o Python tentaria carregar `app.routes` (que ainda está no meio da própria execução) antes dele terminar. Adiando o `from app.routes import _algo` para dentro da view, o import só roda na hora da requisição, quando `app.routes` já está totalmente carregado.

Exemplos concretos encontrados no código:
- `app/blueprints/admin.py`, dentro da view `admin_mudancas_agendamento` (linha ~798): `from app.routes import _agendamento_do_tenant, _registrar_auditoria, _unidade_do_tenant, _validar_data_mudanca` — resolve o agendamento de mudança e a unidade escopados ao tenant do admin logado, e grava o log de auditoria, tudo com lógica que também é usada pelo módulo da portaria
- `app/blueprints/portaria.py`, espalhado por praticamente todas as views (ex.: linha ~156, ~178, ~237): `from app.routes import _condominio_id_portaria` — resolve o `condominio_id` do porteiro logado; e `_criar_notificacao`/`_registrar_auditoria`, usados junto em várias rotas (ex.: linha ~237: `from app.routes import _condominio_id_portaria, _criar_notificacao, _registrar_auditoria`) para registrar a notificação e a auditoria de uma ação de portaria
- `app/blueprints/sindico.py`, na view de dashboard (linha ~99): `from app.routes import _blocos_codigo_sindico, _label_agrupamentos_sindico` — traduz os agrupamentos/blocos sob responsabilidade do síndico logado (`SindicoAgrupamento`) para os códigos e rótulos usados nos filtros das telas

O próprio docstring de `app/blueprints/admin.py` documenta esse acoplamento remanescente: "várias funções privadas continuam em `app/routes.py` por serem compartilhadas com módulos ainda não extraídos (`_validar_data_mudanca` com `mudancas_morador`, `_unidade_do_tenant` / `_usuario_do_tenant` / `_agendamento_do_tenant` / `_ocorrencia_do_tenant` com portaria, `_registrar_auditoria` e `_label_agrupamentos_sindico` de forma ampla) — são só importadas aqui, dentro de cada view."

---

<a id="modelo-de-dados"></a>

## modelo de dados

O esquema é organizado em torno de um tenant raiz — `Condominio` — do qual derivam, direta ou indiretamente, todas as demais entidades do sistema.

### tenant raiz e configuração white-label

**`Condominio`** é o tenant raiz do SaaS: cada cliente da plataforma é um registro nessa tabela, identificado por um `slug` único (usado nas URLs e no login por tenant) e, opcionalmente, um `cnpj`. Não há hard delete — a desativação de um cliente é feita via soft-delete (campo `ativo`), preservando o histórico.

**`ConfiguracaoCondominio`** tem relação 1:1 com `Condominio` e concentra tudo que varia entre clientes sem exigir mudança de código — a camada de white-label e regras operacionais:
- **identidade visual**: `cor_primaria` (aplicada no tema da interface) e `logo_filename` (logo do condomínio).
- **nomenclatura customizável**: `label_agrupamento` e `label_unidade` permitem que cada condomínio chame seus agrupamentos e unidades pelo termo que preferir (por padrão, "Bloco" e "Apto"), além de `usa_agrupamentos` (liga/desliga o conceito de agrupamento) e `tem_subsindicos` (habilita síndicos por agrupamento).
- **fluxo de aprovação de mudança**: `fluxo_aprovacao_mudanca` alterna entre `"Simples"` (aprovação direta) e `"Dupla"` (aprovação em duas etapas — síndico e depois administração), refletido nos status intermediários de `AgendamentoMudanca`.

### identidade e acesso

- **`Usuario`** — conta de acesso da equipe operacional (super admin da plataforma, admin, assistente, síndico ou porteiro), com papel definido por `role` e vínculo a um condomínio.
- **`Unidade`** — a unidade habitacional (bloco + apartamento) que representa o "tenant do morador": tem login próprio (senha), passa por um fluxo de status (pendente → aprovada/registrada ou reprovada) e centraliza os documentos de comprovação de posse/locação.
- **`Pessoa`** — moradores vinculados a uma unidade, com vínculo (proprietário, locatário ou morador) e indicação de responsável/autorização de interfone.
- **`Veiculo`** — veículos cadastrados por unidade, para controle de acesso.
- **`SindicoAgrupamento`** — associação N:N entre um `Usuario` síndico e os agrupamentos (blocos) de um condomínio sob sua responsabilidade, substituindo o antigo campo único `bloco_responsavel` por um modelo 1:N.

### portaria

- **`Visitante`** — cadastro de visitante ou prestador de serviço, com documento único por condomínio (não global).
- **`RegistroAcesso`** — log transacional de entrada/saída de um visitante na portaria, associado à unidade visitada e ao(s) porteiro(s) responsável(is) pela entrada e pela saída.
- **`Encomenda`** — pacote recebido na portaria para uma unidade, com rastreamento de status (pendente/entregue) e dos porteiros de recebimento e entrega.
- **`AutorizacaoAcesso`** — autorização prévia criada pelo próprio morador para liberar a entrada de um visitante/prestador em data futura, com status próprio (pendente, concluída, cancelada).

### operação

- **`EspacoComum`** — área comum reservável (salão de festas, churrasqueira etc.), com regras de gestão (por síndico ou administração), vínculo opcional a um bloco específico e valor de reserva.
- **`Reserva`** — reserva de uma unidade sobre um `EspacoComum` em uma data, com status e valor pago.
- **`AgendamentoMudanca`** — solicitação de entrada/saída de mudança de uma unidade, com fluxo de status que varia conforme a configuração de aprovação simples ou dupla do condomínio, e registro do porteiro que recebeu a mudança.
- **`Ocorrencia`** — chamado de helpdesk/livro digital de ocorrências aberto por uma unidade, categorizado e com acompanhamento de status até resolução.
- **`Notificacao`** — alerta interno trocado entre portaria e moradores, direcionado por perfil de destino.
- **`LogAuditoria`** — trilha de auditoria de ações realizadas por usuários da equipe operacional.

### clube de vantagens

- **`Parceiro`** — empresa parceira cadastrada na plataforma, com seu próprio login, categoria, descrição e status de aprovação.
- **`Cupom`** — cupom de desconto/benefício oferecido por um parceiro, com prefixo de código, validade e limites de uso (total e por unidade).
- **`ResgateCupom`** — registro transacional do resgate de um cupom por uma unidade, com código único gerado e controle de utilização.

### estado de transição multi-tenant

O sistema nasceu single-tenant (um único condomínio, "PRP") e está em processo de evolução para multi-tenant completo, o que se reflete diretamente no esquema:

- nas tabelas **herdadas do sistema legado** — `usuarios`, `unidades`, `agendamentos_mudanca`, `logs_auditoria`, `espacos_comuns` — a coluna `condominio_id` é **nullable**, adicionada via `ALTER TABLE` em tempo de boot (funções `_garantir_colunas_*` em `app/__init__.py`), com comentários no código indicando que ainda "estão em transição".
- nas tabelas **construídas já pensando em multi-tenant** — `Visitante`, `RegistroAcesso`, `Encomenda`, `AutorizacaoAcesso`, `Notificacao`, `Ocorrencia` — `condominio_id` já é **NOT NULL** desde a criação, garantindo isolamento estrito por design.
- no boot da aplicação, a função de seed (`_seed_condominio_transicao`) garante a existência de um condomínio "Cliente Nº 1" com `slug="prp"` (criado automaticamente se a tabela `condominio` estiver vazia) e faz o **backfill** de `condominio_id = <id do PRP>` em todas as linhas legadas que ainda estejam com o campo nulo — exceto usuários com `role="superadmin"`, que permanecem propositalmente sem tenant, já que o super admin opera na camada da plataforma, acima de qualquer condomínio individual.

### isolamento global do clube de vantagens

`Parceiro` e `Cupom` são, por design, entidades **globais**: não possuem coluna `condominio_id`. Um parceiro comercial cadastrado na plataforma — e os cupons que ele emite — ficam visíveis e resgatáveis por moradores de **todos** os condomínios clientes, não apenas de um tenant específico. É um modelo de negócio deliberado (um único catálogo de benefícios compartilhado entre todos os clientes da SaaS), mas que quebra o isolamento estrito de dados que o restante do esquema persegue: não há como um condomínio "esconder" ou restringir catálogo de parceiros de outro.

O único ponto em que o tenant volta a aparecer nessa cadeia é indireto: `ResgateCupom` não tem `condominio_id` próprio, mas referencia `unidade_id` — e é através de `Unidade.condominio_id` que se torna possível segmentar métricas de resgate por condomínio (ex.: "quantos cupons o condomínio X resgatou"), ainda que o cupom e o parceiro em si continuem sendo recursos compartilhados por toda a base de clientes.

---

<a id="fluxos-de-negocio"></a>

## fluxos de negócio

### super admin da plataforma

Login isolado em `/superadmin/login`, restrito a usuários com `Role.SUPERADMIN` (sem vínculo a `condominio_id`). O dashboard (`superadmin_dashboard`) mostra contadores globais: total de condomínios, parceiros ativos e usuários (excluindo o próprio super admin).

- **onboarding de um novo condomínio (tenant)**: em `/superadmin/condominios` (POST), preenche nome, slug (normalizado e validado como único), CNPJ e a configuração operacional do tenant — rótulos customizáveis (`label_agrupamento`/`label_unidade`, ex. "Bloco"/"Apto" ou "Torre"/"Unidade"), se usa agrupamentos, se tem subsíndicos, o fluxo de aprovação de mudança ("Simples" ou "Dupla") e a cor primária/logo (white-label). Isso cria um `Condominio` + `ConfiguracaoCondominio` na mesma transação.
- em seguida cria o **primeiro admin local** (`superadmin_condominio_primeiro_admin`), um `Usuario` com `role=ADMIN` vinculado ao `condominio_id` recém-criado — é esse admin que depois cria o restante da equipe (assistente, síndico, porteiro).
- pode editar dados básicos e configuração depois (`superadmin_condominio_editar` — o slug é imutável) e a identidade visual isoladamente (`superadmin_condominio_whitelabel`).
- **soft delete de tenant**: `superadmin_condominio_desativar` marca `ativo=False` sem apagar nada; a porta `/c/<slug>/` passa a responder 403 com `condominio_suspenso.html` (`_resposta_condominio_inativo`). `superadmin_condominio_ativar` reverte.
- **clube de vantagens é catálogo global**, sem `condominio_id` — só o super admin cadastra/edita parceiros (`superadmin_parceiros_criar`/`superadmin_parceiro_editar`), com status inicial "Pendente" e senha padrão `senha123`. Pode bloquear um parceiro (`superadmin_parceiro_bloquear`), o que desativa em massa todos os cupons dele (`Cupom.query...update({"ativo": False})`), ou reativá-lo (`superadmin_parceiro_ativar`). Toda ação de bloqueio/reativação é auditada via `_registrar_auditoria`.

### admin/assistente do condomínio

Login pela aba "equipe" do login unificado do tenant (`tenant_login`, `/c/<slug>/login?tab=equipe`), autenticado por `username`+senha com `role` em ADMIN/ASSISTENTE/SÍNDICO/PORTEIRO e `condominio_id` da própria porta de entrada.

- **dashboard executivo** (`admin_dashboard`, só ADMIN): KPIs escopados por `condominio_id` — unidades registradas, aguardando registro (aprovadas mas sem doc validado), documentos pendentes (considerando também contrato de locação quando o responsável é locatário) — e três gráficos (cadastros por bloco, série temporal de 30 dias, proporção por status).
- **fila operacional** (`admin_index`, ADMIN ou ASSISTENTE): lista unidades "Aguardando registro" (já aprovadas pelo síndico) e "Finalizadas", além da lista de síndicos e de toda a equipe com acesso.
- **fluxo de validação documental do morador** (o elo entre aprovação do síndico e liberação plena do app): depois que o síndico aprova a unidade (`StatusUnidade.APROVADA`), ela cai na fila do admin, que confere o documento pessoal e/ou o contrato de locação enviados e marca cada um como `ENTREGUE` (`admin_validar_documento`, `admin_validar_contrato_locacao`, ou os dois juntos em `admin_validar_documentos`; pode também reverter manualmente via `admin_atualizar_status_documentos`). Só então marca a unidade como `REGISTRADA` (`admin_registrar`), único ponto que faz essa transição — fechando o onboarding.
- gestão de unidades: redefine senha da unidade (`admin_unidade_alterar_senha`), apaga cadastro por completo liberando a unidade para novo registro (`admin_excluir_unidade`, exclusivo de ADMIN), e mantém dados do proprietário para unidades alugadas (`admin_salvar_proprietario`).
- gestão de equipe: cria assistente/síndico/porteiro (`admin_criar_usuario` — síndico exige escolha de bloco responsável, que gera um `SindicoAgrupamento`), redefine senha de síndico (`admin_alterar_senha_sindico`) e revoga acessos (`admin_excluir_usuario`, não pode revogar a si mesmo, restrito a assistente/síndico/porteiro).
- **ocorrências**: kanban (`admin_ocorrencias`, compartilhado com o síndico via `admin_or_sindico_required`) com colunas Aberto/Em Andamento/Resolvido; a transição de status é feita em `admin_ocorrencias_atualizar_status`, protegida por `condominio_id` (anti-IDOR) e auditada.
- **mudanças (segunda instância de aprovação)**: quando o síndico aprova uma solicitação de mudança, ela vira `PENDENTE_ADMINISTRACAO`; o admin aprova definitivamente (`APROVADA`) ou rejeita com motivo obrigatório em `admin_mudancas`. O admin também pode cadastrar uma mudança já aprovada diretamente (bypass do fluxo do morador/síndico), respeitando a mesma regra de antecedência mínima de 3 dias e proibição de domingo (`_validar_data_mudanca`).
- **clube de vantagens (somente leitura)**: `admin_clube_vantagens` mostra analytics (cupons por parceiro, resgates por bloco, evolução, top unidades, taxa de conversão) escopados ao próprio condomínio — a mutação de parceiros/cupons é exclusiva do super admin.
- **reservas**: quando logado como ADMIN/ASSISTENTE, gerencia os espaços com `gerenciado_por="admin"` — aprova/recusa (`responder_reserva`), cria reserva já aprovada diretamente (`criar_reserva_gestao`), atualiza pagamento (`atualizar_pagamento_reserva`, que auto-aprova quando o valor pago atinge o valor da reserva) e cancela (`cancelar_reserva`).

### síndico

Login próprio por tenant (`/c/<slug>/sindico/login`), escopado por `condominio_id` **e** por jurisdição de bloco/agrupamento, guardada em `SindicoAgrupamento` (um síndico pode responder por um ou mais blocos).

- **dashboard** (`sindico_dashboard`): monta um mapa de todos os apartamentos dos blocos sob sua jurisdição, com status "Aguardando Morador" (sem cadastro), Pendente, Aprovada ou Registrada.
- **aprovação do cadastro inicial do morador** — o coração do fluxo do síndico, com três granularidades:
&nbsp;&nbsp;// `sindico_aprovar`: aprova a unidade inteira de uma vez (`PENDENTE` → `APROVADA`).
&nbsp;&nbsp;// `sindico_reprovar`: reprova a unidade inteira ainda pendente, **excluindo** o cadastro (a unidade volta a "Aguardando Morador").
&nbsp;&nbsp;// `sindico_reprovar_pessoa`: reprova/exclui **um morador específico** dentro de uma unidade pendente (exige motivo dentre 3 opções válidas), grava um aviso na tela da unidade (`_adicionar_notificacao_sindico`, campo `notificacao_sindico`) e envia e-mail de reprovação ao responsável.
&nbsp;&nbsp;// `sindico_validar_unidade`: fluxo granular completo numa única submissão — o síndico marca quais moradores da unidade reprova (motivo obrigatório para cada um) e aprova os demais; se sobrar ao menos um morador aprovado, a unidade vira `APROVADA` (e-mail de sucesso ou de "validação parcial" se houve reprovados); se **todos** forem reprovados, a unidade inteira é excluída.
- **mudanças** (`sindico_mudancas`): aprova (`PENDENTE_SINDICO` → `PENDENTE_ADMINISTRACAO`, repassando ao admin) ou rejeita com motivo as solicitações de mudança dos moradores dos seus blocos.
- **reservas**: gerencia os espaços vinculados ao seu agrupamento (`gerenciado_por="sindico"`) com as mesmas ações do admin (aprovar/recusar, criar direto, pagamento, cancelar), mas restrito à própria jurisdição (`_sindico_gerencia_bloco`).
- toda ação de aprovação/reprovação/mudança fica registrada em auditoria (`_registrar_auditoria`).

### porteiro

Login também pela aba "equipe" do `tenant_login`, com `role=PORTEIRO`. Dashboard (`portaria_dashboard`) mostra contadores ao vivo: visitantes no local, prestadores no local e encomendas pendentes.

**controle de acesso ponta a ponta**:
1. **entrada manual** (`portaria_acesso_entrada`): porteiro informa documento (normalizado/sem pontuação), nome, tipo (Visitante ou Prestador+empresa) e a unidade de destino. O sistema cria ou reaproveita o `Visitante` pelo documento, bloqueia uma segunda entrada se já houver uma em aberto para a mesma pessoa, grava o `RegistroAcesso` com horário local de São Paulo e o porteiro responsável, e **dispara notificação automática ao morador** da unidade ("Chegada na portaria — o visitante/prestador X acabou de entrar").
2. **entrada expressa via autorização prévia do morador** (`portaria_acesso_autorizada`): valida que existe uma `AutorizacaoAcesso` pendente para o dia, reaproveita/cria o `Visitante` pelo documento da autorização, cria o `RegistroAcesso`, marca a autorização como `CONCLUIDA` e dispara a mesma notificação ao morador.
3. **saída** (`portaria_acesso_saida`): fecha o `RegistroAcesso` em aberto (`data_saida` + `porteiro_saida_id`).

**encomendas ponta a ponta**:
1. **recebimento** (`portaria_encomendas_receber`): registra destinatário, transportadora, código de rastreio e foto do pacote (upload), status `PENDENTE`; notifica o morador ("Nova encomenda").
2. **lembrete** (`portaria_encomendas_notificar`): reenvia notificação, só permitido para encomendas ainda pendentes.
3. **entrega** (`portaria_encomendas_entregar`): marca `ENTREGUE`, registrando porteiro e horário.

- **mudanças**: `portaria_mudanca_chegar` registra a chegada do caminhão no dia agendado — só aceito se a mudança já estiver `APROVADA` (passou pelas duas aprovações) e for exatamente o dia marcado.
- todas as ações relevantes (entrada, saída, encomenda, chegada) são auditadas.

### morador (unidade)

**onboarding ponta a ponta**:
1. Acessa `/c/<slug>/login`, informa bloco+apartamento (`verificar_unidade`). Se a unidade não tem cadastro (ou estava `REPROVADA` — nesse caso é excluída na hora), é redirecionado a `cadastro_inicial`.
2. Preenche `cadastro_morador.html`: pessoas do domicílio (com vínculo Proprietário/Locatário/Morador), veículos, dados do proprietário quando o responsável é locatário, e define a senha da unidade. `salvar_cadastro` cria a `Unidade` com status `PENDENTE`.
3. Aguarda a aprovação do síndico do seu bloco (aprovação total, granular por pessoa, ou reprovação) — se reprovado, recebe e-mail com o motivo.
4. Uma vez `APROVADA`, o morador já consegue logar normalmente (login passa a exigir senha) e usar `atualizar_dados`; documento pessoal e/ou contrato de locação ficam pendentes até o admin validá-los.
5. O admin marca a unidade como `REGISTRADA` (`admin_registrar`), concluindo o onboarding.
6. A qualquer momento o morador pode reenviar/editar seus dados (`atualizar_dados`/`salvar_cadastro` em modo atualização). Alterações consideradas sensíveis — troca de responsável/proprietário, ou inclusão/remoção de morador ou veículo — fazem a unidade **voltar automaticamente para `PENDENTE`** (`_requer_nova_aprovacao_sindico`), reabrindo o ciclo de aprovação do síndico.

**uso do app depois de aprovado/registrado**:
- **clube de vantagens**: navega cupons ativos de parceiros (catálogo global), resgata respeitando `limite_total` da oferta e `limite_por_unidade`; o resgate gera um código único no formato `PRP-<BLOCO><APTO>-<PREFIXO>-<SUFIXO>`, usado depois na validação pelo parceiro.
- **reservas**: vê os espaços disponíveis para sua unidade (respeitando `apenas_moradores_bloco`/`bloco_vinculado`), solicita reserva (`Pendente`) e recebe e-mail quando o síndico/admin aprova ou recusa.
- **mudanças**: solicita entrada/saída com no mínimo 3 dias de antecedência e nunca aos domingos (`_validar_data_mudanca`); a solicitação nasce `PENDENTE_SINDICO` → aprovação do síndico → `PENDENTE_ADMINISTRACAO` → aprovação definitiva do admin → `APROVADA`. Pode cancelar enquanto ainda pendente.
- **autorizações de acesso**: pré-cadastra um visitante/prestador esperado com data prevista, o que notifica a portaria; no dia, o porteiro faz o check-in expresso a partir dessa autorização. O morador pode cancelar enquanto ainda estiver `PENDENTE`.
- **ocorrências**: abre chamados com categoria (Manutenção/Reclamação/Sugestão/Outros) e foto opcional; acompanha o andamento (Aberto/Em Andamento/Resolvido) conforme admin/síndico atualizam o kanban.
- **notificações**: recebe avisos de chegada na portaria, novas encomendas/lembretes e avisos do próprio síndico (removíveis da tela via `limpar_notificacao_sindico`).

### parceiro comercial

- **cadastro** é feito exclusivamente pelo super admin (`superadmin_parceiros_criar`), com status inicial `Pendente` e senha padrão `senha123`.
- **login isolado** em `/parceiro` (sessão própria `parceiro_id`, sem relação com `condominio_id`/tenant — é um catálogo compartilhado entre todos os condomínios).
- enquanto `status="Pendente"`, o dashboard exibe apenas a tela `parceiro_pendente.html`; o próprio parceiro precisa clicar para **ativar seu cadastro** (`parceiro_aprovar`, que muda status para `Ativo`) antes de operar cupons.
- uma vez `Ativo`: dashboard mostra métricas (cupons ativos, total de validações, histórico de resgates).
- **gestão de cupons**: cria (`parceiro_cupons_criar` — título, descrição em rich-text sanitizado, prefixo do código, validade, limite total e limite por unidade) e desativa permanentemente (`parceiro_cupons_desativar` — não há reversão).
- **validação do resgate**: recebe do morador (presencialmente ou por telefone) o código único gerado no resgate, digita em `parceiro_validar_codigo`; o sistema confere que o código pertence a este parceiro e está `Ativo`, e muda para `Utilizado`.
- mantém o próprio perfil comercial (nome, contato, categoria, descrição, redes sociais, logo) via `parceiro_perfil_editar`, e tem fluxo próprio de recuperação de senha por token (`parceiro_esqueci_senha`/`parceiro_redefinir_senha`).
- pode ser **bloqueado pelo super admin** (`status="Bloqueado"`), o que desativa em massa todos os seus cupons e impede login funcional (mensagem de suspensão); reativado depois via `superadmin_parceiro_ativar`.

---

<a id="clube-de-vantagens"></a>

## clube de vantagens

O clube de vantagens é o módulo que conecta parceiros comerciais (lojas, restaurantes, serviços) aos moradores dos condomínios atendidos pela plataforma, oferecendo cupons de desconto resgatáveis pela unidade e validados presencialmente pelo parceiro.

### cadastro e aprovação do parceiro — atenção a uma inconsistência de design

Hoje não existe autocadastro público de parceiro: quem cria a conta é sempre o **super admin**, em `/superadmin/parceiros` (`superadmin_parceiros_criar`, em `app/blueprints/superadmin.py`). Ao criar o registro, o parceiro nasce com `status = "Pendente"` e uma senha padrão fixa (`senha123`), que ele deve trocar depois de logar.

O ponto que merece destaque: **existem dois caminhos distintos para tirar o parceiro do estado "Pendente" e o código não deixa claro qual dos dois é o oficial**:

1. **super admin ativa manualmente** — rota `superadmin_parceiro_ativar` (`app/blueprints/superadmin.py`), que seta `status = "Ativo"` e `ativo = True`, com log de auditoria da ação.
2. **o próprio parceiro se autoaprova** — ao logar pela primeira vez com `status = "Pendente"`, o `parceiro_dashboard` (`app/blueprints/parceiro.py`) redireciona para o template `parceiro_pendente.html`, que exibe um botão **"Aprovar e Ativar Meu Cadastro"**. Esse botão faz um POST para `parceiro_aprovar`, uma rota protegida apenas por `@parceiro_required` (login de parceiro), **sem nenhuma checagem de permissão de administrador**. O handler simplesmente faz `parceiro.status = "Ativo"; parceiro.ativo = True` e comita.

Ou seja, qualquer parceiro que receba usuário e senha consegue se autoativar com um único clique, sem qualquer validação humana da plataforma — o estado "Pendente" hoje funciona como uma tela de "aceite de termos", não como um portão de aprovação de fato. Não há como saber pelo código se isso é intencional (uma espécie de onboarding self-service, com o super admin "aprovando" apenas retroativamente/casos de bloqueio) ou um resquício do fluxo antigo que deveria ter sido travado para exigir aprovação humana antes de o parceiro poder criar cupons e aparecer na vitrine dos moradores. Vale confirmar a intenção com o time antes de tratar isso como bug ou como recurso.

### como o parceiro cria e gerencia cupons

Uma vez `Ativo` (por qualquer um dos dois caminhos acima), o parceiro acessa `/parceiro/cupons` e cria ofertas via `parceiro_cupons_criar`, informando:

- **título**, **descrição** (HTML rico sanitizado) e **código prefixo** (usado depois na composição do código de resgate) — campos obrigatórios;
- **data de validade** (opcional; sem data, o cupom não expira);
- **limite total de resgates** (opcional; em branco, é ilimitado);
- **limite por unidade** (padrão 1, se não informado ou inválido).

O cupom nasce `ativo = True`. A desativação (`parceiro_cupons_desativar`) é **permanente** — não existe rota para reativar um cupom desativado, apenas para criar um novo. O parceiro também acompanha, por cupom, o total resgatado e o total já validado (`_metricas_resgates_por_cupom`), e vê um histórico dos últimos 20 resgates no dashboard.

### como o morador resgata

Na tela `clube_vantagens` (`app/routes.py`), o morador logado (`@unidade_required`) vê apenas cupons de parceiros com `status = "Ativo"`, cupons `ativo = True` e ainda dentro da validade. A listagem já filtra no servidor os cupons esgotados: compara o total de resgates do cupom com `limite_total` e o total de resgates *daquela unidade* com `limite_por_unidade`, escondendo o que já bateu o teto.

No resgate (`clube_vantagens_resgatar`), as mesmas checagens são refeitas de forma defensiva (cupom ativo, parceiro ativo, validade, limite total, limite por unidade) antes de gravar. O código único é gerado no formato `PRP-{BLOCO}{APARTAMENTO}-{PREFIXO}-{SUFIXO}`, com sufixo de 4 caracteres alfanuméricos aleatórios, tentando até 20 vezes até achar uma combinação inédita em `ResgateCupom.codigo_unico` (colisão praticamente não acontece, mas o código trata o caso). O `ResgateCupom` nasce com `status = "Ativo"`.

### como o parceiro valida o código na loja física

Em `/parceiro/validacao`, o parceiro digita o código recebido do morador (`parceiro_validar_codigo`). O sistema busca o `ResgateCupom` pelo `codigo_unico` e confere, nesta ordem: (1) se o resgate existe; (2) se o cupom pertence a esse parceiro (um parceiro não valida código de outro); (3) se o status ainda é `"Ativo"` (evita reuso). Passando nas três checagens, marca `status = "Utilizado"` e grava `data_utilizacao`, exibindo bloco/apartamento da unidade para conferência visual no balcão. Não há reversão de validação nem qualquer conferência de identidade além do próprio código — quem detém o código, resgata.

### catálogo global, sem segmentação por condomínio

`Parceiro`, `Cupom` e `ResgateCupom` **não têm `condominio_id`** — os próprios comentários no `models.py` são explícitos: *"escopo GLOBAL (sem condominio_id)"*. Isso significa que todo parceiro cadastrado pelo super admin aparece para **todos os moradores de todos os condomínios da plataforma** ao mesmo tempo, sem qualquer curadoria ou opt-in por tenant. Não existe hoje o conceito de "este parceiro atende apenas o Condomínio X" nem de o síndico/admin do condomínio escolher quais parceiros aparecem para seus moradores. O único vínculo com o tenant é indireto, via `ResgateCupom.unidade_id → Unidade.condominio_id`, usado exclusivamente para permitir corte por condomínio em relatórios (ex.: a auditoria do super admin já faz `join(Unidade)` para listar os últimos 100 resgates da plataforma).

### lacunas para virar um produto de verdade

Dado que o próprio fundador enquadra isso como uma "segunda frente" — o foco atual é gestão de condomínio —, o código hoje reflete exatamente isso: um módulo funcional em sua mecânica central (cupom → resgate → validação), mas ainda sem a camada de produto/negócio que sustentaria uma operação multi-tenant de parceiros comerciais:

- **sem escopo por condomínio.** Não há tabela de associação Parceiro↔Condomínio nem flag de "condomínios atendidos". Qualquer parceiro cadastrado pelo super admin passa a ser visto por moradores de clientes que talvez nem estejam na área de atuação daquele comércio — não há curadoria geográfica nem comercial por tenant.
- **gate de aprovação inconsistente.** Como descrito acima, o próprio parceiro pode se autoativar sem validação humana da plataforma, o que esvazia o propósito do status "Pendente" como controle de qualidade/compliance antes de o parceiro publicar ofertas.
- **nenhuma cobrança ou modelo comercial.** Não existe campo de plano, mensalidade, comissão por resgate/validação, nem qualquer rotina de faturamento ligada a parceiro ou cupom — a entidade `Parceiro` não tem nenhum atributo financeiro.
- **analytics limitado e sem consolidação por condomínio.** O parceiro só enxerga métricas agregadas do próprio negócio (cupons ativos, total de validações, últimos 20 resgates). O super admin tem uma trilha de auditoria simples (últimos 100 resgates da plataforma inteira), mas não há dashboards de conversão, ticket médio, desempenho por condomínio, por bloco, por período, nem exportação de dados — tudo hoje é lista crua, sem agregações de negócio.
- **sem gestão de identidade/duplicidade entre tenants.** Como o cadastro de parceiro é manual pelo super admin (sem autosserviço), qualquer expansão da base de parceiros depende de trabalho operacional humano — não há fluxo de onboarding self-service para o parceiro se cadastrar e pedir para atender um ou mais condomínios específicos.
- **sem controle de capacidade/estoque por período.** Os limites existentes (`limite_total`, `limite_por_unidade`) são estáticos e por cupom; não há recorrência (ex.: "1 por unidade por mês") nem estoque reposto automaticamente — esgotado o limite total, o cupom fica indisponível até o parceiro criar um novo, manualmente.

---

<a id="integracoes-operacao-limitacoes"></a>

## integração com serviços externos e configuração de ambiente

### google drive — fluxo oauth "installed app" e o risco do token.json

`app/drive_api.py` integra com o Google Drive usando `google_auth_oauthlib.flow.InstalledAppFlow`, o fluxo oauth 2.0 pensado para aplicações desktop instaladas na máquina do usuário — não para um processo de servidor rodando sem interface gráfica.

A função `obter_credenciais()` (linhas 18-41) segue esta lógica:

- lê `token.json` do disco (raiz do projeto, `TOKEN_PATH`), se existir, via `Credentials.from_authorized_user_file`.
- se as credenciais estiverem ausentes/inválidas e houver `refresh_token`, tenta `creds.refresh(Request())` dentro de um `try/except Exception` que descarta qualquer erro e apenas seta `creds = None` — a causa real da falha de refresh (token revogado, expirado, revogação manual pelo usuário, etc.) é silenciada.
- se ainda assim não houver credenciais válidas, cai em `InstalledAppFlow.from_client_secrets_file(...).run_local_server(port=8080)` — isso abre um servidor local na porta 8080 e espera um humano completar o consentimento oauth num navegador na mesma máquina.

Isso é frágil especificamente porque, num servidor de produção sem tela e sem navegador local, **o dia em que o `refresh_token` deixar de funcionar** (revogação manual, token não usado por muito tempo, troca de senha da conta Google, app oauth ainda em modo "testing" no console — que expira refresh tokens em 7 dias, ou o limite de tokens simultâneos por client/usuário do Google) **o fallback automático não existe**: o código tenta abrir um navegador e ocupar a porta 8080 num processo que não tem para quem mostrar isso, travando ou falhando a chamada sem qualquer alerta operacional (o erro de refresh já foi engolido pelo `except Exception` genérico).

Outros pontos observados:

- `CLIENT_SECRET_PATH` e `TOKEN_PATH` apontam para dois arquivos na raiz do projeto (`client_secret.json`, `token.json`), listados no `.gitignore` — ou seja, precisam ser provisionados manualmente em qualquer ambiente novo, fora do padrão `.env` usado pelo resto da aplicação.
- `DRIVE_FOLDER_ID` está hardcoded como constante no código-fonte, não como variável de ambiente — não há como apontar tenants diferentes para pastas diferentes.
- `upload_to_drive()` concede permissão pública (`{"type": "anyone", "role": "reader"}`) a cada arquivo enviado — qualquer pessoa com o link acessa o documento, sem controle de acesso por condomínio.
- busca no repositório não encontrou nenhuma chamada a `upload_to_drive()` a partir de rotas/blueprints hoje — o módulo está definido mas não conectado a nenhum fluxo em produção. As colunas que o suportariam já existem no model `Unidade` (`contrato_locacao_drive_id`, `contrato_locacao_url`, `contrato_locacao_status`, adicionadas em `_garantir_colunas_unidades()` em `app/__init__.py`), então a fragilidade descrita acima é um risco latente para quando essa integração for de fato ligada a uma rota.

### envio de e-mail — smtp gmail direto e síncrono, sem fila

`app/email_service.py` não usa fila de mensagens, worker assíncrono nem serviço transacional de e-mail. `_enviar_email()` abre uma conexão `smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15)` diretamente dentro da função chamada pela rota, faz login e `sendmail` de forma bloqueante, e retorna (ou levanta exceção).

Implicações observadas no código:

- os pontos de chamada em `app/routes.py` (ex.: `esqueci_senha`, linha ~910; notificação de nova reserva, linha ~1313) envolvem o envio num `try/except Exception`, mas o `except` só é alcançado *depois* que a chamada de rede termina ou estoura o timeout de 15s — ou seja, no pior caso (smtp do gmail lento ou inacessível), a requisição http fica presa por até 15 segundos antes de cair no `except` e mostrar um `flash` de aviso ao usuário.
- não há retry automático nem fila de reenvio: se o envio falhar (rede instável, limite de envio do gmail, senha de app inválida), a mensagem é simplesmente perdida — o usuário só vê um aviso genérico ("não foi possível enviar o e-mail...") e precisaria repetir a ação manualmente.
- como não há `Celery`, `RQ` ou qualquer outro worker/fila no `requirements.txt`, todo o custo de latência do smtp (conexão, `STARTTLS`/ssl handshake, login, envio) é pago dentro do ciclo request/response do Flask, no mesmo processo que atende a requisição do usuário.
- a aplicação inteira usa uma única conta gmail (uma credencial `MAIL_USERNAME`/`MAIL_PASSWORD`) como remetente — em um SaaS multi-tenant, todos os condomínios enviam e-mails a partir da mesma conta, sem isolamento de remetente por tenant.

### variáveis de ambiente

Carregadas via `load_dotenv()` no topo de `app/__init__.py` (arquivo `.env`, não versionado):

- `SECRET_KEY` — chave de sessão/assinatura (`itsdangerous`, tokens de redefinição de senha). **Obrigatória:** o boot falha com `RuntimeError` se estiver ausente (sem fallback hardcoded). Documentada em `.env.example`.
- `DATABASE_URL` — string de conexão sqlalchemy, com fallback `sqlite:///condominio.db`.
- `MAIL_USERNAME` / `MAIL_PASSWORD` — credenciais smtp do gmail (`app/email_service.py`). Sem uma delas, `_enviar_email()` levanta `RuntimeError` explícito.
- arquivos (não variáveis de ambiente, mas configuração externa obrigatória) — `client_secret.json` e `token.json`, na raiz do projeto, exigidos pela integração com o Google Drive.

O arquivo `.env.example` documenta `SECRET_KEY` (obrigatória) e as credenciais SMTP.

### limitações técnicas conhecidas (observadas no código)

- fluxo oauth do Google Drive é o modelo "installed app" (browser local + `run_local_server(porta 8080)`), inadequado para reautenticação num servidor headless sem interação humana.
- falha no refresh do token do Drive é silenciada (`except Exception: creds = None`) — a causa real do problema se perde antes de cair no fluxo interativo, que por sua vez não tem como funcionar sem um humano com navegador na mesma máquina do processo.
- `upload_to_drive()` não é chamado por nenhuma rota atualmente (confirmado por busca no repositório) — a integração está pronta no model (`Unidade.contrato_locacao_drive_id/url/status`) mas desconectada; o risco descrito acima só se materializa quando alguém ligar essa funcionalidade a uma rota.
- `DRIVE_FOLDER_ID` fixo no código e permissão de leitura pública (`"anyone"`, `"reader"`) por arquivo enviado — sem isolamento de pasta/acesso por condomínio (tenant).
- credenciais do Drive vivem em dois arquivos soltos na raiz do projeto (`client_secret.json`, `token.json`), fora do padrão `.env` usado pelo resto da aplicação — provisionamento manual e não documentado por ambiente.
- envio de e-mail é 100% síncrono dentro do ciclo request/response, com timeout fixo de 15s por tentativa, sem fila e sem worker assíncrono no projeto (`Celery`/`RQ` ausentes do `requirements.txt`).
- sem retry automático de e-mail — falha vira apenas um `flash` de aviso ao usuário; a mensagem não é reenviada nem persistida para nova tentativa.
- uma única conta gmail (`MAIL_USERNAME`) atende a todos os condomínios da instalação — sem isolamento de remetente por tenant.
- banco de dados padrão é sqlite (`DATABASE_URL` default `sqlite:///condominio.db`), com `connect_args={"timeout": 15}` para mitigar `database is locked` — modelo de escritor único do sqlite é um teto de concorrência para um saas multi-tenant em crescimento.
- migrações de schema são feitas por dezenas de funções `_garantir_colunas_*()`/`_garantir_tabela_*()` chamadas sequencialmente a cada boot do `create_app()`, cada uma reintrospectando o schema via `inspect(db.engine)` — não há framework de migração versionado (ex.: Alembic), nem rollback; o custo de boot cresce a cada nova função adicionada.

---

<a id="achados-tecnicos-riscos"></a>

## achados técnicos e riscos identificados

Os achados abaixo foram confirmados por verificação adversarial direta no código. Estão organizados por severidade (crítico → alto → médio → baixo) e, dentro de cada nível, agrupados por categoria. Os itens 17 a 21 (dimensão *database-migrations*) não trouxeram campo de severidade explícito na verificação original; a classificação usada para posicioná-los abaixo é uma avaliação própria, feita a partir do impacto descrito no cenário de falha de cada um.

### status após a sprint de segurança

A **grande maioria** dos riscos abaixo foi **[CORRIGIDA]** na última sprint de segurança, arquitetura e concorrência. Os títulos marcados com **[CORRIGIDO]** descrevem o cenário histórico (como o código falhava) e a linha **Resolução:** resume como o sistema ficou blindado. Itens **sem** essa tag permanecem abertos / pendentes de hardening.

Resumo do pacote aplicado: `SECRET_KEY` obrigatória no boot; XSS da portaria fora de atributos inline; tokens de reset amarrados a tenant + carimbo `senha_atualizada_em`; `session.clear()` em logins de unidade e parceiro; índices/UPDATE atômicos contra corridas de reserva, cupom, mudança e check-in; e proteções contra perda de documentos / exclusão com encomenda pendente.

### crítico

#### 1. `SECRET_KEY` com fallback hardcoded permite forjar sessão de qualquer papel **[CORRIGIDO]**
`app/__init__.py` — *categoria: session-forgery*

- **cenário de falha (histórico):** se a variável de ambiente `SECRET_KEY` não estiver definida no deploy real, qualquer pessoa com acesso ao código-fonte — que continha o valor literal `"dev-change-me-in-production"` — conseguia forjar um cookie de sessão Flask assinado válido com `session['user_id']` de um super admin. Como `get_current_user()` e `get_unidade_logada()` só refazem a busca pelo id da sessão e nunca revalidam mais nada além do `condominio_id`, o cookie forjado passava simultaneamente em `superadmin_required`, `admin_required`, `sindico_required`, `portaria_required` e `unidade_required` — bypass total de autenticação para qualquer papel.
- **Resolução:** removido o fallback hardcoded; `create_app()` agora levanta `RuntimeError` se `SECRET_KEY` não estiver no ambiente (ou no `config` de teste). A variável passou a constar como obrigatória em `.env.example`.

#### 2. XSS armazenado em contexto JavaScript via nome de visitante **[CORRIGIDO]**
`app/templates/portaria/acesso.html` — *categoria: stored-xss-js-context*

- **cenário de falha (histórico):** um morador autenticado cadastrava autorização com `nome_visitante` contendo payload JS; o valor era interpolado em `onsubmit="confirm('...')"` na portaria. O Jinja escapava a aspa como entidade HTML, mas o navegador decodificava a entidade antes de executar o atributo como JavaScript — XSS no browser do porteiro, com potencial de `fetch()` autenticado (sem CSRF) em rotas da portaria.
- **Resolução:** confirmações passaram a usar atributo `data-confirm-mensagem` (contexto HTML escapado pelo Jinja) lido por event listeners JS — sem interpolação em `onsubmit`/`onclick` inline.

### alto

#### 3. redefinição de senha do morador não valida o tenant do token **[CORRIGIDO]**
`app/routes.py` / `app/utils.py` — *categoria: cross-tenant-password-reset*

- **cenário de falha (histórico):** `gerar_token_redefinicao(email, ...)` codificava apenas o e-mail. Com o mesmo e-mail em unidades de dois condomínios e `session['condominio_id']` apontando para outro tenant, `redefinir_senha()` podia trocar a senha da unidade errada.
- **Resolução:** o payload assinado do token inclui `condominio_id`; `verificar_token_redefinicao()` devolve `(email, condominio_id, emitido_em)` e `redefinir_senha()` resolve a unidade **somente** pelo tenant do token (não pela sessão atual). Links legados sem tenant são rejeitados.

#### 4. token de redefinição de senha é reutilizável dentro da janela de validade **[CORRIGIDO]**
`app/utils.py` / models `Unidade` e `Parceiro` — *categoria: password-reset-token-reuse*

- **cenário de falha (histórico):** `verificar_token_redefinicao()` só checava assinatura e prazo (`max_age=3600s`). Um link vazado podia ser reutilizado várias vezes na mesma hora, inclusive após a troca legítima de senha. O mesmo padrão existia em `parceiro_redefinir_senha`.
- **Resolução:** coluna `senha_atualizada_em` em `Unidade` e `Parceiro`, preenchida em `set_password()`. As rotas de reset comparam `emitido_em` do token com esse carimbo e rejeitam links emitidos antes da última troca (uso único efetivo na janela de 1 hora).

#### 5. upload de logo em SVG permite XSS armazenado público
`app/blueprints/superadmin.py` — *categoria: unrestricted-file-upload-xss*

- **cenário de falha:** `_salvar_logo_condominio` permite upload de `.svg` como logo do condomínio (`_LOGO_EXTENSIONS` inclui `svg`), sem checagem de conteúdo/magic-bytes, e o arquivo fica publicamente acessível em `/static/uploads/logos/`. Um super admin (ou uma sessão comprometida) envia um SVG contendo `<script>`/`onload` com payload de exfiltração de cookie; `secure_filename()` só normaliza o nome, não o conteúdo. Qualquer visitante — inclusive não autenticado, já que o logo aparece em `login.html`/`tenant_login.html` — que abra a URL do SVG diretamente no navegador faz o navegador renderizar o SVG como documento de topo e executar o script embutido no mesmo domínio da aplicação.
- **sugestão de correção:** remover `svg` de `_LOGO_EXTENSIONS` (alinhando com o whitelist já usado em `utils.salvar_logo_parceiro`/`_salvar_imagem_upload`, restrito a `{png,jpg,jpeg,webp}`), ou, se SVG for necessário, sanitizar removendo `<script>`/atributos `on*` e servir com `Content-Disposition: attachment` ou CSP que bloqueie script inline.

#### 6. corrida em reserva de área comum permite double-booking **[CORRIGIDO]**
`app/routes.py` / `app/models.py` — *categoria: race-condition*

- **cenário de falha (histórico):** duas requisições concorrentes reservavam o mesmo `espaco_id`+`data_reserva`; ambas passavam no `SELECT` de conflito e ambas inseriam — double-booking.
- **Resolução:** índice único parcial `ux_reserva_espaco_data_ativa` em `Reserva` (`espaco_id` + `data_reserva` onde status ∈ Pendente/Aprovada), com tratamento de `IntegrityError` na criação. O banco rejeita a segunda reserva ativa.

#### 7. corrida no resgate de cupom permite exceder limite contratado **[CORRIGIDO]**
`app/routes.py` — *categoria: race-condition*

- **cenário de falha (histórico):** dois cliques simultâneos faziam `COUNT()` antes do `INSERT` e ultrapassavam `limite_total` / `limite_por_unidade`.
- **Resolução:** reserva atômica via `UPDATE cupom SET total_resgatado = total_resgatado + 1 WHERE ... total_resgatado < limite_total`, com checagem de `rowcount` e revalidação do limite por unidade na mesma transação (lock de escrita do SQLite).

#### 8. reprovar todos os moradores de uma unidade já registrada apaga documentos validados **[CORRIGIDO]**
`app/blueprints/sindico.py` — *categoria: data-loss*

- **cenário de falha (histórico):** reprovação total em unidade com documentos já `Entregue` executava `db.session.delete(unidade)` e perdia documento/contrato/proprietário validados.
- **Resolução:** antes da exclusão, `sindico_validar_unidade` verifica se `documento_status` ou `contrato_locacao_status` já estavam `Entregue`; nesse caso a unidade **não** é apagada — permanece `Pendente` com auditoria, preservando os dados documentais.

#### 9. exclusão de unidade deixa encomendas/acessos/ocorrências órfãos **[CORRIGIDO]**
`app/blueprints/admin.py` / `app/models.py` — *categoria: orphaned-data*

- **cenário de falha (histórico):** `admin_excluir_unidade` apagava a unidade sem tratar `Encomenda` pendente; a portaria quebrava ao processar registro órfão.
- **Resolução:** exclusão administrativa bloqueada enquanto houver encomenda `Pendente` vinculada; relacionamentos de cadastro do morador (`Pessoa`/`Veiculo`/`AgendamentoMudanca`) seguem com cascade; histórico operacional (encomendas/acessos/ocorrências) é tratado como dado de portaria, não apagado às cegas.

#### 10. migrações concorrentes (`_garantir_colunas_*`) quebram boot sob múltiplos workers
`app/__init__.py` — *categoria: concurrent-migration-race* (severidade avaliada: alta)

- **cenário de falha:** como `run.py` chama `create_app()` no nível do módulo, subir com `gunicorn -w 4 run:app` sem `--preload` dispara N processos chamando `create_app()` concorrentemente contra o mesmo SQLite. Se falta, por exemplo, a coluna `notificacao_sindico` em `unidades`, os 4 workers inspecionam o schema quase simultaneamente, todos veem a coluna ausente e todos tentam o mesmo `ALTER TABLE`. O primeiro commita; os demais recebem `sqlite3.OperationalError: duplicate column name`, não capturado em nenhum lugar da cadeia, derrubando `create_app()` e o worker inteiro durante o boot.
- **sugestão de correção:** rodar as migrações uma única vez antes do fork dos workers (`gunicorn --preload`, ou um comando de migração dedicado executado no deploy antes de subir a aplicação), ou envolver cada `ALTER TABLE` em `try/except OperationalError` ignorando "duplicate column".

### médio

#### 11. sessão mista permite super admin operar portaria de outro condomínio sem selecionar tenant **[CORRIGIDO]**
`app/auth.py` — *categoria: superadmin-session-tenant-confusion*

- **cenário de falha (histórico):** super admin autenticado fazia login como morador na mesma aba; `login_unidade()` setava `condominio_id` sem limpar `user_id`/`role`. Rotas de portaria liberavam o super admin operando o tenant da unidade sem seleção explícita.
- **Resolução:** `login_unidade()` chama `session.clear()` antes de gravar os dados da unidade (espelhando `login_usuario()`), eliminando `user_id`/`role` residuais.

#### 12. `login_unidade()` não limpa sessão anterior (session fixation entre papéis) **[CORRIGIDO]**
`app/auth.py` / `app/blueprints/parceiro.py` — *categoria: session-fixation*

- **cenário de falha (histórico):** admin/síndico/porteiro que entrava como unidade sem logout ficava com sessão mista (`admin_required` + `unidade_required` na mesma cookie). O login do parceiro também podia herdar chaves de staff/morador.
- **Resolução:** `session.clear()` no início de `login_unidade()` e no login bem-sucedido do parceiro (`parceiro_login`), antes de setar apenas as chaves do perfil autenticado.

#### 13. kanban de ocorrências do admin ignora jurisdição de bloco do síndico **[CORRIGIDO]**
`app/blueprints/admin.py` — *categoria: sindico-jurisdiction-bypass*

- **cenário de falha (histórico):** síndico de um bloco via/atualizava ocorrências de todo o condomínio no kanban `admin_ocorrencias`.
- **Resolução:** `admin_ocorrencias_atualizar_status` (e listagem associada) aplica `_sindico_gerencia_bloco` quando `role=SINDICO`, alinhado ao restante do módulo do síndico.

#### 14. enumeração de e-mail por tempo de resposta em "esqueci minha senha"
`app/routes.py` — *categoria: email-enumeration-timing*

- **cenário de falha:** `esqueci_senha()` só dispara o envio de e-mail (SMTP síncrono, timeout=15s) quando o e-mail existe no tenant, retornando a mesma mensagem genérica em ambos os casos — mas quando o e-mail existe a rota demora até 15s (conexão SMTP) e quando não existe retorna quase instantaneamente. Um atacante medindo a latência de `POST /esqueci_senha` consegue enumerar e-mails cadastrados apesar do texto de resposta ser idêntico. O mesmo padrão existe em `parceiro_esqueci_senha`.
- **sugestão de correção:** igualar o tempo de resposta nos dois ramos (ex.: sempre executar um envio/"descarte" equivalente, ou aplicar um delay constante) para não vazar a existência do e-mail por timing.

#### 15. ausência de proteção CSRF em ações destrutivas do admin
`app/blueprints/admin.py` — *categoria: csrf*

- **cenário de falha:** não há proteção CSRF em nenhum lugar do projeto (sem Flask-WTF, sem token nos forms) nem `SESSION_COOKIE_SAMESITE`/`SECURE` configurados. `admin_excluir_unidade` apaga permanentemente o cadastro de uma unidade e todos os vínculos de morador; `admin_unidade_alterar_senha` troca a senha de acesso de uma unidade para qualquer valor enviado no form. Ambas são `<form method=post>` sem token anti-CSRF: uma página maliciosa que force um POST enquanto o admin está autenticado consegue excluir uma unidade ou sequestrar o acesso de um morador. O impacto hoje é apenas atenuado pelo comportamento padrão `SameSite=Lax` dos navegadores modernos, já que a aplicação não define nenhuma defesa própria.
- **sugestão de correção:** adotar Flask-WTF/CSRFProtect (token CSRF em todos os forms POST) e configurar `SESSION_COOKIE_SAMESITE="Lax"`/`"Strict"` e `SESSION_COOKIE_SECURE=True`.

#### 16. corrida na aprovação/rejeição de mudança (síndico e administração) **[CORRIGIDO]**
`app/blueprints/sindico.py` / `app/blueprints/admin.py` — *categoria: race-condition*

- **cenário de falha (histórico):** duplo POST quase simultâneo em aprovar/rejeitar o mesmo agendamento; ambas as requisições passavam na guarda de status e o último commit ganhava, com dois flashes/auditorias “válidas”.
- **Resolução:** transição via `UPDATE ... WHERE status = <status_esperado>` com verificação de `rowcount` — a segunda requisição concorrente é rejeitada.

#### 17. `timeout=15` no SQLite só adia "database is locked", sem WAL nem tratamento
`app/__init__.py` — *categoria: sqlite-lock-contention* (severidade avaliada: média)

- **cenário de falha:** com vários workers gravando ao mesmo tempo (portaria registrando entradas, morador criando reservas, migrações concorrentes segurando locks durante o boot), uma transação que espera mais de 15s pelo lock exclusivo do arquivo SQLite recebe `sqlite3.OperationalError: database is locked`; como não há `app.errorhandler` para `OperationalError` nem retry ao redor de `db.session.commit()`, essa exceção sobe como 500 não tratado para o usuário no meio de uma ação.
- **sugestão de correção:** habilitar `PRAGMA journal_mode=WAL` (reduz contenção de escrita) e capturar `OperationalError` ao redor de commits críticos com retry/backoff limitado, além de um `errorhandler` dedicado.

#### 18. `RegistroAcesso` sem constraint contra duas entradas abertas do mesmo visitante **[CORRIGIDO]**
`app/models.py` / `app/__init__.py` / `app/blueprints/portaria.py` — *categoria: missing-unique-constraint* (severidade avaliada: média)

- **cenário de falha (histórico):** dois check-ins concorrentes do mesmo visitante criavam duas entradas com `data_saida IS NULL`.
- **Resolução:** índice único parcial `ux_registro_acesso_aberto` em `(visitante_id) WHERE data_saida IS NULL`, com tratamento de `IntegrityError` nos fluxos de entrada da portaria.

#### 19. seed de superadmin/condomínio legado sem lock — corrida derruba boot com IntegrityError
`app/__init__.py` — *categoria: seed-race-condition* (severidade avaliada: média)

- **cenário de falha:** `_seed_superadmin` e `_seed_condominio_transicao` fazem check-then-insert (query por existente, senão cria) sem lock. Dois workers sobem ao mesmo tempo contra um banco sem usuário superadmin ainda: ambos veem `None` na query e ambos tentam `add`+`commit`; o segundo commit viola a `UniqueConstraint` de `username`, lança `IntegrityError` não capturada e aborta `create_app()` no meio da sequência de migração daquele worker — pulando inclusive as `_garantir_colunas_*` que viriam depois na mesma chamada, deixando o processo com inicialização parcial.
- **sugestão de correção:** envolver o check-then-insert do seed em `try/except IntegrityError` (com rollback e continuação do boot), ou mover o seed para um comando de inicialização único executado antes de subir os workers do gunicorn.

### baixo

#### 20. `Reserva` sem `UniqueConstraint(espaco_id, data_reserva)` **[CORRIGIDO]**
`app/models.py` / migração em `app/__init__.py` — *categoria: missing-unique-constraint*

- **cenário de falha (histórico):** mesmo defeito de fundo do achado #6, visto pelo ângulo do schema.
- **Resolução:** índice único parcial `ux_reserva_espaco_data_ativa` (ver item #6), criado no model e garantido na migração leve de boot.

#### 21. interpolação de nome de tabela via f-string em DDL/DML
`app/__init__.py` — *categoria: sql-string-interpolation-pattern*

- **cenário de falha:** `_garantir_colunas_multi_tenant` e `_seed_condominio_transicao` constroem DDL/DML via f-string interpolando o nome da tabela dentro de `text(...)`, em vez de validar o identificador contra uma allowlist. Hoje não é explorável porque `tabela` só vem de tuplas fixas hardcoded no código — mas qualquer refator futuro que derive esse nome de configuração dinâmica ou de parâmetro de rota reintroduziria SQL injection imediatamente, já que nomes de tabela/coluna não podem ser bind parameters em `text()`.
- **sugestão de correção:** extrair uma função utilitária que valide o nome de tabela contra uma allowlist explícita (ou `Enum`) antes de qualquer interpolação em `text()`, mesmo enquanto os valores só vêm de tuplas hardcoded.

#### 22. import morto (`escape`, `HTMLParser`, `re`) remanescente da extração para `utils.py`
`app/routes.py` — *categoria: dead-import*

- **cenário de falha:** sem impacto funcional — `_SanitizadorHtmlRico`/`_html_rico_form`, que usavam esses imports, foram extraídos para `app/utils.py` durante o refactor, mas os imports órfãos permaneceram em `routes.py` (confirmado por `pyflakes`). Risco é apenas de manutenção: confusão futura sobre onde a sanitização de HTML realmente vive.
- **sugestão de correção:** remover os imports não utilizados (`escape`, `HTMLParser`, `re`) de `app/routes.py`.

#### 23. import morto (`date`) em `admin.py`
`app/blueprints/admin.py` — *categoria: dead-import*

- **cenário de falha:** sem impacto funcional — `date` foi copiado junto com o bloco de import original durante a extração das views para o blueprint, mas nenhuma função do módulo o referencia (só `datetime`/`timedelta` são usados).
- **sugestão de correção:** remover o import não utilizado de `date` em `app/blueprints/admin.py`.

#### 24. import morto (`AgendamentoMudanca`) em `portaria.py`
`app/blueprints/portaria.py` — *categoria: dead-import*

- **cenário de falha:** sem impacto funcional — a única view que trabalha com agendamentos (`portaria_mudanca_chegar`) carrega o registro via `_agendamento_do_tenant` (importado tardiamente de `app.routes`); só `StatusAgendamentoMudanca` é de fato usado no arquivo.
- **sugestão de correção:** remover o import não utilizado de `AgendamentoMudanca` em `app/blueprints/portaria.py`, mantendo apenas `StatusAgendamentoMudanca`.