# =============================================================================
#  ChargeGrid Intelligence — Lógica de Negócio (Core)
#  Sprint 1 | FIAP + GoodWe EV Challenge 2026
# =============================================================================

"""
Módulo de lógica de negócio para o sistema ChargeGrid Intelligence.
Contém todas as regras de tarifação e cálculo de sessões de recarga,
sem dependências de I/O (interface com o usuário delegada ao frontend).
"""

import datetime
from typing import Dict, List

# ---------------------------------------------------------------------------
# Constantes de tarifação
# ---------------------------------------------------------------------------
TARIFA_BASE_KWH       = 1.20   # R$/kWh  — tarifa padrão
DESCONTO_ASSINANTE    = 0.15   # 15 % de desconto para assinantes
MULTIPLICADOR_PICO    = 1.50   # +50 % no horário de pico (18h–22h59)
TAXA_MINIMA_SESSAO    = 2.00   # R$ — cobrança mínima por sessão

POTENCIA_CARREGADOR_KW = 11.0  # kW  — modelo GoodWe GW11K-HCA-20

# Intervalos de simulação (cada "tick" representa 1 minuto real da sessão)
TICK_MINUTOS = 5  # simulamos de 5 em 5 minutos


# ---------------------------------------------------------------------------
# Funções de regra de negócio
# ---------------------------------------------------------------------------

def horario_pico(hora: int, minuto: int) -> bool:
    """
    Verifica se o horário informado está no horário de pico tarifário.

    Args:
        hora: Hora do dia (0-23)
        minuto: Minuto da hora (0-59)

    Returns:
        True se o horário está entre 18:00 e 22:59 (horário de pico)
    """
    total_minutos = hora * 60 + minuto
    return 18 * 60 <= total_minutos <= 22 * 60 + 59


def calcular_tarifa(hora_inicio: int, minuto_inicio: int, tipo_usuario: str) -> float:
    """
    Calcula a tarifa por kWh aplicável à sessão com base nas regras de negócio.

    Regras aplicadas:
      - Horário de pico (18:00–22:59): tarifa base × 1,5
      - Assinante (tipo='A'): desconto de 15% sobre o valor final

    Args:
        hora_inicio: Hora de início da sessão (0-23)
        minuto_inicio: Minuto de início da sessão (0-59)
        tipo_usuario: 'P' (Padrão) ou 'A' (Assinante)

    Returns:
        Tarifa por kWh em R$, arredondada para 4 casas decimais
    """
    tarifa = TARIFA_BASE_KWH

    if horario_pico(hora_inicio, minuto_inicio):
        tarifa *= MULTIPLICADOR_PICO

    if tipo_usuario == "A":
        tarifa *= (1 - DESCONTO_ASSINANTE)

    return round(tarifa, 4)


def simular_progresso_recarga(
    hora_inicio: int,
    minuto_inicio: int,
    duracao_minutos: int,
    potencia_kw: float,
    tipo_usuario: str
) -> tuple[List[Dict], float]:
    """
    Simula o progresso da sessão de recarga em intervalos de TICK_MINUTOS.
    Calcula a tarifa PROPORCIONAL: se a sessão passar das 18h, cobra pico apenas
    nos minutos que estiverem dentro do horário de pico.

    Args:
        hora_inicio: Hora de início da sessão
        minuto_inicio: Minuto de início da sessão
        duracao_minutos: Duração total da sessão em minutos
        potencia_kw: Potência do carregador em kW
        tipo_usuario: 'P' (Padrão) ou 'A' (Assinante)

    Returns:
        Tupla com (log_ticks, tarifa_media_ponderada)
    """
    log = []
    energia_acumulada = 0.0
    custo_acumulado = 0.0
    minuto_atual = 0

    # Converter hora inicial para minutos desde meia-noite
    minutos_desde_meia_noite = hora_inicio * 60 + minuto_inicio

    # Loop que avança de TICK_MINUTOS em TICK_MINUTOS
    for _ in range(0, duracao_minutos, TICK_MINUTOS):
        minuto_atual += TICK_MINUTOS
        if minuto_atual > duracao_minutos:
            minuto_atual = duracao_minutos

        # Calcular horário de INÍCIO deste tick (usado para tarifação)
        minutos_inicio_tick = minutos_desde_meia_noite + minuto_atual - TICK_MINUTOS
        hora_inicio_tick = (minutos_inicio_tick // 60) % 24
        min_inicio_tick = minutos_inicio_tick % 60

        # Calcular horário de FIM deste tick (usado para exibição)
        minutos_fim_tick = minutos_desde_meia_noite + minuto_atual
        hora_fim_tick = (minutos_fim_tick // 60) % 24
        min_fim_tick = minutos_fim_tick % 60

        # Calcular tarifa deste tick (baseado no início do intervalo)
        tarifa_tick = calcular_tarifa(hora_inicio_tick, min_inicio_tick, tipo_usuario)

        # Energia do tick
        energia_tick = round(potencia_kw * (TICK_MINUTOS / 60), 4)
        energia_acumulada = round(energia_acumulada + energia_tick, 4)

        # Custo do tick
        custo_tick = round(energia_tick * tarifa_tick, 4)
        custo_acumulado = round(custo_acumulado + custo_tick, 4)

        # Verificar se está no horário de pico (baseado no início do intervalo)
        em_pico = horario_pico(hora_inicio_tick, min_inicio_tick)

        log.append({
            "minuto": minuto_atual,
            "hora_tick": f"{hora_fim_tick:02d}:{min_fim_tick:02d}",  # Hora FINAL do intervalo
            "energia_tick_kwh": energia_tick,
            "energia_total_kwh": energia_acumulada,
            "tarifa_tick": tarifa_tick,
            "custo_tick": custo_tick,
            "custo_total": custo_acumulado,
            "em_pico": em_pico
        })

    # Calcular tarifa média ponderada
    if energia_acumulada > 0:
        tarifa_media = round(custo_acumulado / energia_acumulada, 4)
    else:
        tarifa_media = TARIFA_BASE_KWH

    return log, tarifa_media


def verificar_sessao_cruza_pico(hora_inicio: int, minuto_inicio: int,
                                duracao_min: int) -> bool:
    """
    Verifica se a sessão passa (total ou parcialmente) pelo horário de pico.

    Três situações resultam em True:
      1. A sessão já começa dentro do pico (18:00–22:59).
      2. A sessão começa antes das 18h mas termina às 18h ou depois.
      3. A sessão atravessa a meia-noite (e pode atingir o pico do dia seguinte).

    Args:
        hora_inicio:   Hora de início da sessão (0-23)
        minuto_inicio: Minuto de início da sessão (0-59)
        duracao_min:   Duração da sessão em minutos

    Returns:
        True se a sessão toca o horário de pico em algum momento.
    """
    minutos_inicio = hora_inicio * 60 + minuto_inicio
    minutos_fim    = minutos_inicio + duracao_min
    hora_fim       = (minutos_fim // 60) % 24

    # 1. Já começa no pico
    if horario_pico(hora_inicio, minuto_inicio):
        return True

    # 2. Termina às 18h ou depois (no mesmo dia)
    if hora_fim >= 18:
        return True

    # 3. Atravessa a meia-noite
    if minutos_fim >= 24 * 60:
        return True

    return False


def processar_sessao(
    nome_usuario: str,
    tipo_usuario: str,
    hora_inicio: int,
    minuto_inicio: int,
    duracao_min: int,
    carregador_id: str = "C1"
) -> Dict:
    """
    Função principal de orquestração: recebe os parâmetros da sessão,
    executa a simulação e retorna o dicionário completo com todos os dados.

    Args:
        nome_usuario: Nome do usuário (string livre)
        tipo_usuario: 'P' (Padrão) ou 'A' (Assinante)
        hora_inicio: Hora de início (0-23)
        minuto_inicio: Minuto de início (0-59)
        duracao_min: Duração desejada em minutos (5-240)
        carregador_id: ID do carregador selecionado

    Returns:
        Dicionário com todos os dados da sessão
    """
    # Garante que duracao_min seja múltiplo de TICK_MINUTOS
    duracao_ajustada = max(TICK_MINUTOS,
                           (duracao_min // TICK_MINUTOS) * TICK_MINUTOS)

    # Executa a simulação com cálculo proporcional de tarifa
    log, tarifa_media = simular_progresso_recarga(
        hora_inicio,
        minuto_inicio,
        duracao_ajustada,
        POTENCIA_CARREGADOR_KW,
        tipo_usuario
    )

    # Energia real consumida (do último tick)
    energia_real = log[-1]["energia_total_kwh"] if log else 0.0

    # Custo já calculado durante a simulação (último tick)
    custo_bruto = log[-1]["custo_total"] if log else 0.0

    # Aplica taxa mínima
    custo_final = max(custo_bruto, TAXA_MINIMA_SESSAO)
    taxa_minima_aplicada = custo_bruto < TAXA_MINIMA_SESSAO

    # Verifica se a sessão cruza o horário de pico
    cruza_pico = verificar_sessao_cruza_pico(hora_inicio, minuto_inicio, duracao_ajustada)

    # Calcula minutos efetivamente em horário de pico
    minutos_em_pico = sum(TICK_MINUTOS for tick in log if tick["em_pico"])

    # Calcula percentual em pico (para mostrar no relatório)
    percentual_em_pico = (minutos_em_pico / duracao_ajustada * 100) if duracao_ajustada > 0 else 0

    # Gera ID único da sessão
    id_sessao = f"CGI-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"

    # Calcular hora de término
    minutos_fim = hora_inicio * 60 + minuto_inicio + duracao_ajustada
    hora_fim = (minutos_fim // 60) % 24
    minuto_fim = minutos_fim % 60

    # Monta o dicionário de resposta
    sessao = {
        "id_sessao":            id_sessao,
        "carregador_id":        carregador_id,
        "nome_usuario":         nome_usuario if nome_usuario.strip() else "Usuário Anônimo",
        "tipo_usuario":         tipo_usuario,
        "hora_inicio_h":        hora_inicio,
        "hora_inicio_m":        minuto_inicio,
        "hora_inicio_str":      f"{hora_inicio:02d}:{minuto_inicio:02d}",
        "hora_fim_str":         f"{hora_fim:02d}:{minuto_fim:02d}",
        "duracao_min":          duracao_ajustada,
        "energia_kwh":          energia_real,
        "tarifa_kwh":           tarifa_media,  # Tarifa média ponderada
        "custo_bruto":          custo_bruto,
        "custo_final":          custo_final,
        "taxa_minima_aplicada": taxa_minima_aplicada,
        "horario_pico_inicio":  horario_pico(hora_inicio, minuto_inicio),
        "sessao_cruza_pico":    cruza_pico,
        "minutos_em_pico":      minutos_em_pico,
        "percentual_em_pico":   round(percentual_em_pico, 1),
        "log_ticks":            log,
        "timestamp":            datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S'),
        # Constantes úteis para o template
        "desconto_assinante_pct": int(DESCONTO_ASSINANTE * 100),
        "taxa_minima":          TAXA_MINIMA_SESSAO,
        "potencia_carregador":  POTENCIA_CARREGADOR_KW,
    }

    return sessao
