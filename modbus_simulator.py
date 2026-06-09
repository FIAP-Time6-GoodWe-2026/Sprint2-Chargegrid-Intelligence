# =============================================================================
#  ChargeGrid Intelligence — Simulador de Protocolo Modbus TCP
#  Sprint 2 | FIAP + GoodWe EV Challenge 2026
# =============================================================================

"""
ModbusSimulator: simula a comunicação Modbus TCP com o carregador GW11K-HCA-20.

Contexto técnico real:
    A linha HCA G2 da GoodWe NÃO suporta OCPP (confirmado na mentoria de
    13/05/2026). O protocolo de comunicação real é Modbus TCP (registrador
    mapeado no documento oficial "交流充电桩二代Modbus协议 V1.0.15").

    Este módulo simula o barramento Modbus: cada operação de leitura (FC03)
    ou escrita (FC06/FC16) é formatada como um frame Modbus real, com:
        - Transaction ID  (2 bytes)
        - Protocol ID     (2 bytes, sempre 0x0000)
        - Length          (2 bytes)
        - Unit ID         (1 byte, endereço do escravo)
        - Function Code   (1 byte)
        - Register Address(2 bytes)
        - Data            (variável)

Registradores utilizados (mapa oficial GoodWe HCA G2):
    10017 — Charging Station Status   (RO) → estado da sessão
    10015 — Charging power            (RO) → potência atual em kW  (÷10)
    10016 — Charging Capacity         (RO) → energia da sessão kWh (÷10)
    10029 — Maximum Charging Power    (RW) → limite de potência     (÷10)
    10025 — Dynamic Load Management   (RW) → habilita/desabilita controle
    10060 — Turn on/off charging      (RW) → 1=off, 2=on
    10026 — Household Circuit Breaker (RW) → corrente máxima em A

Sprint 3+:
    Substituir os prints formatados por conexões reais via pymodbus:
        from pymodbus.client import ModbusTcpClient
        client = ModbusTcpClient(host='192.168.1.100', port=502)
    A assinatura de todos os métodos públicos permanece idêntica.
"""

from __future__ import annotations

import datetime
import logging
import struct
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List

from models import ChargingSession, SessionStatus
from power_manager import AllocationResult

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constantes do protocolo
# ---------------------------------------------------------------------------

MODBUS_PROTOCOL_ID: int = 0x0000
MODBUS_PORT:        int = 502
UNIT_ID:            int = 0x01        # endereço padrão do escravo HCA G2

# Tamanho máximo do log de frames em memória (#R2). Sem este cap, a leitura
# periódica de medidores (on_meter_read, emitida a cada polling de 5s por
# sessão ativa) faria o log crescer indefinidamente — vazamento de memória em
# sessões longas. O log é um buffer circular: mantém apenas os frames recentes,
# que é exatamente o que a tela de diagnóstico Modbus precisa exibir.
MAX_LOG_FRAMES: int = 500

# Function Codes
FC_READ_HOLDING  = 0x03   # Read Holding Registers
FC_WRITE_SINGLE  = 0x06   # Write Single Register
FC_WRITE_MULTI   = 0x10   # Write Multiple Registers (0x10 = 16)

# Mapa de registradores (endereços do documento oficial)
REG = {
    "STATUS":          10017,
    "POWER_KW":        10015,
    "ENERGY_KWH":      10016,
    "MAX_POWER":       10029,
    "DYN_LOAD":        10025,
    "CHARGE_CTRL":     10060,
    "BREAKER_CURRENT": 10026,
    "CHARGE_START_YM": 10158,
    "CHARGE_START_DH": 10159,
    "CHARGE_START_MS": 10160,
    "SESSION_ENERGY":  10016,
    "ACCUMULATED_KWH": 10065,
}

# Mapeamento SessionStatus → código do registrador 10017
STATUS_CODE: Dict[SessionStatus, int] = {
    SessionStatus.WAITING:   0x00,
    SessionStatus.PREPARING: 0x02,
    SessionStatus.CHARGING:  0x03,
    SessionStatus.THROTTLED: 0x03,   # throttled ainda é "charging" para o hardware
    SessionStatus.FINISHED:  0x04,
    SessionStatus.FAULTED:   0x05,
}


# ---------------------------------------------------------------------------
# Frame Modbus TCP
# ---------------------------------------------------------------------------

@dataclass
class ModbusFrame:
    """
    Representa um frame Modbus TCP completo (ADU Application Data Unit).

    Campos conforme especificação Modbus TCP/IP:
        transaction_id : contador sequencial de transações
        protocol_id    : sempre 0x0000 para Modbus TCP
        unit_id        : endereço do dispositivo escravo
        function_code  : FC03 (read), FC06 (write single), FC16 (write multi)
        register_addr  : endereço do registrador alvo
        data           : valor(es) a ler ou escrever
        direction      : "TX" (enviado ao carregador) | "RX" (resposta)
        description    : descrição humana da operação
    """
    transaction_id: int
    unit_id:        int
    function_code:  int
    register_addr:  int
    data:           List[int]
    direction:      str   # "TX" | "RX"
    description:    str
    timestamp:      str   = field(
        default_factory=lambda: datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    )

    def to_hex(self) -> str:
        """
        Serializa o frame no formato hexadecimal conforme Modbus TCP ADU:
            [TID(2)] [PID(2)] [LEN(2)] [UID(1)] [FC(1)] [ADDR(2)] [DATA(n×2)]
        """
        pdu_data = b""
        for val in self.data:
            pdu_data += struct.pack(">H", val & 0xFFFF)

        pdu   = struct.pack(">BH", self.function_code, self.register_addr) + pdu_data
        mbap  = struct.pack(
            ">HHHB",
            self.transaction_id,
            MODBUS_PROTOCOL_ID,
            len(pdu) + 1,
            self.unit_id,
        )
        raw = mbap + pdu
        return " ".join(f"{b:02X}" for b in raw)

    def __str__(self) -> str:
        fc_name = {
            FC_READ_HOLDING: "FC03 Read ",
            FC_WRITE_SINGLE: "FC06 Write",
            FC_WRITE_MULTI:  "FC16 WriteMulti",
        }.get(self.function_code, f"FC{self.function_code:02X}")

        return (
            f"[{self.timestamp}] {self.direction} | {fc_name} | "
            f"Reg {self.register_addr:05d} | "
            f"Data: {[f'0x{d:04X}' for d in self.data]} | "
            f"HEX: {self.to_hex()}\n"
            f"          └─ {self.description}"
        )


# ---------------------------------------------------------------------------
# ModbusSimulator
# ---------------------------------------------------------------------------

class ModbusSimulator:
    """
    Simula o barramento Modbus TCP entre o ChargeGrid e os carregadores HCA G2.

    Mantém um log completo de frames TX/RX para auditoria, relatório
    e demonstração ao avaliador.

    Parâmetros
    ----------
    unit_id  : endereço do escravo Modbus (padrão 0x01)
    verbose  : se True, imprime cada frame no stdout em tempo real
    """

    def __init__(
        self,
        unit_id: int = UNIT_ID,
        verbose: bool = True,
    ) -> None:
        self._unit_id      = unit_id
        self._verbose      = verbose
        self._tid          = 0          # transaction ID auto-incrementado
        # Buffer circular (#R2): mantém só os MAX_LOG_FRAMES mais recentes.
        self._frame_log:   Deque[ModbusFrame] = deque(maxlen=MAX_LOG_FRAMES)
        # Contador acumulado de todos os frames já emitidos (não decai com o cap),
        # preservando a métrica histórica total exibida no dashboard.
        self._total_frames: int = 0
        self._reg_map:     Dict[int, int]    = {}   # espelho do estado dos registradores

    # ------------------------------------------------------------------
    # API pública — eventos do ciclo de sessão
    # ------------------------------------------------------------------

    def on_session_start(self, session: ChargingSession) -> None:
        """
        Emite a sequência Modbus de início de sessão:
            1. TX → FC06 Write 10060 (habilitar carregamento)
            2. TX → FC06 Write 10029 (definir potência máxima)
            3. RX ← FC03 Read  10017 (confirmar status = CHARGING)
            4. RX ← FC03 Read  10015 (confirmar potência entregue)
        """
        self._section(f"INÍCIO DE SESSÃO — {session.session_id}")

        # 1. Habilitar carregamento (registro 10060: 2 = ON)
        self._write(
            REG["CHARGE_CTRL"], [0x0002],
            f"Habilitando carregamento | Carregador: {session.charger_id} "
            f"| Veículo: {session.vehicle_id}"
        )
        self._update_reg(REG["CHARGE_CTRL"], 0x0002)

        # 2. Definir potência máxima (registro 10029, valor ×10)
        power_raw = int(session.allocated_power_kw * 10)
        self._write(
            REG["MAX_POWER"], [power_raw],
            f"Definindo potência máxima: {session.allocated_power_kw:.1f} kW "
            f"(raw: 0x{power_raw:04X})"
        )

        # 3. Ler status e confirmar CHARGING
        status_code = STATUS_CODE[session.status]
        self._read(
            REG["STATUS"], [status_code],
            f"Leitura de status → {status_code:02d} "
            f"({session.status.value}) ✓"
        )

        # 4. Ler potência entregue
        power_read = int(session.allocated_power_kw * 10)
        self._read(
            REG["POWER_KW"], [power_read],
            f"Leitura de potência entregue → {session.allocated_power_kw:.1f} kW ✓"
        )

        self._update_reg(REG["STATUS"],   status_code)
        self._update_reg(REG["MAX_POWER"], power_raw)
        self._update_reg(REG["POWER_KW"], power_read)

    def on_meter_read(self, session: ChargingSession) -> None:
        """
        Leitura periódica dos medidores (equivale ao MeterValues do OCPP).
        Emite FC03 para energia acumulada e potência instantânea.
        """
        energy_raw = int(session.energy_kwh * 10)
        power_raw  = int(session.allocated_power_kw * 10)

        self._read(
            REG["SESSION_ENERGY"], [energy_raw],
            f"MeterRead | Energia sessão: {session.energy_kwh:.3f} kWh "
            f"(raw: 0x{energy_raw:04X})"
        )
        self._read(
            REG["POWER_KW"], [power_raw],
            f"MeterRead | Potência atual: {session.allocated_power_kw:.1f} kW "
            f"(raw: 0x{power_raw:04X})"
        )
        self._update_reg(REG["SESSION_ENERGY"], energy_raw)
        self._update_reg(REG["POWER_KW"],       power_raw)

    def on_throttle(self, session: ChargingSession,
                    result: AllocationResult) -> None:
        """
        Emite a sequência Modbus de redistribuição de potência:
            TX → FC06 Write 10029 (nova potência máxima)
            TX → FC06 Write 10025 (habilitar controle dinâmico)
            RX ← FC03 Read  10017 (confirmar status)
        """
        self._section(
            f"REDISTRIBUIÇÃO DE POTÊNCIA — {len(result.throttle_events)} conector(es)"
        )

        # Habilita Dynamic Load Management (registro 10025)
        self._write(
            REG["DYN_LOAD"], [0x0001],
            "Ativando Dynamic Load Management (reg. 10025 = 1)"
        )
        self._update_reg(REG["DYN_LOAD"], 0x0001)

        for sid, old_kw, new_kw in result.throttle_events:
            new_raw = int(new_kw * 10)
            self._write(
                REG["MAX_POWER"], [new_raw],
                f"Throttle | Sessão {sid} | "
                f"{old_kw:.1f} kW → {new_kw:.1f} kW "
                f"(raw: 0x{new_raw:04X})"
            )

        # Lê status atual do primeiro conector afetado
        self._read(
            REG["STATUS"], [STATUS_CODE[session.status]],
            f"Confirmação status pós-throttle → {session.status.value}"
        )

    def on_rebalance(self, result: AllocationResult) -> None:
        """
        Emite Modbus de restauração de potência após liberação de capacidade.
        """
        self._section("REBALANCEAMENTO — Restaurando potência")

        for sid, old_kw, new_kw in result.throttle_events:
            new_raw = int(new_kw * 10)
            self._write(
                REG["MAX_POWER"], [new_raw],
                f"Restaurar | Sessão {sid} | "
                f"{old_kw:.1f} kW → {new_kw:.1f} kW "
                f"(raw: 0x{new_raw:04X})"
            )

        # Desabilita controle dinâmico se carga normalizada
        self._write(
            REG["DYN_LOAD"], [0x0000],
            "Desativando Dynamic Load Management (reg. 10025 = 0)"
        )
        self._update_reg(REG["DYN_LOAD"], 0x0000)

    def on_session_end(self, session: ChargingSession) -> None:
        """
        Emite a sequência Modbus de encerramento:
            TX → FC06 Write 10060 (desligar carregamento)
            RX ← FC03 Read  10016 (energia total da sessão)
            RX ← FC03 Read  10017 (confirmar status FINISHED/FAULTED)
        """
        self._section(f"ENCERRAMENTO DE SESSÃO — {session.session_id}")

        # Desligar carregamento (10060 = 1)
        self._write(
            REG["CHARGE_CTRL"], [0x0001],
            f"Encerrando carregamento | Carregador: {session.charger_id}"
        )
        self._update_reg(REG["CHARGE_CTRL"], 0x0001)

        # Ler energia total da sessão
        energy_raw = int(session.energy_kwh * 10)
        self._read(
            REG["SESSION_ENERGY"], [energy_raw],
            f"Energia total da sessão: {session.energy_kwh:.3f} kWh "
            f"| Custo: R$ {session.total_cost_brl:.2f}"
        )

        # Confirmar status final
        status_code = STATUS_CODE.get(session.status, 0x04)
        self._read(
            REG["STATUS"], [status_code],
            f"Status final → {status_code:02d} ({session.status.value}) ✓"
        )

        self._update_reg(REG["STATUS"],   status_code)
        self._update_reg(REG["POWER_KW"], 0x0000)

    def on_breaker_config(self, breaker_current_a: int) -> None:
        """
        Escreve a corrente máxima do disjuntor no registrador 10026.
        Executado na inicialização do sistema.
        """
        self._section("CONFIGURAÇÃO DO DISJUNTOR PRINCIPAL")
        self._write(
            REG["BREAKER_CURRENT"], [breaker_current_a],
            f"Corrente máxima do disjuntor: {breaker_current_a} A "
            f"(reg. 10026)"
        )
        self._update_reg(REG["BREAKER_CURRENT"], breaker_current_a)

    # ------------------------------------------------------------------
    # Registro de estado e relatório
    # ------------------------------------------------------------------

    def register_snapshot(self) -> Dict[int, int]:
        """Retorna uma cópia do estado atual dos registradores simulados."""
        return dict(self._reg_map)

    def frame_count(self) -> int:
        """
        Total acumulado de frames já emitidos na sessão.

        Reflete todos os frames desde o início, mesmo após o buffer circular
        (#R2) descartar os mais antigos — preserva a métrica histórica exibida
        no dashboard. Para o número de frames atualmente em memória, use
        ``len(get_log())``.
        """
        return self._total_frames

    def get_log(self) -> List[ModbusFrame]:
        """Retorna os frames atualmente em buffer (até MAX_LOG_FRAMES recentes)."""
        return list(self._frame_log)

    def print_summary(self) -> str:
        """
        Gera um resumo do log Modbus para inclusão no relatório.
        """
        tx = sum(1 for f in self._frame_log if f.direction == "TX")
        rx = sum(1 for f in self._frame_log if f.direction == "RX")
        linhas = [
            "╔══════════════════════════════════════════════════════════╗",
            "║         RESUMO DO LOG MODBUS TCP — ChargeGrid            ║",
            f"║  Total de frames : {self.frame_count():<5}  TX: {tx:<5}  RX: {rx:<5}       ║",
            f"║  Protocolo       : Modbus TCP (porta {MODBUS_PORT})              ║",
            f"║  Endereço escravo: 0x{self._unit_id:02X} (Unit ID)                    ║",
            "╠══════════════════════════════════════════════════════════╣",
            "║ Registradores monitorados:                               ║",
        ]
        nomes = {
            10017: "Charging Status   ",
            10015: "Charging Power    ",
            10016: "Session Energy    ",
            10029: "Max Power Limit   ",
            10025: "Dyn Load Mgmt     ",
            10060: "Charge Control    ",
            10026: "Breaker Current   ",
        }
        for addr, nome in nomes.items():
            val = self._reg_map.get(addr, "—")
            val_str = f"0x{val:04X}" if isinstance(val, int) else val
            linhas.append(f"║  [{addr}] {nome}: {val_str:<10}                  ║")
        linhas.append(
            "╚══════════════════════════════════════════════════════════╝"
        )
        return "\n".join(linhas)

    # ------------------------------------------------------------------
    # Helpers privados — emissão de frames
    # ------------------------------------------------------------------

    def _next_tid(self) -> int:
        self._tid = (self._tid + 1) & 0xFFFF
        return self._tid

    def _write(self, register: int, data: List[int], description: str) -> ModbusFrame:
        fc = FC_WRITE_SINGLE if len(data) == 1 else FC_WRITE_MULTI
        frame = ModbusFrame(
            transaction_id=self._next_tid(),
            unit_id=self._unit_id,
            function_code=fc,
            register_addr=register,
            data=data,
            direction="TX",
            description=description,
        )
        self._frame_log.append(frame)
        self._total_frames += 1
        if self._verbose:
            print(frame)
        return frame

    def _read(self, register: int, data: List[int], description: str) -> ModbusFrame:
        frame = ModbusFrame(
            transaction_id=self._next_tid(),
            unit_id=self._unit_id,
            function_code=FC_READ_HOLDING,
            register_addr=register,
            data=data,
            direction="RX",
            description=description,
        )
        self._frame_log.append(frame)
        self._total_frames += 1
        if self._verbose:
            print(frame)
        return frame

    def _section(self, title: str) -> None:
        """Imprime um separador de seção no log."""
        if self._verbose:
            linha = f"\n{'─' * 60}"
            print(f"{linha}\n  📡 MODBUS | {title}\n{linha}")

    def _update_reg(self, addr: int, value: int) -> None:
        """Atualiza o espelho interno dos registradores."""
        self._reg_map[addr] = value
