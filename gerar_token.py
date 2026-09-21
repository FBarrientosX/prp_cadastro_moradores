from app.drive_api import obter_credenciais

if __name__ == "__main__":
    print("Iniciando fluxo de autenticação do Google Drive...")
    obter_credenciais()
    print("Sucesso! O arquivo token.json foi gerado na raiz do projeto.")