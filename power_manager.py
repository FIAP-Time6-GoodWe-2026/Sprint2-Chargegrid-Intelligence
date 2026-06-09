# =============================================================================
#  ChargeGrid Intelligence — Gerenciador de Potência
#  Sprint 2 | FIAP + GoodWe EV Challenge 2026
# =============================================================================

"""
PowerManager: controle inteligente de demanda de potência entre carregadores.

Responsabilidades:
    - Manter o limite máximo configurável da instalação (kW)
    - Calcular a potência alocável para uma nova sessão
    - Redistribuir carga proporcionalmente quando o limite é excedido
    - Registrar cada decisão de redistribuição (base para o ModbusSimulator)

Algoritmo de redistribuição (inspirado no controle dinâmico do GW11K-HCA-20):
    O hardware real reduz a corrente de cada conector até pausar o carregamento
    quando a corrente total se aproxima do limite do disjuntor principal
    (registrador Modbus 10026). Replicamos essa lógica em software:

        potência_por_sessão = limite_disponível / nº_sessões_ativas

    Se o resultado cair abaixo da potência mínima do HCA (4.2 kW trifásico),
    a sessão entra em THROTTLED mas não é encerrada — o hardware real também
    não desconecta o veículo, apenas reduz a entrega.

Sprint 3+:
    O limite máximo poderá ser lido dinamicamente via Modbus TCP
    (registrador 10026 do hardware real) em vez de ser configurado manualmente.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, runtime_checkable

from models import ChargingSession, SessionStatus


@runtime_checkable
class SessionStore(Protocol):
    """
    Interface estrutural (PEP 544) que o PowerManager exige de um gerenciador
    de sessões. Define apenas os métodos efetivamente utilizados aqui.

    Tanto o SessionManager global quanto o adaptador _PostoSM (que expõe ao
    PowerManager somente as sessões de um posto) satisfazem este protocolo,
    sem precisar herdar de uma classe comum. Isso desacopla o PowerManager da
    implementação concreta do SessionManager e elimina o falso positivo de
    tipagem ao injetar o adaptador por posto.
    """

    def list_active(self) -> List[ChargingSession]: ...
    def total_allocated_power_kw(self) -> float: ...
    def active_count(self) -> int: ...
    def throttle_session(self, session_id: str, power_kw: float) -> ChargingSession: ...
    def restore_session(self, session_id: str, power_kw: float) -> ChargingSession: ...

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constantes do hardware GW11K-HCA-20
# ---------------------------------------------------------------------------

POTENCIA_NOMINAL_KW:  float = 11.0   # potência nominal por conector
POTENCIA_MINIMA_KW:   float = 4.2    # mínima trifásica (registrador 10029, limite inferior)
# Limiar de throttle = 1.0 (100%): redistribuição começa exatamente quando a
# carga projetada excede o limite físico da instalação (33 kW por posto).
# Com 5 conectores de 11 kW, o 4º conector (44 kW projetado > 33 kW) já aciona
# a redistribuição automática — sem margem de folga, como especificado.
LIMIAR_THROTTLE:      float = 1.00
# MARGEM_REDISTRIBUICAO — OBSOLETA (#B29). Pertencia ao antigo "Caso 2
# preventivo", removido por ser código inalcançável com LIMIAR_THROTTLE = 1.00.
# Mantida apenas para compatibilidade de import; não é mais usada na lógica.
MARGEM_REDISTRIBUICAO: float = 0.95

# Pesos de prioridade por tipo de usuário na redistribuição de potência.
# Assinante recebe 20% a mais que a base equânime; Corporativo recebe 10% a mais.
# O tipo Padrão absorve a diferença — o total nunca ultrapassa o limite físico.
PESO_USUARIO: dict = {
    "STANDARD":   1.00,   # base
    "SUBSCRIBER": 1.20,   # +20%
    "CORPORATE":  1.10,   # +10%
}


# ---------------------------------------------------------------------------
# Resultado de alocação — retornado ao main.py / ModbusSimulator
# ---------------------------------------------------------------------------

@dataclass
class AllocationResult:
    """
    Encapsula o resultado de cada decisão de alocação de potência.

    Campos
    ------
    granted_kw          : potência efetivamente concedida à nova sessão
    redistributed       : True se sessões existentes foram throttled
    rejected            : True quando a sessão foi recusada por limite físico
    severity            : nível semântico da decisão para UI — "ok" | "warning" | "danger"
                          Derivado pelo PowerManager; o template usa este campo
                          em vez de inspecionar substrings de ``message``.
    affected_sessions   : IDs das sessões que tiveram potência reduzida
    load_before_kw      : carga total antes da alocação
    load_after_kw       : carga total após alocação e redistribuição
    limit_kw            : limite máximo configurado no momento da decisão
    occupancy_pct       : ocupação percentual após alocação
    throttle_events     : lista de (session_id, antiga_kw, nova_kw) para o log Modbus
    message             : descrição humana da decisão (impressa no menu)
    """
    granted_kw:        float
    redistributed:     bool
    rejected:          bool             = False
    severity:          str              = "ok"      # "ok" | "warning" | "danger"
    affected_sessions: List[str]        = field(default_factory=list)
    load_before_kw:    float            = 0.0
    load_after_kw:     float            = 0.0
    limit_kw:          float            = 0.0
    occupancy_pct:     float            = 0.0
    throttle_events:   List[tuple]      = field(default_factory=list)
    message:           str              = ""


# ---------------------------------------------------------------------------
# PowerManager
# ---------------------------------------------------------------------------

class PowerManager:
    """
    Controla a demanda de potência de uma instalação de eletropostos.

    Parâmetros
    ----------
    session_manager : SessionStore
        Referência injetada — PowerManager nunca instancia SessionManager.
    limit_kw : float
        Limite máximo da instalação em kW.
        Padrão: 33 kW (3 × GW11K-HCA-20 de 11 kW cada).
    """

    def __init__(
        self,
        session_manager: SessionStore,
        limit_kw: float = 33.0,
    ) -> None:
        if limit_kw <= 0:
            raise ValueError("O limite de potência deve ser positivo.")
        self._sm    = session_manager
        self._limit = limit_kw
        self._history: List[AllocationResult] = []
        logger.info(
            "PowerManager inicializado | Limite: %.1f kW | Limiar throttle: %.0f%%",
            limit_kw, LIMIAR_THROTTLE * 100,
        )

    # ------------------------------------------------------------------
    # Propriedades
    # ------------------------------------------------------------------

    @property
    def limit_kw(self) -> float:
        return self._limit

    @limit_kw.setter
    def limit_kw(self, value: float) -> None:
        if value <= 0:
            raise ValueError("Limite deve ser positivo.")
        logger.info("Limite alterado: %.1f kW → %.1f kW", self._limit, value)
        self._limit = value

    @property
    def available_kw(self) -> float:
        """Capacidade restante disponível para novas sessões."""
        return round(self._limit - self._sm.total_allocated_power_kw(), 2)

    @property
    def occupancy_pct(self) -> float:
        """Percentual do limite total em uso [0–100]."""
        return round((self._sm.total_allocated_power_kw() / self._limit) * 100, 1)

    @property
    def history(self) -> List[AllocationResult]:
        """Histórico de decisões de alocação (para relatório)."""
        return list(self._history)

    # ------------------------------------------------------------------
    # Alocação de nova sessão
    # ------------------------------------------------------------------

    def allocate(self, session: ChargingSession) -> AllocationResult:
        """
        Calcula e concede potência para uma sessão recém-criada.

        Fluxo de decisão:
            0. Verifica viabilidade física: se (n+1) sessões × POTENCIA_MINIMA
               excede o limite da instalação, a nova sessão é RECUSADA —
               conectar mais um conector causaria subdivisão abaixo do mínimo
               operacional do HCA (4.2 kW trifásico), o que desligaria o hardware.
            1. Se carga atual + solicitada ≤ 90% do limite → concede integral.
            2. Se cabe dentro do limite mas passa o limiar → concede e redistribui.
            3. Se não cabe → redistribui todas à potência equânime e concede.

        O campo ``rejected`` do resultado indica recusa (bug #2 corrigido).
        ``load_after_kw`` é calculado APÓS aplicar os throttles reais
        (bug #7 corrigido — divergência entre valor reportado e estado real).

        Args:
            session : ChargingSession com status PREPARING

        Returns:
            AllocationResult. Se ``rejected=True``, o chamador deve encerrar
            a sessão com status FAULTED e informar o usuário.
        """
        solicitada      = session.requested_power_kw
        carga_atual     = self._sm.total_allocated_power_kw()
        carga_projetada = round(carga_atual + solicitada, 2)
        # Exclui a nova sessão (status PREPARING, allocated=0) da lista de ativas —
        # ela ainda não tem potência alocada e não deve ser redistribuída junto
        # com as sessões em andamento.
        sessoes_ativas  = [
            s for s in self._sm.list_active()
            if s.session_id != session.session_id
        ]
        n_total         = len(sessoes_ativas) + 1   # inclui a nova sessão

        result = AllocationResult(
            granted_kw=0.0,
            redistributed=False,
            load_before_kw=carga_atual,
            limit_kw=self._limit,
        )

        # ── Caso 0: inviabilidade física — RECUSA ───────────────────────────
        # Se distribuirmos o mínimo operacional para todas as sessões (incluindo
        # a nova), a soma ultrapassa o limite da instalação. Conectar mais um
        # conector causaria potências abaixo de 4.2 kW, que o hardware HCA
        # não suporta (registrador 10029 tem piso de 4.2 kW trifásico).
        minimo_total = round(n_total * POTENCIA_MINIMA_KW, 2)
        if minimo_total > self._limit:
            result.rejected  = True
            result.severity  = "danger"
            result.granted_kw = 0.0
            result.load_after_kw = carga_atual
            result.occupancy_pct = round((carga_atual / self._limit) * 100, 1)
            result.message = (
                f"🚫 Conexão recusada: {n_total} conectores × {POTENCIA_MINIMA_KW} kW mín "
                f"= {minimo_total:.1f} kW > limite {self._limit:.1f} kW. "
                f"Aguarde liberação de um conector."
            )
            self._history.append(result)
            logger.warning("Alocação recusada: %s", result.message)
            return result

        # ── Caso 1: carga projetada dentro do limiar ────────────────────────
        if carga_projetada <= self._limit * LIMIAR_THROTTLE:
            result.granted_kw    = solicitada
            result.severity      = "ok"
            result.load_after_kw = carga_projetada
            result.occupancy_pct = round((carga_projetada / self._limit) * 100, 1)
            result.message = (
                f"✅ Potência integral concedida: {solicitada:.1f} kW "
                f"(carga total: {carga_projetada:.1f}/{self._limit:.1f} kW "
                f"— {result.occupancy_pct:.1f}%)"
            )

        # ── Caso 2: não cabe — redistribui tudo com prioridade por tipo ──────
        # Nota (#B29): com LIMIAR_THROTTLE = 1.00, o antigo "Caso 2 preventivo"
        # (limite < projetada <= limite) cobria um intervalo vazio e era código
        # inalcançável. Foi removido junto com a função _redistribute órfã.
        # Resta a dicotomia: cabe no limite (Caso 1) ou estoura (este Caso 2).
        else:
            # Distribui o limite total entre TODAS as sessões (existentes + nova)
            # pela prioridade de tipo. Antes, a nova sessão recebia uma fatia
            # equânime fixa (limite/n) sem participar da ponderação por peso —
            # então um assinante que entrasse por último num posto cheio podia
            # ficar com MENOS potência que um corporativo já presente, violando
            # a prioridade. Agora a nova sessão entra no mesmo cálculo de pesos.
            todas = list(sessoes_ativas) + [session]
            alvos = self._target_por_peso(todas, self._limit)

            concedida = alvos.get(session.session_id, round(self._limit / n_total, 2))
            result.granted_kw    = concedida
            result.redistributed = True
            result.severity      = "danger"

            if sessoes_ativas:
                # Aplica o throttle às existentes usando os mesmos alvos por peso
                events = []
                for s in sessoes_ativas:
                    nova = alvos[s.session_id]
                    if abs(nova - s.allocated_power_kw) > 0.1:
                        old = s.allocated_power_kw
                        self._sm.throttle_session(s.session_id, nova)
                        events.append((s.session_id, old, nova))
                result.throttle_events = events
                result.affected_sessions = [e[0] for e in events]

            # load_after calculado APÓS throttles reais (corrige bug #7)
            result.load_after_kw = round(
                self._sm.total_allocated_power_kw() + concedida, 2
            )
            result.occupancy_pct = round(
                (result.load_after_kw / self._limit) * 100, 1
            )
            result.message = (
                f"🔴 Limite excedido — redistribuição automática por prioridade: "
                f"{len(sessoes_ativas)} sessão(ões) reajustadas "
                f"(nova sessão recebe {concedida:.1f} kW) "
                f"| carga final: {result.load_after_kw:.1f}/{self._limit:.1f} kW"
                f" ({result.occupancy_pct:.1f}%)"
            )

        self._history.append(result)
        logger.info("Alocação: %s", result.message)
        return result

    # ------------------------------------------------------------------
    # Reavaliação periódica (chamada após encerramento de sessão)
    # ------------------------------------------------------------------

    def rebalance(self) -> Optional[AllocationResult]:
        """
        Restaura potência às sessões throttled após liberação de capacidade.

        Distribuição com prioridade por tipo de usuário (#B26):
            A potência total é repartida entre TODAS as sessões ativas na
            proporção de seus pesos (PESO_USUARIO), exatamente como no allocate.
            Assim Assinante (+20%) e Corporativo (+10%) mantêm fatia maior também
            após o encerramento de um conector — antes o rebalance distribuía
            limite/n igual e a prioridade concedida pelo allocate desaparecia no
            primeiro rebalanceamento.

        Só atua se a carga atual estiver abaixo de 80% do limite, evitando
        restaurações que imediatamente disparariam outro throttle.

        Para cada sessão, a nova potência é o mínimo entre (a) sua fatia
        proporcional do limite, (b) a potência originalmente solicitada e
        (c) o disponível restante, com piso no mínimo do hardware. Sessões cuja
        fatia atinge o solicitado voltam a CHARGING; as demais permanecem
        THROTTLED com potência maior (transição feita por restore_session, #B3).

        Returns:
            AllocationResult com os eventos de restauração, ou None se
            não houve nada a restaurar ou margem insuficiente.
        """
        sessoes_ativas = self._sm.list_active()
        if not sessoes_ativas:
            return None

        carga_atual = self._sm.total_allocated_power_kw()

        # Nota (#R1): não há mais guard de "carga < 80% do limite". Ele era
        # arbitrário e bloqueava restaurações legítimas quando a carga estava
        # entre 80% e 100% (havia margem livre, mas o rebalance recusava). O
        # loop abaixo recalcula o disponível a cada iteração e limita cada
        # sessão por min(alvo_proporcional, solicitado, disponível), portanto
        # é matematicamente impossível ultrapassar o limite — o guard tornou-se
        # redundante além de prejudicial.

        throttled = [
            s for s in sessoes_ativas
            if s.status == SessionStatus.THROTTLED
        ]
        if not throttled:
            return None

        # Alvo proporcional por peso de tipo, sobre TODAS as ativas (mesma base
        # do allocate). Restauramos apenas as throttled; não-throttled mantêm-se.
        alvos = self._target_por_peso(sessoes_ativas, self._limit)

        # Ordena throttled: mais reduzidas primeiro (maior ganho por restauração)
        throttled.sort(key=lambda s: s.allocated_power_kw)

        events: List[tuple] = []

        for s in throttled:
            # Recalcula disponível a cada iteração — estado muda a cada restore
            disponivel = round(self._limit - self._sm.total_allocated_power_kw(), 2)
            if disponivel <= 0.1:
                break   # limite esgotado, para aqui

            # Fatia proporcional, limitada por solicitado, disponível e piso mínimo
            alvo = alvos.get(s.session_id, s.allocated_power_kw)
            nova = round(
                min(alvo, s.requested_power_kw,
                    s.allocated_power_kw + disponivel),
                2,
            )
            nova = max(nova, POTENCIA_MINIMA_KW)

            if nova <= s.allocated_power_kw + 0.1:
                continue   # ganho insignificante — passa para a próxima

            old = s.allocated_power_kw
            self._sm.restore_session(s.session_id, nova)
            events.append((s.session_id, old, nova))

        if not events:
            return None

        carga_apos = self._sm.total_allocated_power_kw()
        result = AllocationResult(
            granted_kw=0.0,
            redistributed=True,
            severity="ok",
            affected_sessions=[e[0] for e in events],
            load_before_kw=carga_atual,
            load_after_kw=carga_apos,
            limit_kw=self._limit,
            throttle_events=events,
            message=(
                f"✅ Rebalanceamento: {len(events)} sessão(ões) restauradas "
                f"(carga: {carga_apos:.1f}/{self._limit:.1f} kW "
                f"— {round(carga_apos/self._limit*100,1):.1f}%)"
            ),
        )
        self._history.append(result)
        logger.info("Rebalanceamento: %s", result.message)
        return result

    # ------------------------------------------------------------------
    # Status da instalação
    # ------------------------------------------------------------------

    def status_report(self) -> str:
        """Retorna um painel de status formatado para o menu interativo."""
        ativas = self._sm.list_active()
        linhas = [
            "┌─────────────────────────────────────────────────┐",
            "│  PAINEL DE POTÊNCIA                             │",
            f"│  Limite instalação : {self._limit:>6.1f} kW               │",
            f"│  Em uso            : {self._sm.total_allocated_power_kw():>6.1f} kW  "
            f"({self.occupancy_pct:>5.1f}%)      │",
            f"│  Disponível        : {self.available_kw:>6.1f} kW               │",
            f"│  Sessões ativas    : {self._sm.active_count():>3d}                      │",
            "├─────────────────────────────────────────────────┤",
        ]
        if ativas:
            for s in ativas:
                flag = "⚡" if s.status == SessionStatus.CHARGING else "🔻"
                linhas.append(
                    f"│  {flag} {s.charger_id:<6} {s.user_name:<15} "
                    f"{s.allocated_power_kw:>5.1f} kW  {s.status.value:<10}│"
                )
        else:
            linhas.append("│  Nenhuma sessão ativa.                          │")
        linhas.append("└─────────────────────────────────────────────────┘")
        return "\n".join(linhas)

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    @staticmethod
    def _target_por_peso(
        sessions: List[ChargingSession], total_kw: float
    ) -> Dict[str, float]:
        """
        Calcula a fatia de potência de cada sessão pela proporção de seu peso
        de tipo (PESO_USUARIO), normalizada pela soma dos pesos.

        Função pura (não muta estado): usada tanto pela redistribuição do
        allocate (estouro) quanto pelo rebalance, garantindo que a prioridade
        por tipo de usuário seja idêntica nos dois caminhos (#B26).

        Não aplica MARGEM_REDISTRIBUICAO: o total_kw já é o alvo exato a
        distribuir. Aplicar margem deixaria a soma sistematicamente abaixo do
        limite e tornaria a fatia da nova sessão maior que a das existentes
        (#B1/#B2). A fatia bruta é apenas clipada pelo solicitado e pelo piso.

        Args:
            sessions : sessões entre as quais repartir
            total_kw : potência total a distribuir

        Returns:
            {session_id → potência alvo em kW (2 casas)}
        """
        if not sessions:
            return {}
        pesos = {s.session_id: PESO_USUARIO.get(s.user_type.name, 1.0)
                 for s in sessions}
        soma = sum(pesos.values())
        alvos: Dict[str, float] = {}
        for s in sessions:
            proporcao = pesos[s.session_id] / soma if soma else 1.0 / len(sessions)
            fatia = total_kw * proporcao
            fatia = min(fatia, s.requested_power_kw)
            fatia = max(fatia, POTENCIA_MINIMA_KW)
            alvos[s.session_id] = round(fatia, 2)
        return alvos

    def _redistribute_to(
        self, sessions: List[ChargingSession], target_kw: float
    ) -> List[tuple]:
        """
        Aplica throttle às sessões existentes repartindo ``target_kw`` por
        prioridade de tipo de usuário (via _target_por_peso). Assinante (+20%)
        e Corporativo (+10%) recebem fatias maiores; o tipo Padrão absorve a
        diferença.

        Args:
            sessions  : sessões ativas a throttlar
            target_kw : potência total disponível para as sessões existentes
                        (a fatia da nova sessão já foi reservada pelo allocate)

        Returns:
            Lista de eventos (session_id, antiga_kw, nova_kw) para o log Modbus.
        """
        if not sessions:
            return []

        alvos = self._target_por_peso(sessions, target_kw)
        events = []
        for s in sessions:
            nova = alvos[s.session_id]
            if abs(nova - s.allocated_power_kw) > 0.1:
                old = s.allocated_power_kw
                self._sm.throttle_session(s.session_id, nova)
                events.append((s.session_id, old, nova))
        return events
