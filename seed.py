"""Script de inicialização do banco de dados com usuários padrão."""

from app import create_app, db
from app.models import Role, Usuario

USUARIOS_INICIAIS = [
    {
        "username": "admin",
        "password": "Fbbx040991!",
        "role": Role.ADMIN,
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


def seed():
    app = create_app()
    criados = []
    ignorados = []

    with app.app_context():
        for dados in USUARIOS_INICIAIS:
            _, foi_criado = criar_usuario_se_nao_existir(dados)
            if foi_criado:
                criados.append(dados)
            else:
                ignorados.append(dados["username"])

        if criados:
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
    print("=" * 50)


if __name__ == "__main__":
    seed()
