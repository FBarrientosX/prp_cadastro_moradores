"""Utilitários de validação e estrutura do condomínio."""

BLOCOS_ANDARES = {
    "1": 7,
    "2": 7,
    "3": 8,
    "4": 8,
    "5": 8,
    "6": 8,
    "7": 8,
    "8": 8,
}

APARTAMENTOS_POR_ANDAR = 8


def _apartamentos_do_andar(andar):
    return [str(andar * 100 + apt) for apt in range(1, APARTAMENTOS_POR_ANDAR + 1)]


def get_apartamentos_bloco(bloco):
    bloco = str(bloco).strip()
    num_andares = BLOCOS_ANDARES.get(bloco)
    if not num_andares:
        return []

    apartamentos = []
    for andar in range(1, num_andares + 1):
        apartamentos.extend(_apartamentos_do_andar(andar))
    return apartamentos


def get_condominio_estrutura():
    """Estrutura completa bloco -> andar -> apartamentos para uso no frontend."""
    estrutura = {}
    for bloco, num_andares in BLOCOS_ANDARES.items():
        estrutura[bloco] = {
            str(andar): _apartamentos_do_andar(andar)
            for andar in range(1, num_andares + 1)
        }
    return estrutura


def get_blocos():
    return list(BLOCOS_ANDARES.keys())


def normalizar_bloco_codigo(bloco):
    """Converte 'Bloco 1' ou '1' para o código numérico usado nas unidades."""
    bloco = str(bloco).strip()
    if bloco.lower().startswith("bloco "):
        return bloco.split(" ", 1)[1].strip()
    return bloco


def blocos_equivalentes(bloco_a, bloco_b):
    return normalizar_bloco_codigo(bloco_a) == normalizar_bloco_codigo(bloco_b)


def normalizar_bloco_apartamento(bloco, apartamento):
    return normalizar_bloco_codigo(bloco), str(apartamento).strip()


def validar_unidade(bloco, apartamento):
    bloco, apartamento = normalizar_bloco_apartamento(bloco, apartamento)
    if bloco not in BLOCOS_ANDARES:
        return False
    return apartamento in get_apartamentos_bloco(bloco)
