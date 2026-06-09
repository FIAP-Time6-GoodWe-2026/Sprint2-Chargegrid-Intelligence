# =============================================================================
#  ChargeGrid Intelligence — Motor de Tarifação Dinâmica
#  Sprint 2 | FIAP + GoodWe EV Challenge 2026
# =============================================================================

"""
PricingEngine: tarifação dinâmica com 3 eixos independentes e combináveis.

Eixo 1 — Horário (herdado do Sprint 1):
    Tarifa base multiplicada por 1.5× no horário de pico (18h–22h59).
    Lógica intacta de logica_recarga.py — sem reescrita.

Eixo 2 — Demanda (novo no Sprint 2):
    Quando a ocupação da rede supera o limiar configurável (padrão 70%),
    aplica um multiplicador adicional proporcional à ocupação.
    Reproduz o comportamento comercial de "preço dinâmico por escassez".

Eixo 3 — Tipo de usuário (expandido no Sprint 2):
    Padrão    (P) — sem desconto
    Assinante (A) — 15% de desconto (mantido do Sprint 1)
    Corporativo(C)— 10% de desconto (novo tipo)

Fórmula final:
    tarifa = TARIFA_BASE
           × multiplicador_pico       (Eixo 1, se aplicável)
           × multiplicador_demanda    (Eixo 2, se aplicável)
           × (1 − desconto_usuario)   (Eixo 3)

    custo_tick = energia_tick × tarifa
    custo_final = max(custo_acumulado, TAXA_MINIMA_SESSAO)

Sprint 4+:
    Os multiplicadores poderão ser ajustados em tempo real por um modelo
    de ML treinado em séries temporais de consumo (previsão de demanda).
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Optional

from models import UserType

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constantes de tarifação (compatíveis com Sprint 1)
# ---------------------------------------------------------------------------

TARIFA_BASE_KWH:    float = 1.20   # R$/kWh — tarifa padrão
DESCONTO_ASSINANTE: float = 0.15   # 15% — UserType.SUBSCRIBER
DESCONTO_CORPORATE: float = 0.10   # 10% — UserType.CORPORATE
MULTIPLICADOR_PICO: float = 1.50   # +50% entre 18h e 22h59
TAXA_MINIMA_SESSAO: float = 2.00   # R$ — cobrança mínima por sessão

# Eixo 2 — demanda
LIMIAR_DEMANDA:        float = 0.70   # ocupação acima da qual a tarifa sobe
MULTIPLICADOR_DEMANDA: float = 1.30   # +30% quando rede ≥ 70% de ocupação


# ---------------------------------------------------------------------------
# Resultado detalhado de tarifação
# ---------------------------------------------------------------------------

@dataclass
class TariffResult:
    """
    Encapsula todos os componentes da tarifa calculada.

    Campos
    ------
    tariff_kwh          : tarifa final aplicada (R$/kWh)
    base_kwh            : tarifa base antes dos multiplicadores
    peak_applied        : True se o multiplicador de pico foi aplicado
    demand_applied      : True se o multiplicador de demanda foi aplicado
    user_discount_pct   : percentual de desconto aplicado ao tipo de usuário
    occupancy_pct       : ocupação da rede no momento do cálculo (%)
    hora                : hora do cálculo (para auditoria)
    breakdown           : descrição textual de cada componente aplicado
    """
    tariff_kwh:        float
    base_kwh:          float
    peak_applied:      bool
    demand_applied:    bool
    user_discount_pct: float
    occupancy_pct:     float
    hora:              int
    breakdown:         str


# ---------------------------------------------------------------------------
# PricingEngine
# ---------------------------------------------------------------------------

class PricingEngine:
    """
    Calcula a tarifa dinâmica por sessão de recarga.

    Parâmetros
    ----------
    session_manager : SessionManager
        Injetado — usado para consultar a ocupação atual da rede (Eixo 2).
    tarifa_base     : float, opcional
        Sobrescreve TARIFA_BASE_KWH (útil em testes e configuração regional).
    limiar_demanda  : float, opcional
        Percentual de ocupação [0,1] que ativa o multiplicador de demanda.
    """

    def __init__(
        self,
        session_manager,                      # SessionManager (sem import circular)
        tarifa_base:    float = TARIFA_BASE_KWH,
        limiar_demanda: float = LIMIAR_DEMANDA,
    ) -> None:
        self._sm            = session_manager
        self._tarifa_base   = tarifa_base
        self._limiar_demanda = limiar_demanda

    # ------------------------------------------------------------------
    # API principal
    # ------------------------------------------------------------------

    def calculate(
        self,
        user_type:           UserType,
        hora:                Optional[int] = None,
        minuto:              int = 0,
        occupancy_override:  Optional[float] = None,
    ) -> TariffResult:
        """
        Calcula a tarifa por kWh aplicável a uma sessão.

        Decisão de design — tarifa fixada no momento da conexão:
            A tarifa é calculada uma única vez ao iniciar a sessão e
            permanece constante durante toda a recarga. Esta é a prática
            comercial padrão (equivalente a uma tarifa contratada): o
            usuário sabe exatamente quanto pagará por kWh ao conectar.
            Como consequência, o eixo de demanda (Eixo 2) reflete a
            ocupação da rede no instante da conexão — sessões que já
            estavam carregando quando a rede ficou cheia não sofrem
            reajuste retroativo. Isso é intencional e correto do ponto
            de vista do faturamento. No Sprint 4, o modelo de ML poderá
            sugerir tarifas preditivas com base em séries temporais de
            ocupação.

        Eixo 2 — escopo da ocupação (#B28):
            Por padrão usa ``self._sm.occupancy_ratio()`` (ocupação global).
            Quando ``occupancy_override`` é fornecido, usa esse valor — o app
            passa a ocupação do POSTO da sessão, alinhando a tarifa de escassez
            ao controle de potência, que também é por posto. Sem isso, um posto
            lotado e em throttle cobraria tarifa normal enquanto os outros
            estivessem vazios.

        Args:
            user_type          : categoria do usuário (UserType)
            hora               : hora de início (0-23); usa hora atual se None
            minuto             : minuto de início (0-59)
            occupancy_override : ocupação [0,1] a usar no Eixo 2; None = global

        Returns:
            TariffResult com tarifa final e breakdown completo
        """
        if hora is None:
            hora = datetime.datetime.now().hour
            minuto = datetime.datetime.now().minute

        tarifa = self._tarifa_base
        componentes = [f"Base: R$ {tarifa:.4f}/kWh"]

        # ── Eixo 1 — Horário ────────────────────────────────────────────
        em_pico = self._em_pico(hora, minuto)
        if em_pico:
            tarifa = round(tarifa * MULTIPLICADOR_PICO, 4)
            componentes.append(
                f"Pico (18h–22h59): ×{MULTIPLICADOR_PICO} → R$ {tarifa:.4f}/kWh"
            )

        # ── Eixo 2 — Demanda ─────────────────────────────────────────────
        # #B28 — usa a ocupação do posto quando fornecida; caso contrário, global.
        ocupacao = (occupancy_override if occupancy_override is not None
                    else self._sm.occupancy_ratio())
        em_demanda = ocupacao >= self._limiar_demanda
        if em_demanda:
            tarifa = round(tarifa * MULTIPLICADOR_DEMANDA, 4)
            componentes.append(
                f"Alta demanda ({ocupacao:.0%} ocupação): "
                f"×{MULTIPLICADOR_DEMANDA} → R$ {tarifa:.4f}/kWh"
            )

        # ── Eixo 3 — Tipo de usuário ──────────────────────────────────────
        desconto = self._desconto_usuario(user_type)
        if desconto > 0:
            tarifa = round(tarifa * (1 - desconto), 4)
            componentes.append(
                f"Desconto {user_type.name} ({desconto:.0%}): "
                f"→ R$ {tarifa:.4f}/kWh"
            )

        breakdown = " | ".join(componentes)

        result = TariffResult(
            tariff_kwh=tarifa,
            base_kwh=self._tarifa_base,
            peak_applied=em_pico,
            demand_applied=em_demanda,
            user_discount_pct=desconto * 100,
            occupancy_pct=round(ocupacao * 100, 1),
            hora=hora,
            breakdown=breakdown,
        )

        logger.debug(
            "Tarifa calculada: R$ %.4f/kWh | %s | Ocupação: %.1f%%",
            tarifa, user_type.name, ocupacao * 100,
        )
        return result

    def estimate_cost(
        self,
        user_type: UserType,
        energy_kwh: float,
        hora: Optional[int] = None,
        minuto: int = 0,
    ) -> float:
        """
        Estima o custo total de uma sessão dado consumo e perfil do usuário.

        Args:
            user_type  : categoria do usuário
            energy_kwh : energia estimada a consumir
            hora       : hora de início (usa atual se None)
            minuto     : minuto de início

        Returns:
            Custo estimado em reais, nunca abaixo de TAXA_MINIMA_SESSAO
        """
        result = self.calculate(user_type, hora, minuto)
        custo = round(energy_kwh * result.tariff_kwh, 2)
        return max(custo, TAXA_MINIMA_SESSAO)

    def tariff_label(self, result: TariffResult) -> str:
        """
        Gera um rótulo legível para exibição no menu e relatório.

        Exemplo:
            "R$ 1.8360/kWh  [PICO + DEMANDA | Assinante -15%]"
        """
        flags = []
        if result.peak_applied:
            flags.append("PICO")
        if result.demand_applied:
            flags.append(f"DEMANDA ({result.occupancy_pct:.0f}%)")
        if result.user_discount_pct > 0:
            flags.append(f"Desconto -{result.user_discount_pct:.0f}%")

        flag_str = " + ".join(flags) if flags else "Tarifa padrão"
        return f"R$ {result.tariff_kwh:.4f}/kWh  [{flag_str}]"

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    @staticmethod
    def _em_pico(hora: int, minuto: int) -> bool:
        """
        Verifica horário de pico tarifário (18:00–22:59).
        Lógica idêntica à de logica_recarga.py do Sprint 1.
        """
        total = hora * 60 + minuto
        return 18 * 60 <= total <= 22 * 60 + 59

    @staticmethod
    def _desconto_usuario(user_type: UserType) -> float:
        """Retorna o percentual de desconto [0,1] para o tipo de usuário."""
        descontos = {
            UserType.STANDARD:   0.0,
            UserType.SUBSCRIBER: DESCONTO_ASSINANTE,
            UserType.CORPORATE:  DESCONTO_CORPORATE,
        }
        return descontos.get(user_type, 0.0)

    # ------------------------------------------------------------------
    # Tabela de tarifas (para impressão no menu)
    # ------------------------------------------------------------------

    def print_tariff_table(self) -> str:
        """
        Gera uma tabela legível com todas as combinações de tarifa possíveis.
        Útil para o menu interativo e para o documento técnico.
        """
        ocupacao_atual = self._sm.occupancy_ratio()
        linhas = [
            "╔══════════════════════════════════════════════════════════╗",
            "║           TABELA DE TARIFAS — ChargeGrid Intelligence    ║",
            f"║  Tarifa base: R$ {self._tarifa_base:.2f}/kWh  |  "
            f"Ocupação atual: {ocupacao_atual:.0%}          ║",
            "╠══════════════╦══════════════╦══════════════╦═════════════╣",
            "║ Tipo usuário ║ Fora de pico ║  Horário pico║ Pico+Demanda║",
            "╠══════════════╬══════════════╬══════════════╬═════════════╣",
        ]
        for ut in UserType:
            # Simula ocupação baixa para coluna "fora de pico"
            class _FakeSM:
                def occupancy_ratio(self): return 0.0
            pe_low = PricingEngine(_FakeSM(), self._tarifa_base, self._limiar_demanda)
            t_normal = pe_low.calculate(ut, hora=10)

            t_pico   = pe_low.calculate(ut, hora=19)

            class _HighSM:
                def occupancy_ratio(self): return 0.80
            pe_high = PricingEngine(_HighSM(), self._tarifa_base, self._limiar_demanda)
            t_pd = pe_high.calculate(ut, hora=19)

            linhas.append(
                f"║ {ut.name:<12} ║ R$ {t_normal.tariff_kwh:.4f}   ║"
                f" R$ {t_pico.tariff_kwh:.4f}   ║ R$ {t_pd.tariff_kwh:.4f}  ║"
            )

        linhas.append(
            "╚══════════════╩══════════════╩══════════════╩═════════════╝"
        )
        return "\n".join(linhas)
