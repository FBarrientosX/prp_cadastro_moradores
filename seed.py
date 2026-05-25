"""Script de inicialização do banco de dados com usuários padrão."""

from app import create_app, db
from app.models import Role, Usuario

USUARIOS_INICIAIS = [
    {
        "username": "admin",
        "password": "admin123",
        "role": Role.ADMIN,
        "bloco_responsavel": None,
    },
    *[
        {
            "username": f"sindico_b{n}",
            "password": "sindico123",
            "role": Role.SINDICO,
            "bloco_responsavel": f"Bloco {n}",
        }
        for n in range(1, 9)
    ],
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
    print("    Senha:   admin123")
    print("    Bloco:   (nenhum)")
    print()
    print("  Síndicos (senha padrão: sindico123):")
    for n in range(1, 9):
        print(f"    Usuário: sindico_b{n}  |  Bloco responsável: Bloco {n}")
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
