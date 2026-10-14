"""Vizinsync Agent: envia moradores e credenciais às controladoras da portaria.

Rode no computador da portaria, na mesma rede dos equipamentos:

    pip install requests
    python vizinsync_agent.py

A API e o token já vêm preenchidos quando o arquivo é baixado em
Controle de Acesso. Variáveis opcionais: VIZINSYNC_API_URL, VIZINSYNC_API_TOKEN,
VIZINSYNC_INTERVALO (segundos) e VIZINSYNC_DRY_RUN=1 para não falar com o hardware.
Se o IP da controladora não responder, o ciclo também entra em simulação e
não marca o morador como sincronizado.
"""

import base64
import json
import os
import socket
import time
from pathlib import Path

import requests

API_URL = os.environ.get("VIZINSYNC_API_URL") or "__VIZINSYNC_API_URL__"
API_TOKEN = os.environ.get("VIZINSYNC_API_TOKEN") or "__VIZINSYNC_API_TOKEN__"
INTERVALO = int(os.environ.get("VIZINSYNC_INTERVALO") or "60")
ARQUIVO_ESTADO = Path(__file__).with_name("agent_state.json")


def usuario_no_escopo(usuario, escopo):
    """Portaria geral recebe todos. Bloco específico recebe o bloco e a equipe ADM."""
    texto = str(escopo or "").strip()
    if not texto or texto.upper() == "GERAL":
        return True
    if usuario.get("setor_interno"):
        return True
    return str(usuario.get("bloco") or "") == texto


def selecionar_delta(equipamento, usuarios, cache):
    """Quem mudou ou entrou, e quem saiu do escopo desde o último envio bem-sucedido."""
    relevantes = [
        usuario
        for usuario in usuarios
        if usuario_no_escopo(usuario, equipamento.get("bloco_escopo"))
    ]
    estado = cache.get(str(equipamento.get("id")), {})
    enviar = [
        usuario
        for usuario in relevantes
        if estado.get(str(usuario.get("id_interno"))) != usuario.get("hash_versao")
    ]
    atuais = {str(usuario.get("id_interno")) for usuario in relevantes}
    remover = [usuario_id for usuario_id in estado if usuario_id not in atuais]
    return enviar, remover


def gravar_hashes(cache, equipamento_id, usuarios_ok, removidos):
    estado = cache.setdefault(str(equipamento_id), {})
    for usuario in usuarios_ok:
        estado[str(usuario.get("id_interno"))] = usuario.get("hash_versao")
    for usuario_id in removidos:
        estado.pop(str(usuario_id), None)


def carregar_estado():
    if not ARQUIVO_ESTADO.is_file():
        return {}
    try:
        dados = json.loads(ARQUIVO_ESTADO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dados if isinstance(dados, dict) else {}


def salvar_estado(cache):
    ARQUIVO_ESTADO.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _porta_aberta(ip, porta, timeout=2):
    try:
        with socket.create_connection((ip, int(porta)), timeout=timeout):
            return True
    except OSError:
        return False


def _forcar_simulacao():
    return os.environ.get("VIZINSYNC_DRY_RUN") == "1"


class ControlIdDriver:
    """API HTTP pública das leitoras Control iD (login.fcgi, objetos e foto)."""

    def __init__(self, equipamento):
        self.base = f"http://{equipamento['ip_local']}:{int(equipamento.get('porta') or 80)}"
        self.usuario = equipamento.get("usuario_equipamento") or "admin"
        self.senha = equipamento.get("senha_equipamento") or "admin"
        self.sessao = ""

    def _url(self, caminho):
        separador = "&" if "?" in caminho else "?"
        return f"{self.base}/{caminho}{separador}session={self.sessao}"

    def _post(self, caminho, payload=None, corpo=None, content_type=None, timeout=15):
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        elif payload is not None:
            headers["Content-Type"] = "application/json"
        resposta = requests.post(
            self._url(caminho) if self.sessao else f"{self.base}/{caminho}",
            json=payload if corpo is None else None,
            data=corpo,
            headers=headers,
            timeout=timeout,
        )
        if resposta.status_code >= 400:
            raise RuntimeError(f"Control iD respondeu {resposta.status_code}.")
        return resposta

    def login(self):
        resposta = requests.post(
            f"{self.base}/login.fcgi",
            json={"login": self.usuario, "password": self.senha},
            timeout=8,
        )
        if resposta.status_code >= 400:
            raise RuntimeError("Control iD recusou o login.")
        dados = resposta.json()
        self.sessao = str(dados.get("session") or "")
        if not self.sessao:
            raise RuntimeError("Control iD não devolveu sessão.")

    def upsert_usuario(self, usuario):
        valor = {
            "id": int(usuario["id_interno"]),
            "name": str(usuario.get("nome") or "")[:100],
            "registration": "".join(
                caractere for caractere in str(usuario.get("cpf") or "") if caractere.isdigit()
            )[:20]
            or str(usuario["id_interno"]),
        }
        try:
            self._post("create_objects.fcgi", {"object": "users", "values": [valor]})
        except RuntimeError:
            self._post(
                "modify_objects.fcgi",
                {
                    "object": "users",
                    "values": [valor],
                    "where": {"users": {"id": valor["id"]}},
                },
            )
        try:
            self._post(
                "destroy_objects.fcgi",
                {"object": "cards", "where": {"cards": {"user_id": valor["id"]}}},
            )
        except RuntimeError:
            pass
        cartoes = []
        for tag in usuario.get("tags_rfid") or []:
            codigo = str(tag).strip()
            if not codigo:
                continue
            item = {"user_id": valor["id"]}
            item["value"] = int(codigo) if codigo.isdigit() else codigo
            cartoes.append(item)
        if cartoes:
            self._post("create_objects.fcgi", {"object": "cards", "values": cartoes})
        self._enviar_foto(valor["id"], usuario.get("url_foto_facial") or "")

    def _enviar_foto(self, usuario_id, url):
        if not url:
            return
        foto = requests.get(url, timeout=15)
        if foto.status_code >= 400 or not foto.content:
            raise RuntimeError("Não foi possível baixar a foto facial.")
        self._post(
            f"user_set_image.fcgi?user_id={int(usuario_id)}&timestamp={int(time.time())}",
            corpo=foto.content,
            content_type="application/octet-stream",
            timeout=20,
        )

    def remover(self, usuario_id):
        self._post(
            "destroy_objects.fcgi",
            {"object": "users", "where": {"users": {"id": int(usuario_id)}}},
        )


class IntelbrasDriver:
    """API CGI das controladoras Intelbras (usuário, cartão e face)."""

    def __init__(self, equipamento):
        self.base = f"http://{equipamento['ip_local']}:{int(equipamento.get('porta') or 80)}"
        self.auth = requests.auth.HTTPDigestAuth(
            equipamento.get("usuario_equipamento") or "admin",
            equipamento.get("senha_equipamento") or "admin",
        )

    def _post(self, caminho, payload):
        resposta = requests.post(
            f"{self.base}{caminho}",
            json=payload,
            auth=self.auth,
            timeout=15,
        )
        if resposta.status_code >= 400:
            raise RuntimeError(f"Intelbras respondeu {resposta.status_code}.")
        return resposta

    def login(self):
        resposta = requests.get(
            f"{self.base}/cgi-bin/magicBox.cgi?action=getDeviceType",
            auth=self.auth,
            timeout=8,
        )
        if resposta.status_code >= 400:
            raise RuntimeError("Intelbras recusou o login.")

    def upsert_usuario(self, usuario):
        usuario_id = str(usuario["id_interno"])
        ficha = {
            "UserID": usuario_id,
            "UserName": str(usuario.get("nome") or "")[:100],
            "UserType": 0,
            "Authority": 2,
            "UserStatus": 0,
        }
        try:
            self._post("/cgi-bin/AccessUser.cgi?action=insertMulti", {"UserList": [ficha]})
        except RuntimeError:
            self._post("/cgi-bin/AccessUser.cgi?action=updateMulti", {"UserList": [ficha]})
        cartoes = []
        for tag in usuario.get("tags_rfid") or []:
            codigo = str(tag).strip()
            if codigo:
                cartoes.append(
                    {"UserID": usuario_id, "CardNo": codigo, "CardType": 0, "CardStatus": 0}
                )
        if cartoes:
            try:
                self._post(
                    "/cgi-bin/AccessCard.cgi?action=insertMulti",
                    {"CardList": cartoes},
                )
            except RuntimeError:
                self._post(
                    "/cgi-bin/AccessCard.cgi?action=updateMulti",
                    {"CardList": cartoes},
                )
        url = usuario.get("url_foto_facial") or ""
        if not url:
            return
        foto = requests.get(url, timeout=15)
        if foto.status_code >= 400 or not foto.content:
            raise RuntimeError("Não foi possível baixar a foto facial.")
        self._post(
            "/cgi-bin/AccessFace.cgi?action=insertMulti",
            {
                "FaceList": [
                    {
                        "UserID": usuario_id,
                        "PhotoData": [base64.b64encode(foto.content).decode("ascii")],
                    }
                ]
            },
        )

    def remover(self, usuario_id):
        self._post(
            "/cgi-bin/AccessUser.cgi?action=removeMulti",
            {"UserList": [{"UserID": str(usuario_id)}]},
        )


def _driver(equipamento):
    if equipamento.get("fabricante") == "intelbras":
        return IntelbrasDriver(equipamento)
    return ControlIdDriver(equipamento)


def sincronizar_equipamento(equipamento, usuarios, cache):
    enviar, remover = selecionar_delta(equipamento, usuarios, cache)
    simulacao = _forcar_simulacao() or not _porta_aberta(
        equipamento.get("ip_local"), equipamento.get("porta") or 80
    )
    if simulacao:
        return {
            "equipamento_id": equipamento.get("id"),
            "sucesso": False,
            "mensagem": (
                "Simulado: equipamento inacessível. "
                "Nenhuma alteração foi gravada na controladora."
            ),
        }
    if not enviar and not remover:
        return {
            "equipamento_id": equipamento.get("id"),
            "sucesso": True,
            "mensagem": "Nada novo para enviar.",
        }
    driver = _driver(equipamento)
    try:
        driver.login()
    except (requests.RequestException, RuntimeError, ValueError):
        return {
            "equipamento_id": equipamento.get("id"),
            "sucesso": False,
            "mensagem": "Não foi possível autenticar na controladora.",
        }
    ok = []
    falhas = 0
    removidos_ok = []
    for usuario in enviar:
        try:
            driver.upsert_usuario(usuario)
            ok.append(usuario)
        except (requests.RequestException, RuntimeError, ValueError):
            falhas += 1
    for usuario_id in remover:
        try:
            driver.remover(usuario_id)
            removidos_ok.append(usuario_id)
        except (requests.RequestException, RuntimeError, ValueError):
            falhas += 1
    gravar_hashes(cache, equipamento.get("id"), ok, removidos_ok)
    if falhas:
        return {
            "equipamento_id": equipamento.get("id"),
            "sucesso": False,
            "mensagem": f"{len(ok)} enviados, {len(removidos_ok)} removidos, {falhas} falhas.",
        }
    return {
        "equipamento_id": equipamento.get("id"),
        "sucesso": True,
        "mensagem": f"{len(ok)} enviados, {len(removidos_ok)} removidos.",
    }


def _cabecalhos():
    return {"Authorization": f"Bearer {API_TOKEN}", "Accept": "application/json"}


def consultar_payload():
    resposta = requests.get(
        f"{API_URL.rstrip('/')}/api/v1/agent/sync-payload",
        headers=_cabecalhos(),
        timeout=30,
    )
    resposta.raise_for_status()
    return resposta.json()


def reportar(resultado):
    requests.post(
        f"{API_URL.rstrip('/')}/api/v1/agent/report",
        headers={**_cabecalhos(), "Content-Type": "application/json"},
        json={
            "equipamento_id": resultado.get("equipamento_id"),
            "sucesso": bool(resultado.get("sucesso")),
            "mensagem": resultado.get("mensagem") or "",
        },
        timeout=20,
    ).raise_for_status()


def ciclo(cache):
    payload = consultar_payload()
    equipamentos = payload.get("equipamentos") or []
    usuarios = payload.get("usuarios") or []
    for equipamento in equipamentos:
        resultado = sincronizar_equipamento(equipamento, usuarios, cache)
        reportar(resultado)
        salvar_estado(cache)
        print(
            f"{equipamento.get('nome')}: {resultado.get('mensagem')}",
            flush=True,
        )


def main():
    if "__VIZINSYNC_" in API_URL or "__VIZINSYNC_" in API_TOKEN:
        print(
            "Baixe o script em Controle de Acesso ou defina VIZINSYNC_API_URL e VIZINSYNC_API_TOKEN.",
            flush=True,
        )
        return
    cache = carregar_estado()
    while True:
        try:
            ciclo(cache)
        except (requests.RequestException, ValueError, OSError) as exc:
            print(f"Falha ao consultar a API: {exc.__class__.__name__}", flush=True)
        time.sleep(max(15, INTERVALO))


if __name__ == "__main__":
    main()
