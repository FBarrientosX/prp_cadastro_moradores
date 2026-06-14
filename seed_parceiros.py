"""Script para popular dados de teste do Clube de Vantagens."""

from datetime import date, timedelta

from werkzeug.security import generate_password_hash

from app import create_app, db
from app.models import Cupom, Parceiro


def seed_parceiros():
    app = create_app()

    with app.app_context():
        if Parceiro.query.first():
            print("Parceiros já cadastrados. Seed cancelado para evitar duplicação.")
            return

        parceiros_seed = [
            {
                "nome_empresa": "Pizzaria do Bairro",
                "email": "pizza@teste.com",
                "telefone": "11990000001",
                "categoria": "Alimentação",
                "cupons": [
                    {
                        "titulo": "15% OFF em pizzas grandes",
                        "descricao": "Desconto válido para pedidos no balcão e retirada.",
                        "codigo_prefixo": "PIZZA15",
                    },
                    {
                        "titulo": "Frete Grátis no bairro",
                        "descricao": "Frete gratuito para pedidos acima de R$ 50,00.",
                        "codigo_prefixo": "PIZFRETE",
                    },
                ],
            },
            {
                "nome_empresa": "Farmácia Saúde",
                "email": "farmacia@teste.com",
                "telefone": "11990000002",
                "categoria": "Saúde",
                "cupons": [
                    {
                        "titulo": "10% OFF em medicamentos genéricos",
                        "descricao": "Não cumulativo com outros descontos promocionais.",
                        "codigo_prefixo": "SAUDE10",
                    },
                    {
                        "titulo": "Kit bem-estar com desconto",
                        "descricao": "Aproveite 20% no kit de vitaminas selecionadas.",
                        "codigo_prefixo": "VITAKIT",
                    },
                ],
            },
            {
                "nome_empresa": "PetShop Cão Feliz",
                "email": "petshop@teste.com",
                "telefone": "11990000003",
                "categoria": "Serviços",
                "cupons": [
                    {
                        "titulo": "Banho com 20% OFF",
                        "descricao": "Válido para cães de pequeno e médio porte.",
                        "codigo_prefixo": "BANHO20",
                    },
                    {
                        "titulo": "Frete grátis em ração",
                        "descricao": "Entrega sem custo para compras acima de R$ 120,00.",
                        "codigo_prefixo": "PETFRETE",
                    },
                ],
            },
        ]

        validade_padrao = date.today() + timedelta(days=60)

        for dados_parceiro in parceiros_seed:
            parceiro = Parceiro(
                nome_empresa=dados_parceiro["nome_empresa"],
                email=dados_parceiro["email"],
                senha_hash=generate_password_hash("senha123"),
                telefone=dados_parceiro["telefone"],
                categoria=dados_parceiro["categoria"],
                ativo=True,
            )
            db.session.add(parceiro)
            db.session.flush()

            for dados_cupom in dados_parceiro["cupons"]:
                db.session.add(
                    Cupom(
                        parceiro_id=parceiro.id,
                        titulo=dados_cupom["titulo"],
                        descricao=dados_cupom["descricao"],
                        codigo_prefixo=dados_cupom["codigo_prefixo"],
                        data_validade=validade_padrao,
                        ativo=True,
                    )
                )

        db.session.commit()
        print("Seed de parceiros e cupons executado com sucesso.")


if __name__ == "__main__":
    seed_parceiros()
