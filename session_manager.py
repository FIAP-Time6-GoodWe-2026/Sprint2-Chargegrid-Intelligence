# =============================================================================
#  ChargeGrid Intelligence — Gerenciador de Sessões
#  Sprint 2 | FIAP + GoodWe EV Challenge 2026
# =============================================================================

"""
SessionManager: responsável pelo ciclo de vida completo das sessões.

Responsabilidades:
    - Criar, consultar, atualizar e encerrar sessões de recarga
    - Manter o estado em memória (dict indexado por session_id)
    - Expor métricas agregadas usadas pelo PowerManager e PricingEngine
    - NÃO decide potência nem calcula tarifas (delegado a outros módulos)

Arquitetura:
    O SessionManager é o hub central. PowerManager e PricingEngine recebem
    uma referência a ele via injeção de dependência no main.py, garantindo
    que a lógica fique separada sem acoplamento direto entre os módulos.

Sprint 3+:
    O dict em memória será substituído por um repositório com ORM (SQLAlchemy).
    A interface pública (create, get, update_*, finish, list_*) permanece
    inalterada — o main.py não precisará ser modificado.
"""

import datetime
import logging
from typing import Dict, List, Optional

from models import ChargingSession, SessionStatus, UserType

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

MAX_CHARGERS_PER_STATION: int = 5   # máximo de conectores por posto
POTENCIA_NOMINAL_KW: float   = 11.0  # GW11K-HCA-20


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------

class SessionManager:
    """
    Gerenciador de sessões de recarga simultâneas.

    Suporta múltiplos postos (stations) e múltiplos carregadores por posto.
    O estado é mantido em dois dicionários:
        _sessions  : {session_id → ChargingSession}  — todas as sessões
        _chargers  : {charger_id → session_id | None} — ocupação dos conectores
    """

    def __init__(self) -> None:
        self._sessions: Dict[str, ChargingSession] = {}
        # Carregadores pré-cadastrados: 5 por posto, 3 postos = 15 total
        self._chargers: Dict[str, Optional[str]] = {
            f"P{p}-C{c}": None
            for p in range(1, 4)
            for c in range(1, MAX_CHARGERS_PER_STATION + 1)
        }
        logger.info("SessionManager inicializado com %d carregadores.", len(self._chargers))

    # ------------------------------------------------------------------
    # Consulta de carregadores
    # ------------------------------------------------------------------

    def list_chargers(self) -> Dict[str, Optional[str]]:
        """Retorna o dicionário completo de carregadores e suas sessões."""
        return dict(self._chargers)

    def available_chargers(self) -> List[str]:
        """Lista os IDs de carregadores sem sessão ativa."""
        return [cid for cid, sid in self._chargers.items() if sid is None]

    def is_charger_available(self, charger_id: str) -> bool:
        """Verifica se um carregador específico está livre."""
        if charger_id not in self._chargers:
            raise ValueError(f"Carregador '{charger_id}' não existe no sistema.")
        return self._chargers[charger_id] is None

    # ------------------------------------------------------------------
    # Criação de sessão
    # ------------------------------------------------------------------

    def create_session(
        self,
        charger_id: str,
        vehicle_id: str,
        user_name: str,
        user_type: UserType,
        requested_power_kw: float = POTENCIA_NOMINAL_KW,
    ) -> ChargingSession:
        """
        Inicia uma nova sessão de recarga em um carregador disponível.

        Args:
            charger_id          : ID do carregador (ex.: "P1-C3")
            vehicle_id          : placa ou identificador do veículo
            user_name           : nome do motorista
            user_type           : categoria tarifária (UserType)
            requested_power_kw  : potência solicitada (padrão: nominal do HCA)

        Returns:
            ChargingSession recém-criada (status = PREPARING)

        Raises:
            ValueError  : carregador inválido ou ocupado
            ValueError  : potência solicitada fora do intervalo permitido
        """
        if charger_id not in self._chargers:
            raise ValueError(f"Carregador '{charger_id}' não cadastrado.")

        if self._chargers[charger_id] is not None:
            raise ValueError(
                f"Carregador '{charger_id}' está ocupado "
                f"(sessão: {self._chargers[charger_id]})."
            )

        # Valida faixa de potência (mínima do HCA trifásico: 4.2 kW)
        if not (4.2 <= requested_power_kw <= POTENCIA_NOMINAL_KW):
            raise ValueError(
                f"Potência solicitada {requested_power_kw} kW fora do intervalo "
                f"permitido [4.2, {POTENCIA_NOMINAL_KW}] kW."
            )

        # Extrai station_id do charger_id (formato "P1-C3" → "P1")
        station_id = charger_id.split("-")[0]

        session = ChargingSession(
            charger_id=charger_id,
            station_id=station_id,
            vehicle_id=vehicle_id,
            user_name=user_name,
            user_type=user_type,
            requested_power_kw=requested_power_kw,
            status=SessionStatus.PREPARING,
        )

        self._sessions[session.session_id] = session
        self._chargers[charger_id] = session.session_id

        logger.info(
            "Sessão criada: %s | Carregador: %s | Veículo: %s | Usuário: %s (%s)",
            session.session_id, charger_id, vehicle_id, user_name, user_type.value,
        )
        return session

    # ------------------------------------------------------------------
    # Atualização de sessão
    # ------------------------------------------------------------------

    def start_charging(self, session_id: str, allocated_power_kw: float,
                       tariff_kwh: float) -> ChargingSession:
        """
        Transita a sessão de PREPARING → CHARGING após alocação de potência.

        Args:
            session_id          : ID da sessão
            allocated_power_kw  : potência concedida pelo PowerManager
            tariff_kwh          : tarifa calculada pela PricingEngine

        Returns:
            Sessão atualizada
        """
        session = self._get_or_raise(session_id)
        session.allocated_power_kw = allocated_power_kw
        session.tariff_kwh = tariff_kwh
        session.status = SessionStatus.CHARGING
        # Inicia o relógio de energia: a partir daqui a energia é acumulada
        # com base no tempo real decorrido (ver accrue_energy).
        session.last_energy_update = datetime.datetime.now()
        logger.info(
            "Carregamento iniciado: %s | %.1f kW | R$ %.4f/kWh",
            session_id, allocated_power_kw, tariff_kwh,
        )
        return session

    def accrue_energy(self, session_id: str) -> Optional[ChargingSession]:
        """
        Acumula energia com base no TEMPO REAL decorrido desde a última
        contabilização, à potência atualmente alocada.

        Esta é a forma correta de acumular energia na camada web: é
        idempotente em relação ao número de chamadas. Chamar duas vezes
        em sequência rápida acrescenta quase nada (pouco tempo decorreu);
        múltiplas abas ou refreshes convergem para o mesmo valor real,
        pois cada chamada contabiliza apenas o intervalo desde a anterior.

        Energia (kWh) = potência (kW) × Δt (horas)

        Args:
            session_id : ID da sessão

        Returns:
            Sessão atualizada (inalterada se inativa ou sem relógio iniciado)
        """
        session = self._sessions.get(session_id)
        if session is None or not session.is_active:
            return session  # silencioso: nada a acumular

        agora = datetime.datetime.now()
        referencia = session.last_energy_update or session.start_time
        delta_horas = (agora - referencia).total_seconds() / 3600.0

        if delta_horas <= 0:
            return session  # relógio sem avanço (chamadas concorrentes)

        delta_kwh = session.allocated_power_kw * delta_horas
        session.energy_kwh = round(session.energy_kwh + delta_kwh, 4)
        session.total_cost_brl = round(
            session.energy_kwh * session.tariff_kwh, 2
        )
        session.last_energy_update = agora
        return session

    def update_energy(self, session_id: str, delta_kwh: float) -> ChargingSession:
        """
        Adiciona uma quantidade FIXA de energia à sessão e recalcula o custo.

        Uso destinado a simulações determinísticas (demo do main.py e testes),
        onde se quer controlar exatamente quanta energia foi consumida sem
        depender do relógio. A camada web usa accrue_energy (tempo real).

        Args:
            session_id : ID da sessão
            delta_kwh  : energia a adicionar neste intervalo (kWh)

        Returns:
            Sessão atualizada
        """
        session = self._get_or_raise(session_id)
        if not session.is_active:
            raise RuntimeError(
                f"Tentativa de atualizar energia em sessão inativa: {session_id}"
            )
        session.energy_kwh = round(session.energy_kwh + delta_kwh, 4)
        session.total_cost_brl = round(
            session.energy_kwh * session.tariff_kwh, 2
        )
        return session

    def throttle_session(self, session_id: str,
                         new_power_kw: float) -> ChargingSession:
        """
        Reduz a potência de uma sessão ativa (controle dinâmico de demanda).

        Transita o status para THROTTLED para sinalizar ao Modbus
        que o registrador 10029 foi reescrito.
        """
        session = self._get_or_raise(session_id)
        # Contabiliza a energia consumida à potência ATUAL antes de alterá-la,
        # para que o intervalo até agora não seja cobrado à nova potência.
        self.accrue_energy(session_id)
        old_power = session.allocated_power_kw
        session.allocated_power_kw = round(new_power_kw, 2)
        session.status = SessionStatus.THROTTLED
        logger.warning(
            "Throttle aplicado: %s | %.1f kW → %.1f kW",
            session_id, old_power, new_power_kw,
        )
        return session

    def restore_session(self, session_id: str,
                        power_kw: float) -> ChargingSession:
        """
        Restaura potência de uma sessão THROTTLED após liberação de capacidade.

        O status só transita para CHARGING quando a potência concedida atinge
        (com margem de 0.1 kW) a potência solicitada originalmente.
        Se a restauração for parcial — capacidade disponível permite aumentar
        mas não atingir o valor original — a sessão permanece THROTTLED com
        a nova potência mais alta. Isso evita o bug em que sessões exibem
        CHARGING mesmo recebendo menos do que pediram.
        """
        session = self._get_or_raise(session_id)
        # Contabiliza energia à potência reduzida antes de mudar a alocação.
        self.accrue_energy(session_id)
        session.allocated_power_kw = round(power_kw, 2)
        # Transita para CHARGING apenas se a potência foi totalmente restaurada.
        if session.allocated_power_kw >= session.requested_power_kw - 0.1:
            session.status = SessionStatus.CHARGING
            logger.info(
                "Sessão restaurada (CHARGING): %s | %.1f kW", session_id, power_kw
            )
        else:
            session.status = SessionStatus.THROTTLED
            logger.info(
                "Sessão parcialmente restaurada (THROTTLED): %s | %.1f kW "
                "(solicitado: %.1f kW)",
                session_id, power_kw, session.requested_power_kw,
            )
        return session

    # ------------------------------------------------------------------
    # Encerramento
    # ------------------------------------------------------------------

    def finish_session(self, session_id: str,
                       status: SessionStatus = SessionStatus.FINISHED
                       ) -> ChargingSession:
        """
        Encerra uma sessão, libera o carregador e aplica taxa mínima se necessário.

        Idempotente: chamar duas vezes na mesma sessão retorna o objeto
        inalterado sem reprocessar taxa mínima nem tentar liberar o
        carregador novamente.

        Args:
            session_id : ID da sessão
            status     : FINISHED (padrão) ou FAULTED

        Returns:
            Sessão encerrada
        """
        from pricing_engine import TAXA_MINIMA_SESSAO  # importação local evita circular

        session = self._get_or_raise(session_id)

        # Guarda de idempotência: se já foi encerrada, retorna sem reprocessar
        if session.status in (SessionStatus.FINISHED, SessionStatus.FAULTED):
            logger.debug(
                "finish_session chamado em sessão já encerrada: %s (%s) — ignorado.",
                session_id, session.status.value,
            )
            return session

        # Contabiliza energia até o momento do encerramento (tempo real)
        self.accrue_energy(session_id)

        session.end_time = datetime.datetime.now()
        session.status = status

        # Aplica taxa mínima (herdada do Sprint 1)
        if session.total_cost_brl < TAXA_MINIMA_SESSAO:
            session.total_cost_brl = TAXA_MINIMA_SESSAO

        # Libera o carregador
        self._chargers[session.charger_id] = None

        logger.info(
            "Sessão encerrada: %s | Status: %s | Energia: %.3f kWh | Custo: R$ %.2f",
            session_id, status.value, session.energy_kwh, session.total_cost_brl,
        )
        return session

    # ------------------------------------------------------------------
    # Consultas agregadas
    # ------------------------------------------------------------------

    def get_session(self, session_id: str) -> Optional[ChargingSession]:
        """Retorna a sessão ou None se não encontrada."""
        return self._sessions.get(session_id)

    def list_active(self) -> List[ChargingSession]:
        """Lista todas as sessões em andamento (CHARGING ou THROTTLED)."""
        return [s for s in self._sessions.values() if s.is_active]

    def list_all(self) -> List[ChargingSession]:
        """Lista todas as sessões (ativas e encerradas)."""
        return list(self._sessions.values())

    def total_allocated_power_kw(self) -> float:
        """Soma da potência alocada em todas as sessões ativas."""
        return round(sum(s.allocated_power_kw for s in self.list_active()), 2)

    def active_count(self) -> int:
        """Número de sessões atualmente ativas."""
        return len(self.list_active())

    def occupancy_ratio(self) -> float:
        """
        Taxa de ocupação da rede [0.0, 1.0].
        Usada pela PricingEngine para o eixo de tarifação por demanda.

        Deriva do total real de conectores cadastrados no sistema em vez
        de um valor hardcoded — se MAX_CHARGERS_PER_STATION ou o range
        de postos mudar, a ocupação continua correta automaticamente.
        """
        total = len(self._chargers)
        return self.active_count() / max(total, 1)

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    def _get_or_raise(self, session_id: str) -> ChargingSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError(f"Sessão '{session_id}' não encontrada.")
        return session
