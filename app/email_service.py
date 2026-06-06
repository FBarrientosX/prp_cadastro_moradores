import os
import smtplib
from email.mime.text import MIMEText


SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT_SSL = 465


def _enviar_email(email_destino, assunto, corpo):
    username = os.environ.get("MAIL_USERNAME")
    password = os.environ.get("MAIL_PASSWORD")

    if not email_destino:
        raise ValueError("E-mail de destino não informado.")
    if not username or not password:
        raise RuntimeError("Credenciais de e-mail não configuradas.")

    mensagem = MIMEText(corpo, "plain", "utf-8")
    mensagem["Subject"] = assunto
    mensagem["From"] = username
    mensagem["To"] = email_destino

    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT_SSL, timeout=15) as servidor:
        servidor.login(username, password)
        servidor.sendmail(username, [email_destino], mensagem.as_string())


def enviar_email_reprovacao(email_destino, bloco, apartamento, nome_morador, motivo):
    assunto = (
        f"Atualização do cadastro de moradores - Bloco {bloco}, Apartamento {apartamento}"
    )
    corpo = (
        f"O cadastro do morador {nome_morador} foi reprovado e removido pelo síndico "
        f"responsável. Motivo informado: {motivo}.\n"
        "Por favor, procure o síndico do seu bloco para maiores orientações e "
        "esclarecimentos antes de tentar cadastrar esta pessoa novamente."
    )
    _enviar_email(email_destino, assunto, corpo)


def enviar_email_validacao_sucesso(email_destino, bloco, apartamento):
    assunto = "PRP Condomínio - Cadastro Validado com Sucesso"
    corpo = (
        f"Sua unidade (Bloco {bloco}, Apartamento {apartamento}) foi verificada pelo "
        "síndico e enviada para registro.\n"
        "Os moradores cadastrados nesta etapa foram aprovados com sucesso."
    )
    _enviar_email(email_destino, assunto, corpo)


def enviar_email_validacao_parcial(email_destino, moradores_recusados):
    assunto = "PRP Condomínio - Validação com Pendências"
    lista_recusados = "\n".join(
        f"- {item['nome']} - Motivo: {item['motivo']}" for item in moradores_recusados
    )
    corpo = (
        "Atenção: A validação da sua unidade foi concluída, mas o(s) seguinte(s) "
        "morador(es) foi(ram) recusado(s) pelo síndico:\n"
        f"{lista_recusados}\n"
        "Os demais moradores foram aprovados. Por favor, os moradores ativos devem "
        "procurar o síndico do bloco para orientações."
    )
    _enviar_email(email_destino, assunto, corpo)


def enviar_email_nova_reserva(email_destino, nome_espaco, bloco, apartamento, data_reserva):
    assunto = "PRP Condomínio - Nova Solicitação de Reserva"
    corpo = (
        "Uma nova solicitação de reserva foi registrada no sistema.\n\n"
        f"Espaço: {nome_espaco}\n"
        f"Unidade solicitante: Bloco {bloco}, Apto {apartamento}\n"
        f"Data desejada: {data_reserva}\n\n"
        "Acesse o módulo de reservas para aprovar ou recusar a solicitação."
    )
    _enviar_email(email_destino, assunto, corpo)


def enviar_email_resposta_reserva(email_destino, nome_espaco, data_reserva, status):
    assunto = "PRP Condomínio - Atualização da sua Reserva"
    corpo = (
        "Sua solicitação de reserva foi atualizada.\n\n"
        f"Espaço: {nome_espaco}\n"
        f"Data solicitada: {data_reserva}\n"
        f"Status final: {status}\n\n"
        "Em caso de dúvidas, procure a administração do condomínio."
    )
    _enviar_email(email_destino, assunto, corpo)
