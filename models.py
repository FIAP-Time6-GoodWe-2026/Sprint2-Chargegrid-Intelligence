# =============================================================================
#  ChargeGrid Intelligence — Modelos de Domínio
#  Sprint 2 | FIAP + GoodWe EV Challenge 2026
# =============================================================================

"""
Definição das entidades centrais do sistema:
- SessionStatus : estados do ciclo de vida de uma sessão de recarga
- UserType      : categorias de usuário para tarifação diferenciada
- ChargingSession: representa uma sessão ativa ou encerrada

Nenhuma regra de negócio vive aqui — apenas estrutura de dados.
Isso garante que PowerManager, PricingEngine e ModbusSimulator possam
importar os modelos sem dependências circulares.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enumerações
# ---------------------------------------------------------------------------

class SessionStatus(Enum):
    """
    Ciclo de vida de uma sessão de recarga.

    Mapeamento direto com o registrador Modbus 10017 do GW11K-HCA-20:
        WAITING   → 00 (Idle, conector não conectado)
        PREPARING → 02 (Handshaking with vehicle)
        CHARGING  → 03 (Charging in progress)
        THROTTLED → 10 (Charging interrupted — insufficient power)
        FINISHED  → 04 (Charging completed)
        FAULTED   → 05 (Abnormal alarm)
    """
    WAITING   = "WAITING"
    PREPARING = "PREPARING"
    CHARGING  = "CHARGING"
    THROTTLED = "THROTTLED"   # carregando com potência reduzida
    FINISHED  = "FINISHED"
    FAULTED   = "FAULTED"


class UserType(Enum):
    """
    Categorias de usuário para aplicação de tarifas diferenciadas.

    Compatível com o campo tipo_usuario do Sprint 1 (P / A).
    O tipo CORPORATE foi adicionado no Sprint 2 para o terceiro eixo
    de tarifação dinâmica.
    """
    STANDARD    = "P"   # Padrão — tarifa base
    SUBSCRIBER  = "A"   # Assinante — 15% de desconto
    CORPORATE   = "C"   # Corporativo — tarifa negociada (10% de desconto)


# ---------------------------------------------------------------------------
# Entidade principal
# ---------------------------------------------------------------------------

@dataclass
class ChargingSession:
    """
    Representa uma sessão de recarga do início ao fim.

    Atributos de identidade
    -----------------------
    session_id      : identificador único (UUID4 prefixado com CGI-)
    charger_id      : ID do carregador físico (ex.: "C1", "C2")
    station_id      : ID do posto ao qual o carregador pertence
    vehicle_id      : placa ou identificador do veículo

    Atributos de usuário
    --------------------
    user_name       : nome do condutor
    user_type       : categoria (UserType) usada pela PricingEngine

    Atributos de tempo
    ------------------
    start_time      : datetime de início da sessão
    end_time        : datetime de encerramento (None enquanto ativa)

    Atributos elétricos
    -------------------
    requested_power_kw  : potência solicitada pelo veículo
    allocated_power_kw  : potência efetivamente alocada (após throttling)
    energy_kwh          : energia acumulada na sessão (kWh)

    Atributos financeiros
    ----------------------
    tariff_kwh      : tarifa aplicada R$/kWh (pode variar por eixo)
    total_cost_brl  : custo acumulado em reais

    Atributos de estado
    -------------------
    status          : estado atual (SessionStatus)
    """

    # Identidade
    charger_id: str
    station_id: str
    vehicle_id: str
    user_name: str
    user_type: UserType

    # Elétrico
    requested_power_kw: float

    # Gerados automaticamente
    session_id: str = field(default_factory=lambda: f"CGI-{uuid.uuid4().hex[:8].upper()}")
    start_time: datetime.datetime = field(default_factory=datetime.datetime.now)

    # Preenchidos ao longo da sessão
    end_time: Optional[datetime.datetime] = None
    # Marca de tempo da última vez que a energia foi contabilizada.
    # Usado para acumular energia proporcional ao tempo real decorrido
    # (e não por número de chamadas), garantindo idempotência.
    last_energy_update: Optional[datetime.datetime] = None
    allocated_power_kw: float = 0.0
    energy_kwh: float = 0.0
    tariff_kwh: float = 0.0
    total_cost_brl: float = 0.0
    status: SessionStatus = SessionStatus.WAITING

    # ---------------------------------------------------------------------------
    # Propriedades derivadas
    # ---------------------------------------------------------------------------

    @property
    def duration_seconds(self) -> float:
        """
        Duração da sessão em segundos.

        Para sessões encerradas usa end_time (imutável após finish_session).
        Para sessões ativas usa last_energy_update quando disponível —
        o mesmo timestamp que o motor de energia usa para calcular kWh —
        evitando pequenas inconsistências entre duração exibida e energia
        acumulada quando ambas são lidas em momentos diferentes.
        Fallback para datetime.now() quando a sessão ainda não iniciou
        o relógio de energia (status PREPARING).
        """
        if self.end_time is not None:
            return (self.end_time - self.start_time).total_seconds()
        reference = self.last_energy_update or datetime.datetime.now()
        return (reference - self.start_time).total_seconds()

    @property
    def duration_minutes(self) -> float:
        """Duração da sessão em minutos."""
        return self.duration_seconds / 60.0

    @property
    def is_active(self) -> bool:
        """True se a sessão ainda está em andamento."""
        return self.status in (SessionStatus.CHARGING, SessionStatus.THROTTLED,
                               SessionStatus.PREPARING)

    # ---------------------------------------------------------------------------
    # Representação
    # ---------------------------------------------------------------------------

    def status_line(self) -> str:
        """Linha resumida para exibição no menu interativo."""
        potencia = f"{self.allocated_power_kw:.1f} kW"
        energia  = f"{self.energy_kwh:.2f} kWh"
        custo    = f"R$ {self.total_cost_brl:.2f}"
        duracao  = f"{int(self.duration_minutes)} min"
        return (
            f"[{self.session_id}] {self.charger_id} | "
            f"{self.user_name} ({self.user_type.value}) | "
            f"Status: {self.status.value:<10} | "
            f"Potência: {potencia} | Energia: {energia} | "
            f"Custo: {custo} | Duração: {duracao}"
        )

    def to_report_dict(self) -> dict:
        """
        Serializa a sessão para o gerador de relatórios.
        Todos os campos são tipos primitivos (str, float, int)
        para facilitar formatação e futura persistência em banco.
        """
        return {
            "session_id":         self.session_id,
            "charger_id":         self.charger_id,
            "station_id":         self.station_id,
            "vehicle_id":         self.vehicle_id,
            "user_name":          self.user_name,
            "user_type":          self.user_type.value,
            "status":             self.status.value,
            "start_time":         self.start_time.strftime("%d/%m/%Y %H:%M:%S"),
            "end_time":           self.end_time.strftime("%d/%m/%Y %H:%M:%S") if self.end_time else "—",
            "duration_min":       round(self.duration_minutes, 1),
            "requested_power_kw": self.requested_power_kw,
            "allocated_power_kw": self.allocated_power_kw,
            "energy_kwh":         round(self.energy_kwh, 3),
            "tariff_kwh":         round(self.tariff_kwh, 4),
            "total_cost_brl":     round(self.total_cost_brl, 2),
        }
