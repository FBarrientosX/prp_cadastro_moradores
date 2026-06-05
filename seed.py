"""Script de inicialização do banco de dados com usuários padrão."""

from app import create_app, db
from app.models import Pessoa, Role, StatusUnidade, Unidade, Usuario, Veiculo, VinculoPessoa

USUARIOS_INICIAIS = [
    {
        "username": "admin",
        "password": "Fbbx040991!",
        "role": Role.ADMIN,
        "bloco_responsavel": None,
    },
    {
        "username": "assistente",
        "password": "assistente123",
        "role": Role.ASSISTENTE,
        "bloco_responsavel": None,
    },
    {
        "username": "sindico_b1",
        "password": "sindico_b1_0193",
        "role": Role.SINDICO,
        "bloco_responsavel": "Bloco 1",
    },
    {
        "username": "sindico_b2",
        "password": "sindico_b2_3820",
        "role": Role.SINDICO,
        "bloco_responsavel": "Bloco 2",
    },
    {
        "username": "sindico_b3",
        "password": "sindico_b3_9543",
        "role": Role.SINDICO,
        "bloco_responsavel": "Bloco 3",
    },
    {
        "username": "sindico_b4",
        "password": "sindico_b4_8463",
        "role": Role.SINDICO,
        "bloco_responsavel": "Bloco 4",
    },
    {
        "username": "sindico_b5",
        "password": "sindico_b5_8917",
        "role": Role.SINDICO,
        "bloco_responsavel": "Bloco 5",
    },
    {
        "username": "sindico_b6",
        "password": "sindico_b6_9017",
        "role": Role.SINDICO,
        "bloco_responsavel": "Bloco 6",
    },
    {
        "username": "sindico_b7",
        "password": "sindico_b7_0192",
        "role": Role.SINDICO,
        "bloco_responsavel": "Bloco 7",
    },
    {
        "username": "sindico_b8",
        "password": "sindico_b8_5673",
        "role": Role.SINDICO,
        "bloco_responsavel": "Bloco 8",
    },
]


def criar_usuario_se_nao_existir(dados):
    existente = Usuario.query.filter_by(username=dados["username"]).first()
    if existente:
        return existente, False

    usuario = Usuario(
        username=dados["username"],
        role=dados["role"],
        bloco_responsavel=dados["bloco_responsavel"],
    )
    usuario.set_password(dados["password"])
    db.session.add(usuario)
    return usuario, True


def popular_unidades_teste():
    criadas = []
    ignoradas = []

    cenarios = [
        {
            "bloco": "1",
            "apartamento": "101",
            "status": StatusUnidade.PENDENTE,
            "notificacao_sindico": None,
            "pessoas": [
                {
                    "nome_completo": "Morador Teste 1",
                    "cpf": "111.111.111-11",
                    "vinculo": VinculoPessoa.PROPRIETARIO,
                    "telefone": "11911111111",
                    "email": "morador1.teste@example.com",
                    "parentesco": None,
                    "is_responsavel": True,
                },
                {
                    "nome_completo": "Morador Teste 2",
                    "cpf": "222.222.222-22",
                    "vinculo": VinculoPessoa.MORADOR,
                    "telefone": "11922222222",
                    "email": "morador2.teste@example.com",
                    "parentesco": "Cônjuge",
                    "is_responsavel": False,
                },
            ],
            "veiculos": [
                {"placa": "TES1A01", "marca": "Fiat", "cor": "Prata"},
            ],
        },
        {
            "bloco": "2",
            "apartamento": "201",
            "status": StatusUnidade.APROVADA,
            "notificacao_sindico": None,
            "pessoas": [
                {
                    "nome_completo": "Morador Teste 3",
                    "cpf": "333.333.333-33",
                    "vinculo": VinculoPessoa.PROPRIETARIO,
                    "telefone": "11933333333",
                    "email": "morador3.teste@example.com",
                    "parentesco": None,
                    "is_responsavel": True,
                }
            ],
            "veiculos": [],
        },
        {
            "bloco": "3",
            "apartamento": "301",
            "status": StatusUnidade.REGISTRADA,
            "notificacao_sindico": None,
            "pessoas": [
                {
                    "nome_completo": "Morador Teste 4",
                    "cpf": "444.444.444-44",
                    "vinculo": VinculoPessoa.PROPRIETARIO,
                    "telefone": "11944444444",
                    "email": "morador4.teste@example.com",
                    "parentesco": None,
                    "is_responsavel": True,
                },
                {
                    "nome_completo": "Morador Teste 5",
                    "cpf": "555.555.555-55",
                    "vinculo": VinculoPessoa.MORADOR,
                    "telefone": "11955555555",
                    "email": "morador5.teste@example.com",
                    "parentesco": "Filho",
                    "is_responsavel": False,
                },
                {
                    "nome_completo": "Morador Teste 6",
                    "cpf": "666.666.666-66",
                    "vinculo": VinculoPessoa.MORADOR,
                    "telefone": "11966666666",
                    "email": "morador6.teste@example.com",
                    "parentesco": "Filha",
                    "is_responsavel": False,
                },
            ],
            "veiculos": [
                {"placa": "TES3B01", "marca": "Volkswagen", "cor": "Branco"},
                {"placa": "TES3B02", "marca": "Chevrolet", "cor": "Preto"},
            ],
        },
        {
            "bloco": "4",
            "apartamento": "401",
            "status": StatusUnidade.PENDENTE,
            "notificacao_sindico": (
                "O cadastro do morador João foi reprovado. Motivo: Não é morador. "
                "Procure o síndico."
            ),
            "pessoas": [
                {
                    "nome_completo": "Morador Teste 7",
                    "cpf": "777.777.777-77",
                    "vinculo": VinculoPessoa.PROPRIETARIO,
                    "telefone": "11977777777",
                    "email": "morador7.teste@example.com",
                    "parentesco": None,
                    "is_responsavel": True,
                }
            ],
            "veiculos": [],
        },
    ]

    for cenario in cenarios:
        existente = Unidade.query.filter_by(
            bloco=cenario["bloco"], apartamento=cenario["apartamento"]
        ).first()
        identificador = f"Bloco {cenario['bloco']} Apto {cenario['apartamento']}"

        if existente:
            ignoradas.append(identificador)
            continue

        unidade = Unidade(
            bloco=cenario["bloco"],
            apartamento=cenario["apartamento"],
            status=cenario["status"],
            notificacao_sindico=cenario["notificacao_sindico"],
        )
        unidade.set_password("senha123")
        db.session.add(unidade)
        db.session.flush()

        for pessoa in cenario["pessoas"]:
            db.session.add(
                Pessoa(
                    unidade_id=unidade.id,
                    nome_completo=pessoa["nome_completo"],
                    cpf=pessoa["cpf"],
                    vinculo=pessoa["vinculo"],
                    telefone=pessoa["telefone"],
                    email=pessoa["email"],
                    parentesco=pessoa["parentesco"],
                    data_nascimento=None,
                    is_responsavel=pessoa["is_responsavel"],
                    autoriza_interfone=True,
                )
            )

        for veiculo in cenario["veiculos"]:
            db.session.add(
                Veiculo(
                    unidade_id=unidade.id,
                    placa=veiculo["placa"],
                    marca=veiculo["marca"],
                    cor=veiculo["cor"],
                )
            )

        criadas.append(identificador)

    return criadas, ignoradas


def seed():
    app = create_app()
    criados = []
    ignorados = []
    unidades_criadas = []
    unidades_ignoradas = []

    with app.app_context():
        for dados in USUARIOS_INICIAIS:
            _, foi_criado = criar_usuario_se_nao_existir(dados)
            if foi_criado:
                criados.append(dados)
            else:
                ignorados.append(dados["username"])

        unidades_criadas, unidades_ignoradas = popular_unidades_teste()
        db.session.commit()

    print()
    print("=" * 50)
    print("  Banco inicializado com sucesso!")
    print("=" * 50)
    print()
    print("Logins disponíveis:")
    print()
    print("  Administrador:")
    print("    Usuário: admin")
    print("    Senha:   Fbbx040991!")
    print("    Bloco:   (nenhum)")
    print()
    print("  Assistente:")
    print("    Usuário: assistente")
    print("    Senha:   assistente123")
    print("    Bloco:   (nenhum)")
    print()
    print("  Síndicos:")
    for dados in USUARIOS_INICIAIS:
        if dados["role"] == Role.SINDICO:
            print(
                f"    Usuário: {dados['username']}  |  "
                f"Senha: {dados['password']}  |  "
                f"Bloco responsável: {dados['bloco_responsavel']}"
            )
    print()

    if criados:
        print(f"  {len(criados)} usuário(s) criado(s) nesta execução.")
    else:
        print("  Nenhum usuário novo criado (todos já existiam).")

    if ignorados:
        print(f"  Ignorados (já existiam): {', '.join(ignorados)}")

    print()
    print("Dados de teste de unidades:")
    if unidades_criadas:
        print(f"  {len(unidades_criadas)} unidade(s) de teste criada(s):")
        for unidade in unidades_criadas:
            print(f"    - {unidade}")
    else:
        print("  Nenhuma unidade de teste nova criada (todas já existiam).")

    if unidades_ignoradas:
        print(f"  Unidades ignoradas (já existiam): {', '.join(unidades_ignoradas)}")

    print()
    print("=" * 50)


if __name__ == "__main__":
    seed()
