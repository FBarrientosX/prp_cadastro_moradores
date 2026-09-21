# gestão de condomínios & clube de vantagens — documentação de arquitetura e produto

---

Este documento é a referência técnica e de produto do SaaS multi-tenant de gestão de condomínios (Flask + SQLAlchemy), incluindo o módulo de Clube de Vantagens. Descreve o que o sistema **faz hoje**, como foi construído (da origem single-tenant PRP até a plataforma), o modelo de dados, os fluxos por papel, as decisões de isolamento, a sprint de segurança e as limitações ainda abertas.

Não descreve arquitetura futura não implementada como se já existisse. Quando um campo existe no banco mas o runtime ainda não o usa (ex.: fluxo de mudança `"Simples"`), isso fica explícito.

Destina-se ao time interno: onboarding de quem vai manter o código, revisão de produto e continuidade das evoluções.

## sumário

- [linha do tempo — o que foi feito](#linha-do-tempo)
- [visão geral do produto](#visao-geral-produto)
- [arquitetura técnica](#arquitetura-tecnica)
- [multi-tenant e isolamento](#multi-tenant)
- [autenticação, sessão e recuperação de senha](#autenticacao)
- [modelo de dados](#modelo-de-dados)
- [fluxos de negócio por papel](#fluxos-de-negocio)
- [smart diff — atualização de cadastro](#smart-diff)
- [clube de vantagens](#clube-de-vantagens)
- [interface, templates e bibliotecas](#interface)
- [e-mail](#email)
- [integrações, ambiente e operação](#integracoes-operacao-limitacoes)
- [migrações leves e consistência](#migracoes)
- [catálogo de rotas](#catalogo-rotas)
- [achados técnicos, sprint de segurança e pendências](#achados-tecnicos-riscos)

---

<a id="linha-do-tempo"></a>

## linha do tempo — o que foi feito

O repositório nasceu como **cadastro de moradores de um único condomínio** (PRP). Cada camada abaixo foi acrescentada em cima da anterior, sem reescrita total. Entender essa sequência explica por que o schema tem `condominio_id` nullable em tabelas antigas, por que as rotas vivem em `app/blueprints/` sem a classe `Blueprint` do Flask, e por que o boot ainda roda dezenas de `_garantir_colunas_*`.

### 1. núcleo de cadastro (produto original)

- Unidade identificada por bloco + apartamento, com senha própria (o morador **não** é um `Usuario`).
- Pessoas (responsável, vínculo proprietário/locatário/morador, menoridade, interfone) e veículos.
- Status da unidade: `Pendente` → `Aprovada` (síndico) → `Registrada` (administração).
- Validação unificada do síndico (recusar morador com motivo, aprovar o restante, ou apagar a unidade se todos forem recusados).
- Smart Diff: correção de telefone/e-mail/CPF **não** devolve a unidade para `Pendente`; inclusão/remoção de morador ou veículo, sim.
- Recuperação de senha do morador com anti-enumeração de e-mail.

### 2. operação do condomínio

- Agendamento de mudanças (entrada/saída) com antecedência mínima de 3 dias e bloqueio de domingo; dupla aprovação síndico → administração; check-in na portaria no dia.
- Reservas de área comum (`EspacoComum` / `Reserva`), calendário FullCalendar, pagamento e jurisdição (admin vs síndico do bloco).
- Portaria: visitantes/prestadores, registro de entrada/saída, encomendas (foto, rastreio, notificação ao morador), autorizações prévias criadas pelo morador.
- Ocorrências (helpdesk/kanban) e notificações internas portaria ↔ morador.
- Auditoria (`LogAuditoria`) nas ações sensíveis.

### 3. clube de vantagens (módulo anexo)

- Parceiro comercial com portal próprio (`parceiro_id`, sem tenant).
- Cupons com limite total (contador atômico) e limite por unidade; QR no resgate; validação no balcão.
- Catálogo **global** da plataforma: `Parceiro` e `Cupom` não têm `condominio_id`. Métricas por condomínio só via `ResgateCupom.unidade_id → Unidade.condominio_id`.

### 4. virada SaaS multi-tenant

- Tenant raiz `Condominio` + `ConfiguracaoCondominio` (white-label: cor, logo, rótulos Bloco/Apto).
- Login por slug: `/c/<slug>/login` (morador e equipe no mesmo formulário, abas distintas).
- Papel `superadmin` da plataforma (`condominio_id` nulo): cria clientes, primeiro admin, ativa/desativa (soft delete), CRUD global de parceiros.
- `SindicoAgrupamento` substitui o campo único `bloco_responsavel` (síndico 1:N).
- Soft delete do cliente (`Condominio.ativo`); porta do slug inativo renderiza `condominio_suspenso.html`.
- Helpers anti-IDOR (`_unidade_do_tenant`, etc.) e `session.clear()` em todo login de contexto.
- Seed de transição: se o banco estiver vazio, cria o cliente legado `"PRP Condomínio"` (`slug=prp`) e faz backfill de `condominio_id`.

### 5. extração de rotas (sem Flask Blueprint)

O arquivo monolítico `app/routes.py` foi fatiado em módulos por perfil (`app/blueprints/parceiro.py`, `superadmin.py`, `sindico.py`, `admin.py`, `portaria.py`). A extração **preserva os nomes de endpoint** com `app.add_url_rule(...)`, de propósito: a classe `flask.Blueprint` prefixaria `admin.admin_index` e quebraria todos os `url_for` dos templates.

O núcleo compartilhado (cadastro, login tenant, reservas, clube do morador, helpers `_algo`) permanece em `routes.py`. Os módulos importam esses helpers **dentro da view**, não no topo do arquivo, para evitar import circular.

### 6. sprint de segurança, concorrência e MySQL

Pacote aplicado no código atual (detalhe na [seção de achados](#achados-tecnicos-riscos)):

- `SECRET_KEY` obrigatória no boot (`RuntimeError` se ausente).
- Tokens de reset com `condominio_id` no payload + invalidação por `senha_atualizada_em`.
- `session.clear()` em login de unidade e de parceiro.
- XSS da portaria: confirmações saíram de `onsubmit` inline para `data-confirm-mensagem`.
- Resgate de cupom atômico (`UPDATE ... total_resgatado + 1 WHERE ...`).
- Aprovação/rejeição de mudança com `UPDATE ... WHERE status = esperado`.
- Duplo-booking de reserva e “entrada aberta” de visitante: **trava na aplicação** (`with_for_update`), não índice parcial SQLite — o MySQL não suporta `CREATE UNIQUE INDEX ... WHERE`.
- Síndico não apaga unidade com documento já `Entregue`; admin não exclui unidade com encomenda pendente.
- Kanban de ocorrências recortado pela jurisdição do síndico.
- `SQLALCHEMY_ENGINE_OPTIONS = {"pool_recycle": 280}` para conexões ociosas (MySQL).

---

<a id="visao-geral-produto"></a>

## visão geral do produto

### o que o produto faz hoje

A aplicação é um **SaaS multi-tenant de gestão de condomínio**. O comprador é o condomínio (ou sua administradora). O Clube de Vantagens é um segundo módulo, mais novo, que liga moradores a parceiros comerciais locais — catálogo único da plataforma, não um marketplace por cliente.

O tenant raiz é `Condominio` (`app/models.py`), identificado por **slug único** na URL (`/c/<slug>/login`). Cada cliente tem:

- soft-delete (`ativo`); condomínio inativo não entra no painel — vê `condominio_suspenso.html`;
- `ConfiguracaoCondominio` 1:1: cor primária e logo (white-label), rótulos de agrupamento/unidade (`label_agrupamento` / `label_unidade`, padrão "Bloco" / "Apto"), flags `usa_agrupamentos` e `tem_subsindicos`, e o campo `fluxo_aprovacao_mudanca` (`Simples` | `Dupla`).

**Importante sobre mudanças:** o Super Admin grava `fluxo_aprovacao_mudanca`, mas o **runtime ainda implementa só o fluxo duplo** (Pendente Síndico → Pendente Administração → Aprovada). Não existe atalho “Simples” nas views de síndico/admin/morador. Tratar o campo como configuração persistida, não como regra já ligada ponta a ponta.

O núcleo funcional cobre:

1. **Cadastro e ciclo de vida da unidade** — moradores se cadastram por bloco/apartamento, vinculam pessoas e veículos, definem senha da unidade. Documentos (comprovante / contrato de locação) têm **status operacional** (`Pendente` / `Entregue` / `Nao Enviado` / `Nao Aplicavel`) conferido pela administração. As colunas de Google Drive existem no model; o upload **não está ligado a nenhuma rota** hoje (ver [integrações](#integracoes-operacao-limitacoes)).
2. **Aprovação** — síndico valida pessoas da unidade; administração registra a unidade (`Registrada`) após conferir documentos.
3. **Reservas de área comum** — espaços com dias, valor e gestão por administração ou por bloco do síndico.
4. **Mudanças** — entrada/saída com D+3, sem domingo, dupla aprovação e check-in na portaria.
5. **Portaria** — visitantes/prestadores, encomendas, autorizações prévias, notificações ao morador.
6. **Ocorrências** — chamados do morador; kanban para admin/síndico.
7. **Auditoria** — `LogAuditoria` por usuário e condomínio.
8. **Clube de vantagens** — catálogo global; resgate por unidade; validação pelo parceiro.

Estado de transição do banco: tabelas antigas (`usuarios`, `unidades`, `espacos_comuns`, `logs_auditoria`, `agendamentos_mudanca`) ainda têm `condominio_id` **nullable** (legado PRP). Módulos novos (`visitantes`, `registros_acesso`, `encomendas`, `autorizacoes_acesso`, `notificacoes`, `ocorrencias`) nascem com `condominio_id` **NOT NULL**.

A planta física usada em `validar_unidade()` (`BLOCOS_ANDARES` em `app/utils.py`) ainda é a do PRP (blocos 1–8, 7 ou 8 andares, 8 aptos por andar). **Todos os tenants passam por essa validação no cadastro público.** White-label muda o texto da UI, não a geometria dos blocos.

A unicidade de `Unidade` no banco continua `UniqueConstraint("bloco", "apartamento")` **sem** `condominio_id`. As buscas de negócio filtram pelo tenant; dois condomínios não podem, no schema atual, ter o mesmo par bloco+apto.

### para quem

- **Condomínios e administradoras** — cliente pagante; painel por tenant (cadastros, documentos, equipe, ocorrências, mudanças, reservas, analytics do clube).
- **Síndicos / subsíndicos** — jurisdição por `SindicoAgrupamento`; aprovam cadastros e mudanças do bloco; tratam ocorrências e reservas do recorte.
- **Moradores** — autoatendimento na unidade: cadastro, reservas, mudanças, autorizações, ocorrências, clube.
- **Portaria** — entrada/saída, encomendas, check-in de mudança, notificações.
- **Parceiros comerciais** — público secundário: cupons para moradores de **todos** os condomínios da instalação.

### papéis

Definidos em `Role` (`app/models.py`) e aplicados por decorators em `app/auth.py`. O morador **não** é `Usuario`.

| Papel | Persistência | Tenant | O que faz |
|---|---|---|---|
| Super Admin | `Usuario.role=superadmin` | nenhum (`condominio_id` NULL) | Plataforma: clientes, white-label, primeiro admin, parceiros globais |
| Admin local | `role=admin` | obrigatório | Dono operacional do condomínio |
| Assistente | `role=assistente` | obrigatório | Fila de cadastros, senha de unidade, mudanças, reservas da admin |
| Síndico | `role=sindico` | obrigatório + agrupamentos | Cadastros, mudanças 1ª instância, ocorrências e reservas do bloco |
| Porteiro | `role=porteiro` | obrigatório | Só portaria |
| Morador | `Unidade` + senha | `Unidade.condominio_id` | Autoatendimento da unidade |
| Parceiro | tabela `parceiro` | **global** | Cupons e validação; sessão `parceiro_id` |

---

<a id="arquitetura-tecnica"></a>

## arquitetura técnica

### stack

- **Flask 3.0** (`run.py` → `create_app()`), host `0.0.0.0:5000` em debug.
- **SQLAlchemy 2** via Flask-SQLAlchemy; `db = SQLAlchemy()` único em `app/__init__.py`.
- **Banco:** variável **`SQLALCHEMY_DATABASE_URI`** (não `DATABASE_URL`), fallback `sqlite:///condominio.db`. `pool_recycle=280` para reciclar conexões ociosas (cenário MySQL). Não há `connect_args` de timeout SQLite no `create_app()` atual.
- **Frontend:** Jinja2, Bootstrap 5.3, Bootstrap Icons, JavaScript nativo.
- **E-mail:** `smtplib` síncrono Gmail SSL (`smtp.gmail.com:465`), sem fila.
- **Drive:** OAuth “installed app” em `app/drive_api.py` (`client_secret.json` + `token.json`); módulo presente, **sem chamada a partir de rotas**.
- **python-dotenv:** `load_dotenv()` no topo de `app/__init__.py`.

### app factory (`create_app`)

Ordem real do boot:

1. Pastas de upload: `static/uploads/{logos,parceiros,ocorrencias,encomendas}`.
2. `SECRET_KEY` do ambiente (ou `config` de teste). Ausente → `RuntimeError` explicando como gerar a chave.
3. `SQLALCHEMY_DATABASE_URI`, `SQLALCHEMY_TRACK_MODIFICATIONS=False`, `pool_recycle=280`, `MAX_CONTENT_LENGTH=10MB`.
4. `db.init_app(app)`.
5. `context_processor` `inject_nav_context`: usuário, unidade, condomínio, RGB da cor primária, contador de reservas pendentes (jurisdição do papel), sino de notificações.
6. `routes.init_app(app)` — núcleo + `register()` dos cinco módulos.
7. Dentro de `app.app_context()`:
   - `db.create_all()` (tabelas novas);
   - `_garantir_colunas_multi_tenant()`
   - `_garantir_colunas_usuarios()` (`senha_atualizada_em`)
   - `_garantir_coluna_slug_condominio()`
   - `_garantir_colunas_whitelabel()`
   - `_garantir_coluna_ativo_condominio()`
   - `_seed_condominio_transicao()` (PRP + backfill + `_seed_superadmin()`)
   - `_migrar_sindico_agrupamentos()`
   - `_garantir_colunas_unidades()` / `_pessoas()` / `_reservas()` / `_espacos_comuns`
   - `_garantir_colunas_parceiros()` / `_cupom()`
   - `_garantir_tabela_agendamentos_mudanca()`
   - `_garantir_colunas_registros_acesso()` / `_encomendas()`
8. `_garantir_tabelas_parceiros(app)` chama `db.create_all()` de novo no contexto.

### por que “blueprints” sem a classe Blueprint

Cada arquivo em `app/blueprints/` documenta no docstring: registrar com `app.add_url_rule(regra, "nome_do_endpoint", view)` **sem** `flask.Blueprint`.

`Blueprint.route` geraria endpoints `admin.admin_index`. Os templates chamam `url_for('admin_index')`, `url_for('portaria_acesso_autorizada', auth_id=...)`, etc. A extração só moveu funções; mudar o nome do endpoint quebraria a UI.

`routes.init_app(app)`:

```python
parceiro_routes.register(app)
superadmin_routes.register(app)
sindico_routes.register(app)
admin_routes.register(app)
portaria_routes.register(app)
# em seguida add_url_rule do núcleo (login tenant, cadastro, reservas, clube, ...)
```

### árvore do projeto

```
app/
├── __init__.py              # factory, _garantir_*, seeds, context processor
├── auth.py                  # sessão, slug, decorators, resolução de tenant
├── models.py                # Condominio = tenant raiz
├── routes.py                # núcleo + init_app + helpers _*
├── utils.py                 # planta PRP, tokens, Quill sanitizado, logo parceiro
├── email_service.py
├── drive_api.py             # não ligado às rotas
├── blueprints/
│   ├── __init__.py          # vazio
│   ├── parceiro.py
│   ├── superadmin.py
│   ├── sindico.py
│   ├── admin.py
│   └── portaria.py
├── static/css|js|uploads/
└── templates/
    ├── base.html, auth_base.html, portaria_base.html,
    │   parceiro_base.html, superadmin_base.html
    ├── admin/, morador/, portaria/, includes/
run.py
.env.example
requirements.txt
```

### helpers compartilhados (import tardio)

Vários `_algo` vivem em `routes.py` porque síndico, admin, portaria e o próprio núcleo os usam. Importar no **topo** de `admin.py` puxaria `app.routes` no meio do carregamento de `init_app` → import circular.

Padrão: `from app.routes import _unidade_do_tenant` **dentro** da view. Exemplos:

- admin (mudanças, exclusão, ocorrências): `_agendamento_do_tenant`, `_unidade_do_tenant`, `_validar_data_mudanca`, `_registrar_auditoria`, `_sindico_gerencia_bloco`;
- portaria: `_condominio_id_portaria`, `_criar_notificacao`, `_agendamento_do_tenant`, `_salvar_imagem_upload`;
- síndico: `_blocos_codigo_sindico`, `_sindico_gerencia_bloco`, `_emails_unicos`.

---

<a id="multi-tenant"></a>

## multi-tenant e isolamento

### princípio

Toda entidade **local** (unidade, usuário da equipe, agendamento, espaço, reserva, ocorrência, visitante, encomenda, autorização, notificação, log) é lida/alterada com filtro de `condominio_id` do ator autenticado. Não se usa `Model.query.get_or_404(id)` sem tenant.

Helpers anti-IDOR:

- `routes.py`: `_unidade_do_tenant`, `_usuario_do_tenant`, `_agendamento_do_tenant`, `_ocorrencia_do_tenant`, `_pessoa_do_tenant`, `_espaco_do_tenant`, `_reserva_do_tenant` (join com `EspacoComum.condominio_id`), `_buscar_unidade(bloco, apto, condominio_id)` (sem id de tenant **não consulta**).
- `portaria.py`: `_autorizacao_do_tenant`, `_registro_acesso_do_tenant`, `_encomenda_do_tenant`.

### resolução de tenant (`app/auth.py`)

- Equipe: `usuario.condominio_id` via `condominio_id_obrigatorio()` — fonte de verdade nas rotas administrativas.
- Morador: `unidade.condominio_id`.
- Porta pública: slug da URL + sessão (`tenant_slug`, `cadastro_condominio_id`, `cadastro_slug`).
- `resolver_condominio_id(..., permitir_fallback=True)` cai no PRP só em fluxos públicos legados. Admin/síndico/portaria **não** devem usar esse fallback.
- `_condominio_id_da_sessao()` (esqueci senha) **não** faz fallback PRP — evita vazar existência de e-mail cross-tenant.
- Condomínio inativo: `_resposta_condominio_inativo` / `condominio_suspenso.html`.

### sessão

Três contextos, nunca misturados:

| Contexto | Chaves | Login limpa a sessão? |
|---|---|---|
| Equipe / Super Admin | `user_id`, `role`, `condominio_id`, `tenant_slug` | `login_usuario()` faz `session.clear()` |
| Unidade | `unidade_id`, bloco/apto, `condominio_id`, `tenant_slug` | `login_unidade()` faz `session.clear()` |
| Parceiro | `parceiro_id` | `parceiro_login` faz `session.clear()` |

`get_current_user()` e `get_unidade_logada()` recusam se `session['condominio_id']` divergir do registro (Super Admin é exceção: opera sem tenant obrigatório).

Decorators de equipe local exigem `condominio_id` preenchido e chamam `_sincronizar_sessao_tenant`.

### o que é global de propósito

`Parceiro` e `Cupom` **não** têm `condominio_id`. Um parceiro ativo aparece para moradores de todos os clientes. `ResgateCupom` isola métricas só indiretamente (pela unidade).

### seed de transição

`_seed_condominio_transicao`:

- se não houver condomínio, cria `"PRP Condomínio"` + `ConfiguracaoCondominio`;
- garante `slug=prp` no legado;
- backfill de `condominio_id` em unidades, usuários (exceto `superadmin`), agendamentos e logs;
- chama `_seed_superadmin()`: se não existir Super Admin, cria `username=superadmin` com senha definida no código de seed. **Trocar imediatamente** em qualquer ambiente compartilhado.

`_migrar_sindico_agrupamentos` lê a coluna SQLite legado `bloco_responsavel` (já removida do model) e cria `SindicoAgrupamento`.

---

<a id="autenticacao"></a>

## autenticação, sessão e recuperação de senha

### portas de entrada

| URL | Quem |
|---|---|
| `/c/<slug>/login` | Morador (bloco/apto/senha) e equipe (username/senha) no mesmo tenant |
| `/c/<slug>/sindico/login` | Síndico (alias; equipe também entra pela aba equipe) |
| `/sindico/login`, `/admin/login` | Legado → PRP / tenant da sessão |
| `/superadmin/login` | Só `role=superadmin` |
| `/parceiro` e `/parceiro/login` | Parceiro (`usuario_login` ou e-mail) |

Cadastro público: `/c/<slug>/verificar-unidade` e `/c/<slug>/cadastro-inicial`. Aliases `/verificar-unidade` e `/cadastro-inicial` redirecionam para o slug `prp`.

Equipe autenticada no POST de `tenant_login` é filtrada por `condominio_id == condominio.id` **e** role em admin/assistente/síndico/porteiro — um admin do condomínio A não entra pelo slug do B com o mesmo username (usernames são únicos **globalmente** na tabela `usuarios`).

Após login da equipe: síndico → dashboard do síndico; porteiro → portaria; admin → `admin_dashboard`; assistente → `admin_index`.

Unidade `Pendente`: `verificar_unidade` não completa o login; mostra estado pendente. `Aprovada`/`Registrada`: exige senha e vai para `/atualizar-dados`. Sem cadastro (ou `Reprovada`, que é apagada na hora): vai para cadastro inicial, gravando `cadastro_*` na sessão.

### decorators

| Decorator | Efeito |
|---|---|
| `superadmin_required` | Só Super Admin; senão `/superadmin/login` |
| `admin_required` | Admin local com tenant |
| `admin_or_assistente_required` | Admin ou assistente com tenant |
| `admin_or_sindico_required` | Kanban de ocorrências |
| `sindico_required` | Síndico com tenant |
| `portaria_required` | Porteiro, admin local **ou** Super Admin |
| `unidade_required` | Injeta `unidade` na view |
| `parceiro_required` | `session['parceiro_id']` |
| `acesso_reservas_required` | Unidade ou equipe; **bloqueia porteiro** |
| `gestao_espacos_required` | Admin, assistente ou síndico |

### recuperação de senha

Anti-enumeração: a mensagem é sempre *"Se o e-mail estiver cadastrado, enviaremos instruções..."*. SMTP falho vira aviso genérico, sem confirmar conta.

- Morador: busca só `Unidade`+`Pessoa` responsável/`proprietario_email` **do `condominio_id` da sessão**. Sem tenant na sessão, não resolve.
- Parceiro: só tabela `parceiro`, envio se `status==Ativo`.
- Salts distintos: `recuperacao-morador` / `recuperacao-parceiro`.
- Token (`URLSafeTimedSerializer`, `max_age=3600`): payload `{"email", "condominio_id"}`. A redefinição usa o tenant **do token**, não o da sessão no clique. Payload legado (e-mail puro) é rejeitado no fluxo do morador.
- `set_password()` grava `senha_atualizada_em`. Token emitido antes desse instante é recusado (uso único efetivo).

Parceiro: login por `usuario_login`; fallback e-mail; se login vazio no sucesso, preenche com o e-mail. O parceiro **pode editar o e-mail** no perfil; `usuario_login` não é alterado pelo próprio parceiro (Super Admin edita os dois).

---

<a id="modelo-de-dados"></a>

## modelo de dados

Constantes de domínio em `app/models.py`: `Role`, `StatusUnidade`, `StatusDocumento`, `VinculoPessoa`, `StatusAgendamentoMudanca`, `TipoVisitante`, `StatusEncomenda`, `StatusAutorizacaoAcesso`, `PerfilDestinoNotificacao`, `StatusOcorrencia`, `CategoriaOcorrencia`.

### tenant e white-label

**`Condominio`** (`condominio`): `nome`, `slug` único, `cnpj`, `ativo`, `data_cadastro`. Sem hard delete.

**`ConfiguracaoCondominio`**: `label_agrupamento`, `label_unidade`, `usa_agrupamentos`, `tem_subsindicos`, `fluxo_aprovacao_mudanca` (persistido; runtime de mudança ainda é duplo), `cor_primaria` (`#RRGGBB`, default `#0d6efd`), `logo_filename`.

**`SindicoAgrupamento`**: `usuario_id`, `condominio_id`, `nome_agrupamento`. Um síndico pode ter vários blocos.

### identidade

**`Usuario`**: `username` único global, `password_hash`, `role`, `condominio_id` (NULL só Super Admin), `senha_atualizada_em`. Propriedades `is_superadmin`, `is_admin`, `is_sindico`, `is_assistente`, `is_porteiro`.

**`Unidade`**: `condominio_id`, `bloco`, `apartamento`, senha, `status`, datas, documentos (`documento_drive_id/url/status`, `contrato_locacao_*`), proprietário externo, `notificacao_sindico`, `senha_atualizada_em`. Relacionamentos: pessoas, veículos, resgates, mudanças, acessos, encomendas, ocorrências.

**`Pessoa`**: nome, CPF, vínculo, telefone, e-mail, parentesco, nascimento, `is_responsavel`, `autoriza_interfone`.

**`Veiculo`**: placa, marca, cor.

### áreas comuns

**`EspacoComum`**: `condominio_id`, `nome`, `tipo`, `gerenciado_por` (`admin` ou síndico), `bloco_vinculado`, `apenas_moradores_bloco`, `dias_funcionamento` (csv `seg,ter,...`), `valor_reserva`.

**`Reserva`**: `espaco_id`, `unidade_id` nullable (gestão pode criar sem unidade), `data_reserva`, `status` (Pendente/Aprovada/Recusada/Cancelada), `motivo_reserva`, `valor_pago`. Conflito de data no mesmo espaço: `_existe_reserva_ativa` com lock no espaço — **sem** índice único parcial.

### clube (global)

**`Parceiro`**: empresa, `usuario_login`, `email`, senha, contato, categoria, endereço, descrição HTML, logo, Instagram/Facebook, `ativo` legado + `status` Pendente/Ativo/Bloqueado, `senha_atualizada_em`.

**`Cupom`**: prefixo, validade, `limite_total`, `limite_por_unidade` (default 1), `total_resgatado` (contador atômico), `data_desativacao`. Sem reativação.

**`ResgateCupom`**: `codigo_unico`, `status` Ativo/Utilizado, `data_resgate`, `data_utilizacao`.

### operação e portaria

**`LogAuditoria`**: `condominio_id`, `usuario_id`, `mensagem`.

**`AgendamentoMudanca`**: tipo Entrada/Saída, `data_mudanca`, status (Pendente Síndico / Pendente Administração / Aprovada / Rejeitada / Cancelada), `motivo_rejeicao`, `data_chegada`, `porteiro_id`.

**`Visitante`**: único por (`condominio_id`, `documento`); tipo Visitante/Prestador; `empresa` opcional.

**`RegistroAcesso`**: entrada/saída; `porteiro_id` e `porteiro_saida_id`; uma entrada aberta (`data_saida IS NULL`) por visitante travada na aplicação.

**`Encomenda`**: destinatário, transportadora, rastreio, foto, status Pendente/Entregue, porteiros de recebimento/entrega.

**`AutorizacaoAcesso`**: nome, documento, `data_prevista`, tipo, status Pendente/Concluída/Cancelada.

**`Notificacao`**: `perfil_destino` MORADOR (exige `unidade_id`) ou PORTARIA (`unidade_id` nulo), `lida`.

**`Ocorrencia`**: título, descrição, categoria, status Aberto/Em Andamento/Resolvido, `foto_arquivo`.

---

<a id="fluxos-de-negocio"></a>

## fluxos de negócio por papel

### super admin da plataforma

Login `/superadmin/login`. Layout `superadmin_base.html` (sidebar escura). Dashboard: totais globais de condomínios, parceiros ativos e usuários (exceto o próprio papel).

**Onboarding de cliente** (`POST /superadmin/condominios`): nome, slug (normalizado `[a-z0-9-]+`, único, ≤50), CNPJ, rótulos, flags de agrupamento/subsíndico, fluxo Simples/Dupla (persistido), cor e logo. Cria `Condominio` + `ConfiguracaoCondominio` na mesma transação.

Em seguida, **primeiro admin local** (`POST .../primeiro-admin`): `Usuario` `role=admin` amarrado ao `condominio_id`. É esse admin quem cria assistente, síndico e porteiro.

Edição posterior: dados e config (`slug` imutável); white-label isolado; desativar (`ativo=False`) / reativar. Slug desativado responde a tela de suspensão.

**Parceiros globais:** criar (status `Pendente`, senha inicial `senha123` informada no flash), editar, bloquear (desativa cupons da vitrine em massa) e reativar. Auditoria em bloqueio/reativação.

Logo do condomínio aceita png/jpg/jpeg/gif/webp/**svg** (`_LOGO_EXTENSIONS`) — pendência de XSS em SVG (ver achados). Logo de parceiro **não** aceita SVG.

### admin e assistente do condomínio

Login na aba equipe de `/c/<slug>/login`. Layout `base.html` com cor/logo do tenant.

**Só admin**

- Dashboard executivo (`/admin/dashboard`, Chart.js): unidades registradas, aguardando registro, documentos pendentes (inclui contrato se o responsável é locatário), cadastros por bloco, série de 30 dias, proporção de status — tudo filtrado por `condominio_id`.
- Validação documental: marcar comprovante e/ou contrato `Entregue`, ou ajustar status manualmente.
- Equipe: criar assistente/síndico/porteiro (síndico exige bloco → `SindicoAgrupamento`); alterar senha desses papéis (não a própria nesta tela); revogar acesso (não autoexclusão).
- Clube: **somente analytics** do próprio condomínio (`admin_clube_vantagens`). Sem CRUD de parceiro.
- Menu Portaria (com botão voltar ao painel no `portaria_base.html`).
- Botão Excluir/Resetar cadastro na UI; a rota até aceita assistente no decorator, mas a view devolve “Acesso negado” se `role != admin`. Bloqueia se houver encomenda `Pendente`.

**Admin e assistente**

- Fila `/admin`: abas Aguardando Registro (`Aprovada`), Cadastros Finalizados (`Registrada`), Gestão de Síndicos; aba Equipe só admin.
- `POST /admin/registrar/<id>`: `Aprovada` → `Registrada`.
- Alterar senha da unidade.
- Mudanças: segunda instância (UPDATE condicional) e criação avulsa já `Aprovada`, com as mesmas regras D+3 / domingo.
- Reservas dos espaços `gerenciado_por=admin`.

**Ocorrências:** admin e síndico (`admin_or_sindico_required`). Síndico só vê/atualiza unidades dos seus agrupamentos.

### síndico

Login `/c/<slug>/sindico/login` ou aba equipe. Jurisdição: `_sindico_gerencia_bloco` / `_blocos_codigo_sindico` a partir de `SindicoAgrupamento`.

Dashboard: mapa de todos os aptos dos blocos (planta PRP), inclusive “Aguardando Morador”. Modal único de validação.

**Validação unificada** (`POST /sindico/validar-unidade/<id>`):

- só unidade `Pendente` do próprio tenant e bloco;
- checkboxes `pessoas_reprovadas` + `motivo_pessoa_<id>` (três motivos válidos);
- remove só os marcados; e-mails únicos dos aprovados;
- se restar ≥1 morador → `Aprovada` + e-mail de sucesso ou validação parcial;
- se todos recusados **e** documento/contrato já `Entregue` → **não apaga** a unidade (fica `Pendente`, auditoria) — protege dados validados após um Smart Diff crítico;
- se todos recusados sem documento validado → `delete` da unidade (mapa volta a aguardar).

Rotas pontuais `sindico_aprovar` / `sindico_reprovar` / `sindico_reprovar_pessoa` ainda existem; o fluxo de UI principal é o unificado.

Mudanças: `PENDENTE_SINDICO` → `PENDENTE_ADMINISTRACAO` (ou rejeita com motivo), sempre com UPDATE condicional.

Reservas: só espaços cujo `bloco_vinculado` está na jurisdição.

### porteiro

Dashboard: visitantes no local, prestadores no local, encomendas pendentes, mudanças do dia. Horário operacional em **America/Sao_Paulo** (`zoneinfo`).

**Acesso**

1. Entrada manual: documento normalizado (alfanumérico), nome, tipo, unidade. Reusa `Visitante` pelo documento do tenant; lock no visitante; recusa segunda entrada aberta; notifica o morador.
2. Entrada por autorização: `AutorizacaoAcesso` pendente do dia → cria registro, marca autorização `Concluída`, notifica.
3. Saída: preenche `data_saida` + `porteiro_saida_id`.

**Encomendas:** receber (foto PNG/JPG/WEBP ≤2MB, rastreio), reenviar notificação só se pendente, entregar. Leitura de QR (`html5-qrcode`) na tela.

**Mudança:** check-in só se `Aprovada`, **na data de hoje** (São Paulo), sem `data_chegada` prévia. `/portaria/mudancas` redireciona ao dashboard.

Admin e Super Admin podem abrir a portaria; o template oferece volta ao painel correspondente (`role == 'admin'` | `'superadmin'` — não existe `current_user.perfil`).

### morador (unidade)

1. `/c/<slug>/login` → bloco+apto.
2. Sem cadastro → formulário (`cadastro_morador.html` em `auth_base.html`): pessoas, veículos, senha ≥6, dados do dono se locatário. `salvar_cadastro` cria `Unidade` `Pendente` no `cadastro_condominio_id` da sessão.
3. Síndico valida.
4. `Aprovada`: login com senha; app liberado; documentos ainda podem estar pendentes na administração.
5. Admin marca `Registrada`.
6. Atualização: ver [Smart Diff](#smart-diff).

Depois de aprovado/registrado: clube, reservas, mudanças (cancelar só enquanto pendente), autorizações, ocorrências (foto opcional), notificações, limpar aviso textual do síndico (`notificacao_sindico`).

Ocorrências exigem unidade `Aprovada` ou `Registrada` (`_unidade_pode_abrir_ocorrencia`).

---

<a id="smart-diff"></a>

## smart diff — atualização de cadastro

Objetivo: o morador corrige telefone ou CPF **sem** reabrir aprovação do síndico.

O diff roda **antes** de persistir, comparando o POST com o snapshot no banco (`_requer_nova_aprovacao_sindico`). `_salvar_pessoas_veiculos` continua apagando e recriando pessoas/veículos; por isso o snapshot precisa ser anterior ao save.

**Silencioso** (mantém `Aprovada`/`Registrada`): contato; dados pessoais de quem já existia; marca/cor do veículo com a **mesma placa**; telefone/e-mail do proprietário externo sem troca de nome.

**Crítico** (volta `Pendente`):

- `_houve_add_remove_pessoas` — conjunto de IDs (campo oculto `pessoa_{i}_id`); sem IDs, pareamento por CPF ou nome+nascimento;
- `_houve_add_remove_veiculos` — conjunto de placas normalizadas;
- `_houve_mudanca_proprietario_ou_responsavel` — troca de responsável, vínculo, locatário↔não locatário, ou nome do dono externo.

`_validar_ids_pessoas_unidade` impede ID de outra unidade no form.

Flash crítico vs. silencioso; `data_alteracao` sempre atualiza.

---

<a id="clube-de-vantagens"></a>

## clube de vantagens

Módulo anexo: um catálogo para **toda** a instalação. Gestão de parceiros = Super Admin. Admin do condomínio = relatórios do próprio tenant. Parceiro = portal comercial. Morador = vitrine e resgate.

### ciclo de vida do parceiro

1. Super Admin cria: `Pendente`, `ativo=True`, senha inicial `senha123`.
2. Parceiro loga. Enquanto pendente, vê `parceiro_pendente.html` (sem sidebar) com botão que POSTa `parceiro_aprovar` — **o próprio parceiro se ativa** (`status=Ativo`). Não há gate humano obrigatório além de ter recebido usuário/senha.
3. Super Admin também pode `superadmin_parceiro_ativar`.
4. Ativo: cupons, validação, perfil (incluindo e-mail, logo, redes, descrição Quill sanitizada).
5. Bloqueio: `Bloqueado`, `ativo=False`, cupons `ativo=False`; login recusado. Reativação só Super Admin. Cupom individual desativado pelo parceiro **não** volta.

Há, portanto, dois caminhos para sair de Pendente. No código atual o status funciona mais como “aceite de onboarding” do que como aprovação compliance. Confirmar intenção de produto antes de tratar como bug.

### cupom e resgate

Criação: título, HTML sanitizado (`html_rico_form`: p, br, strong/b, em/i, u), prefixo, validade opcional, limite total opcional, limite por unidade (default 1).

Vitrine do morador: parceiro `Ativo`, cupom `ativo`, validade, tetos.

`clube_vantagens_resgatar`:

1. checagens defensivas;
2. `UPDATE cupom SET total_resgatado = total_resgatado + 1 WHERE id=? AND (limite_total IS NULL OR total_resgatado < limite_total)`;
3. se `rowcount=0`, oferta esgotada (rollback);
4. revalida limite por unidade na mesma transação;
5. gera `PRP-{BLOCO}{APTO}-{PREFIXO}-{SUFIXO}` (até 20 tentativas); falha de código faz rollback para não consumir a vaga;
6. `ResgateCupom` `Ativo`.

QR no cliente (`qrcode.js`) aponta para `/parceiro/validacao?codigo=...`.

Validação: código existe, cupom é deste parceiro, status ainda `Ativo` → `Utilizado` + `data_utilizacao`. Sem reversão. Quem tem o código, valida.

Analytics admin local: joins em `Unidade.condominio_id`. Super Admin vê auditoria cruzada da plataforma.

### lacunas de produto (não implementadas)

Sem associação Parceiro↔Condomínio; sem cobrança/comissão; autoativação do pendente; analytics de negócio raso; limites estáticos (não “1 por mês”); dependência operacional do Super Admin para onboarding.

---

<a id="interface"></a>

## interface, templates e bibliotecas

### bases por contexto (não misturar)

| Base | Quem | Identidade |
|---|---|---|
| `base.html` | morador, síndico, admin, assistente | `--bs-primary` do white-label |
| `portaria_base.html` | portaria | visual próprio + sino |
| `parceiro_base.html` | parceiro ativo | verde `--parceiro-brand-dark` |
| `superadmin_base.html` | plataforma | escuro / accent roxo |
| `auth_base.html` | login, cadastro, recuperação, suspenso | sem app shell |

Sidebars: `nav-pills`, ativo por `request.endpoint`, offcanvas no mobile. Menus de Super Admin **não** entram no `base.html` do cliente (só atalho “Ir para Plataforma” se essa sessão cair ali).

Anti dead-end da portaria: botão voltar conforme `role`.

Padrões: `nav-tabs` em telas densas, modais em vez de tabelas largas, `flash` + alert dismissible, `confirm` em ações perigosas.

Datas: Flatpickr (`type=text` + `.datepicker`), locale pt, valor `Y-m-d`. Evitar `<input type="date">`.

Outras libs CDN: FullCalendar 6 (reservas), Chart.js 4 (dashboards), qrcode.js, html5-qrcode, Quill 1.3 + `quill-rich.js` / sanitização no POST.

Uploads de imagem (ocorrência, encomenda, logo parceiro): png/jpg/jpeg/webp, 2MB, nome aleatorizado.

---

<a id="email"></a>

## e-mail

`app/email_service.py`. Uma conta Gmail (`MAIL_USERNAME` / `MAIL_PASSWORD`) para todos os tenants. Sem fila, timeout 15s, bloqueante no request.

Funções: redefinição (morador/parceiro), reprovação individual (legado), validação sucesso/parcial do síndico, nova reserva, resposta de reserva.

Lote na validação: destinatários = e-mails únicos dos **aprovados**. Falha SMTP não desfaz o commit; só flash.

Assuntos ainda usam o prefixo “PRP Condomínio”, mesmo em instalação white-label.

---

<a id="integracoes-operacao-limitacoes"></a>

## integrações, ambiente e operação

### variáveis (`.env`, não versionado)

| Variável | Uso |
|---|---|
| `SECRET_KEY` | Sessão Flask + tokens. **Obrigatória.** `.env.example` instrui `secrets.token_hex(32)`. |
| `SQLALCHEMY_DATABASE_URI` | Conexão. Default SQLite. |
| `MAIL_USERNAME` / `MAIL_PASSWORD` | SMTP. Ausentes → `RuntimeError` no envio. |

Não existe `DATABASE_URL` no código. Arquivos locais no `.gitignore`: `.env`, `client_secret.json`, `token.json`, `*.db`, `instance/`, uploads.

### Google Drive

`InstalledAppFlow.run_local_server(port=8080)` — fluxo de aplicativo instalado, inadequado para servidor headless. `DRIVE_FOLDER_ID` hardcoded. Arquivo público (`anyone/reader`). Refresh engolido em `except Exception`.

`upload_to_drive()` **não é chamado** por nenhuma rota. `salvar_cadastro` não envia arquivo; só zera ou ignora colunas `*_drive_id`. O admin marca status documental sem conferir um binário no Drive. Risco latente para quando a integração for religada.

### limitações operacionais conhecidas

- SMTP síncrono: request pode esperar até 15s; sem retry/fila; um remetente para todos os clientes.
- SQLite: escritor único; boot com vários workers pode colidir em `_garantir_colunas_*` e seeds (ver achados).
- Planta de blocos única (PRP) para validação de cadastro de qualquer tenant.
- Unicidade `bloco+apartamento` global, não por condomínio.
- Sem CSRF token / Flask-WTF; cookies sem `SESSION_COOKIE_SECURE` explícito (depende do default `SameSite=Lax` do browser).
- Username de `Usuario` único na plataforma inteira.

---

<a id="migracoes"></a>

## migrações leves e consistência

Não há Alembic. Cada boot introspecta o schema (`inspect`) e faz `ALTER TABLE ... ADD COLUMN` só do que falta — idempotente.

Regras:

- SQLite não relaxa NOT NULL com `ALTER COLUMN`; `_garantir_colunas_reservas` já recriou a tabela (RENAME/CREATE/INSERT/DROP) uma vez para tornar `unidade_id` nullable. Não copiar esse padrão sem necessidade.
- Evitar `DEFAULT CURRENT_TIMESTAMP` em ADD COLUMN no SQLite.
- Índices únicos **parciais** (`WHERE status IN (...)`) foram **retirados** do model para caber no MySQL. A exclusão equivalente está na aplicação (`_existe_reserva_ativa`, `_entrada_aberta_visitante`).
- Soft delete para histórico (cupom, parceiro, condomínio). Hard delete: unidade (admin, sem encomenda pendente) e revogação de usuário da equipe.

Nova coluna: campo no model **e** `_garantir_*` correspondente.

---

<a id="catalogo-rotas"></a>

## catálogo de rotas

### núcleo (`routes.py`)

`/`, `/c/<slug>/login`, `/c/<slug>/verificar-unidade`, `/c/<slug>/cadastro-inicial`, aliases legados, `/esqueci_senha`, `/redefinir_senha/<token>`, `/atualizar-dados`, `/salvar-cadastro`, `/limpar-notificacao-sindico`, `/sair`, `/clube_vantagens`, `POST .../resgatar/<id>`, `/reservas` e sub-rotas (solicitar, gestao/criar, responder, pagamento, cancelar, espacos/salvar), `GET /api/reservas/eventos`, `/mudancas`, `/morador/autorizacoes`, `/morador/ocorrencias`, `/notificacoes`.

### superadmin

`/superadmin/login|logout`, `/superadmin`, `/superadmin/condominios` (+ primeiro-admin, whitelabel, editar, desativar, ativar), `/superadmin/parceiros` (+ criar, editar, bloquear, ativar).

### admin

`/admin/login` (redirect PRP), `/admin/logout`, `/admin/dashboard`, `/admin`, clube + analytics (analytics redireciona ao clube), usuarios novo/excluir/alterar-senha, registrar, alterar senha unidade, excluir unidade, validar documentos, salvar proprietário, ocorrências, mudanças.

### síndico

`/c/<slug>/sindico/login`, `/sindico/login` legado, logout, `/sindico`, aprovar/reprovar/reprovar-pessoa/validar-unidade, `/sindico/mudancas`.

### portaria

`/portaria`, `/portaria/dashboard`, logout, acesso (entrada, autorizada, saida), encomendas (receber, entregar, notificar), `POST /portaria/mudanca/<id>/chegar`.

### parceiro

`/parceiro`, `/parceiro/login`, logout, esqueci/redefinir senha, dashboard, validacao, validar_codigo, aprovar, cupons (+ criar, desativar), perfil (+ editar).

---

<a id="achados-tecnicos-riscos"></a>

## achados técnicos, sprint de segurança e pendências

Verificação adversarial no código. Itens **[CORRIGIDO]** descrevem a falha histórica e o estado atual. Itens sem a tag permanecem abertos.

### resumo da sprint

`SECRET_KEY` obrigatória; XSS da portaria fora de JS inline; tokens de reset com tenant + `senha_atualizada_em`; `session.clear()` em unidade e parceiro; corridas de cupom e mudança com UPDATE condicional; trava de aplicação para reserva e visitante (em vez de índice parcial SQLite, por causa do MySQL); documentos validados preservados na reprovação total; exclusão de unidade bloqueada com encomenda pendente; kanban do síndico recortado por bloco.

### crítico

#### 1. `SECRET_KEY` com fallback hardcoded **[CORRIGIDO]**

Histórico: fallback `"dev-change-me-in-production"` permitia forjar cookie de qualquer papel.  
**Agora:** boot aborta sem a variável.

#### 2. XSS em nome de visitante no `onsubmit` **[CORRIGIDO]**

Histórico: interpolação em atributo JS na portaria.  
**Agora:** `data-confirm-mensagem` + listener; Jinja escapa HTML.

### alto

#### 3. reset de senha cross-tenant **[CORRIGIDO]**

Token carrega `condominio_id`; resolução usa o tenant do token. Link legado sem tenant é recusado.

#### 4. reuso do token na janela de 1h **[CORRIGIDO]**

`senha_atualizada_em` em `Unidade`, `Parceiro` e `Usuario`; `set_password()` atualiza o carimbo.

#### 5. upload de logo SVG (XSS armazenado) — **aberto**

`_salvar_logo_condominio` ainda permite `.svg`. O arquivo é público em `/static/uploads/logos/` e aparece no login do tenant. Logo de parceiro já restringe a raster.  
Sugestão: tirar `svg` da allowlist, ou sanitizar e CSP.

#### 6. double-booking de reserva **[CORRIGIDO]** (estratégia atualizada)

Histórico: duas requisições passavam no SELECT. A documentação antiga citava índice único parcial `ux_reserva_espaco_data_ativa`.  
**Agora:** esse índice **não existe** (MySQL). `_existe_reserva_ativa` faz `with_for_update()` no `EspacoComum` e consulta reservas Pendente/Aprovada. Comentário equivalente em `_garantir_colunas_reservas`.

#### 7. corrida no resgate de cupom **[CORRIGIDO]**

`UPDATE` atômico em `total_resgatado` + revalidação do limite por unidade.

#### 8. apagar unidade com documentos já Entregue **[CORRIGIDO]**

`sindico_validar_unidade` mantém a unidade `Pendente` se comprovante ou contrato já estavam `Entregue`.

#### 9. exclusão de unidade com encomenda pendente **[CORRIGIDO]**

`admin_excluir_unidade` recusa; histórico de portaria não tem cascade cego a partir da unidade.

#### 10. migrações concorrentes no boot — **aberto**

Vários workers chamando `create_app()` podem disputar o mesmo `ALTER TABLE` no SQLite (`duplicate column`) e derrubar o worker.  
Sugestão: `--preload`, comando de migrate único, ou `try/except OperationalError`.

### médio

#### 11–12. sessão mista Super Admin / unidade / parceiro **[CORRIGIDO]**

`session.clear()` em `login_unidade` e no login do parceiro.

#### 13. kanban do síndico sem recorte de bloco **[CORRIGIDO]**

Listagem e update usam `_sindico_gerencia_bloco` / join em `Unidade.bloco`.

#### 14. enumeração de e-mail por timing — **aberto**

SMTP só dispara se o e-mail existe; a resposta textual é igual, o tempo não. Vale para morador e parceiro.

#### 15. CSRF em POSTs destrutivos — **aberto**

Sem Flask-WTF. `admin_excluir_unidade` e troca de senha da unidade são form POST. Atenuado por `SameSite=Lax` padrão, sem defesa própria da app.

#### 16. corrida aprovar/rejeitar mudança **[CORRIGIDO]**

`UPDATE ... WHERE status = esperado` + `rowcount` no síndico e na administração.

#### 17. SQLite `database is locked` — **aberto**

Sem WAL explícito no factory, sem retry em commit, sem `errorhandler` de `OperationalError`. O `timeout=15` citado em versões antigas desta documentação **não** está em `create_app()` hoje.

#### 18. duas entradas abertas do mesmo visitante **[CORRIGIDO]** (estratégia atualizada)

Índice parcial `ux_registro_acesso_aberto` **removido**. `_entrada_aberta_visitante` usa `with_for_update()` no `Visitante`.

#### 19. seed Super Admin/PRP em corrida — **aberto**

Check-then-insert sem lock; segundo worker pode levar `IntegrityError` no boot.

### baixo

#### 20. constraint única de reserva — coberto pelo item 6 (aplicação, não índice parcial).

#### 21. f-string com nome de tabela nas migrações — **aberto** (hoje só tuplas fixas; allowlist evitaria regressão).

#### 22. imports mortos em `routes.py` — **aberto**

`from html import escape`, `HTMLParser` e `import re` restam após a extração do sanitizador para `utils.py`. `re` não é usado no arquivo.

#### 23. `from datetime import date` em `admin.py`

O módulo usa `func.date` (SQLAlchemy) e `.date()` de `datetime`. O símbolo `date` importado provavelmente é morto (confirmar com linter).

#### 24. `AgendamentoMudanca` importado em `portaria.py`

A view usa `_agendamento_do_tenant` + `StatusAgendamentoMudanca`; o model importado no topo não é referenciado nas views.

---

## como usar este documento

- Regras operacionais para o Cursor/agente: `.cursorrules` (mais curto, normativo).
- Este arquivo: contexto de produto, o “porquê” das decisões, o que a sprint já fechou e o que ainda é dívida.
- Ao mudar schema: model + `_garantir_*` + forms + isolamento por tenant.
- Ao mudar rota: preservar o nome do endpoint ou atualizar todos os `url_for`/templates.
- Ao falar de fluxo de mudança “Simples”: só está persistido; o código de aprovação ainda é duplo.
- Ao falar de documentos no Drive: colunas prontas, upload desconectado.
