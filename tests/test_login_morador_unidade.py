import unittest

from app import create_app, db
from app.models import Condominio, StatusUnidade, Unidade


class LoginMoradorUnidadeTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(
            {
                "SECRET_KEY": "teste-login-morador",
                "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
                "TESTING": True,
                "WTF_CSRF_ENABLED": False,
            }
        )
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

        self.condominio = Condominio.query.filter_by(slug="prp").first()
        self.assertIsNotNone(self.condominio)

        unidade = Unidade(
            condominio_id=self.condominio.id,
            bloco="6",
            apartamento="703",
            status=StatusUnidade.APROVADA,
        )
        unidade.set_password("senha123")
        db.session.add(unidade)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_status_unidade_cadastrada_exige_senha(self):
        resposta = self.client.get(
            "/c/prp/status-unidade",
            query_string={"bloco": "6", "apartamento": "703"},
        )
        self.assertEqual(resposta.status_code, 200)
        dados = resposta.get_json()
        self.assertTrue(dados["ok"])
        self.assertTrue(dados["exige_senha"])
        self.assertTrue(dados["cadastrada"])
        self.assertEqual(resposta.headers.get("Cache-Control"), "no-store")

    def test_status_unidade_sem_cadastro_nao_exige_senha(self):
        resposta = self.client.get(
            "/c/prp/status-unidade",
            query_string={"bloco": "6", "apartamento": "301"},
        )
        self.assertEqual(resposta.status_code, 200)
        dados = resposta.get_json()
        self.assertTrue(dados["ok"])
        self.assertFalse(dados["exige_senha"])
        self.assertFalse(dados["cadastrada"])

    def test_verificar_unidade_cadastrada_sem_senha_pede_senha(self):
        resposta = self.client.post(
            "/c/prp/verificar-unidade",
            data={"bloco": "6", "apartamento": "703"},
        )
        self.assertEqual(resposta.status_code, 200)
        html = resposta.get_data(as_text=True)
        self.assertIn("Senha da unidade", html)
        self.assertIn("id=\"campo-senha-unidade\"", html)
        self.assertNotIn("hidden", html.split("id=\"campo-senha-unidade\"")[1][:80])

    def test_trocar_para_unidade_sem_cadastro_segue_para_cadastro(self):
        self.client.post(
            "/c/prp/verificar-unidade",
            data={"bloco": "6", "apartamento": "703"},
        )
        resposta = self.client.post(
            "/c/prp/verificar-unidade",
            data={"bloco": "6", "apartamento": "301"},
            follow_redirects=False,
        )
        self.assertEqual(resposta.status_code, 302)
        self.assertIn("/c/prp/cadastro-inicial", resposta.headers.get("Location", ""))

    def test_trocar_para_unidade_sem_cadastro_ignora_senha_residual(self):
        resposta = self.client.post(
            "/c/prp/verificar-unidade",
            data={"bloco": "6", "apartamento": "301", "senha": "qualquer"},
            follow_redirects=False,
        )
        self.assertEqual(resposta.status_code, 302)
        self.assertIn("/c/prp/cadastro-inicial", resposta.headers.get("Location", ""))

    def test_get_verificar_unidade_volta_ao_login(self):
        resposta = self.client.get("/c/prp/verificar-unidade", follow_redirects=False)
        self.assertEqual(resposta.status_code, 302)
        self.assertIn("/c/prp/login", resposta.headers.get("Location", ""))

    def test_login_sempre_inclui_campo_senha_e_script_de_revalidacao(self):
        resposta = self.client.get("/c/prp/login")
        self.assertEqual(resposta.status_code, 200)
        html = resposta.get_data(as_text=True)
        self.assertIn("id=\"campo-senha-unidade\"", html)
        self.assertIn("status-unidade", html)
        self.assertIn("aoTrocarUnidade", html)
        self.assertIn("apartamentoSelect.addEventListener(\"change\"", html)


if __name__ == "__main__":
    unittest.main()
