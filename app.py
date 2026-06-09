# =============================================================================
#  ChargeGrid Intelligence — Servidor Web Flask
#  Sprint 2 | FIAP + GoodWe EV Challenge 2026
# =============================================================================

"""
Servidor Flask: camada de apresentação e roteamento HTTP.

Nenhuma regra de negócio vive aqui. Todas as decisões são delegadas:
    SessionManager  → ciclo de vida das sessões
    PowerManager    → controle de demanda de potência
    PricingEngine   → tarifação dinâmica em 3 eixos
    ModbusSimulator → log de frames Modbus TCP (registradores HCA G2)

Nota de arquitetura — estado global (#11):
    Os objetos sm/pm/pe/mb são globais de módulo. Isso funciona corretamente
    em modo debug (single-process, single-thread do Flask). Em produção com
    múltiplos workers (gunicorn), cada worker teria seu próprio estado,
    causando divergência de sessões entre requests. A solução definitiva é
    o Sprint 3, onde SessionManager será substituído por um repositório com
    SQLAlchemy + PostgreSQL, tornando o estado compartilhado via banco.

    Um threading.Lock protege as operações críticas do SessionManager contra
    condições de corrida em modo single-process com múltiplas threads (#12).

Rotas do Sprint 1 (mantidas):
    GET  /                          → mapa de postos
    GET  /posto/<id>                → carregadores do posto
    GET  /posto/<id>/carregador/<id>→ formulário de sessão
    POST /sessao                    → processar nova sessão (Sprint 1 flow)

Rotas novas do Sprint 2:
    GET  /dashboard                 → painel central de gerenciamento
    POST /dashboard/nova-sessao     → criar sessão via dashboard
    POST /dashboard/encerrar        → encerrar sessão via dashboard
    GET  /api/status                → JSON com estado atual (polling JS)
    GET  /relatorio                 → relatório consolidado de todas as sessões
    GET  /modbus-log                → log de frames Modbus
"""

import datetime
import logging
import os
import re
import threading

from flask import (Flask, flash, jsonify, redirect, render_template,
                   request, url_for)

from logica_recarga import processar_sessao   # Sprint 1 — intacto
from models import UserType, SessionStatus
from session_manager import SessionManager
from power_manager import PowerManager
from pricing_engine import PricingEngine
from modbus_simulator import ModbusSimulator

# ---------------------------------------------------------------------------
# App e logging
# ---------------------------------------------------------------------------

app = Flask(__name__)

# #13 — SECRET_KEY lida de variável de ambiente.
# Em desenvolvimento, usa o fallback hardcoded.
# Em produção: export CHARGEGRID_SECRET_KEY="<valor-aleatorio-seguro>"
app.config["SECRET_KEY"] = os.environ.get(
    "CHARGEGRID_SECRET_KEY",
    "chargegrid-intelligence-fiap-goodwe-2026-dev-only",
)


# Tradução dos tipos de usuário para exibição em português.
# Aceita o nome do enum (STANDARD), o código (P/A/C) ou o próprio Enum.
_TIPO_USUARIO_PT = {
    "STANDARD": "Padrão",
    "SUBSCRIBER": "Assinante",
    "CORPORATE": "Corporativo",
    "P": "Padrão",
    "A": "Assinante",
    "C": "Corporativo",
}


@app.template_filter("tipo_pt")
def _tipo_pt(valor) -> str:
    """Filtro Jinja: traduz um tipo de usuário para português.

    Uso no template: {{ s.user_type.name | tipo_pt }} → "Padrão".
    Tolera o nome do enum, o código de uma letra ou um objeto Enum.
    """
    if hasattr(valor, "name"):        # objeto UserType
        valor = valor.name
    return _TIPO_USUARIO_PT.get(str(valor).upper(), str(valor).title())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler("chargegrid.log", encoding="utf-8")],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Estado global da aplicação (Sprint 3 → banco de dados via ORM)
# ---------------------------------------------------------------------------

# Limite por posto: 3 × GW11K-HCA-20 de 11 kW = 33 kW.
# Com 5 conectores, throttle ativa a partir do 3º carro (33/5 < 11kW cada).
# Cada posto tem seu próprio PowerManager — throttle é isolado por posto,
# não afetando sessões de outros postos.
LIMITE_POR_POSTO_KW = 33.0

sm = SessionManager()

# Um PowerManager por posto — isolamento de throttle por instalação física
# O PowerManager recebe um SessionManager filtrado via adaptador leve
class _PostoSM:
    """Adaptador que expõe ao PowerManager apenas as sessões de um posto."""
    def __init__(self, sm_global: SessionManager, posto_id: str) -> None:
        self._sm       = sm_global
        self._posto_id = posto_id

    def list_active(self):
        return [s for s in self._sm.list_active()
                if s.charger_id.startswith(self._posto_id)]

    def list_chargers(self):
        return {k: v for k, v in self._sm.list_chargers().items()
                if k.startswith(self._posto_id)}

    def total_allocated_power_kw(self) -> float:
        return round(sum(s.allocated_power_kw for s in self.list_active()), 2)

    def active_count(self) -> int:
        return len(self.list_active())

    def occupancy_ratio(self) -> float:
        total = len(self.list_chargers())
        return self.active_count() / max(total, 1)

    def get_session(self, session_id):
        return self._sm.get_session(session_id)

    def accrue_energy(self, session_id):
        return self._sm.accrue_energy(session_id)

    def restore_session(self, session_id, power_kw):
        return self._sm.restore_session(session_id, power_kw)

    def throttle_session(self, session_id, power_kw):
        return self._sm.throttle_session(session_id, power_kw)

    def finish_session(self, session_id, **kwargs):
        return self._sm.finish_session(session_id, **kwargs)

# Instância um PowerManager por posto
_posto_sms = {pid: _PostoSM(sm, pid) for pid in ["P1", "P2", "P3"]}
posto_pms  = {pid: PowerManager(_posto_sms[pid], limit_kw=LIMITE_POR_POSTO_KW)
              for pid in ["P1", "P2", "P3"]}

# pm global para compatibilidade com código legado (relatório, etc.)
# Aponta para P1 como default; as rotas usam _get_pm(posto_id)
pm = posto_pms["P1"]

def _get_pm(posto_id: str) -> PowerManager:
    """Retorna o PowerManager do posto. Fallback para P1 se inválido."""
    return posto_pms.get(posto_id, posto_pms["P1"])

pe = PricingEngine(sm)
mb = ModbusSimulator(verbose=False)

# #12 — Lock global para serializar operações críticas de estado
# (create_session / finish_session) em ambientes multi-thread.
# Não resolve o problema de multi-process (gunicorn) — isso é Sprint 3.
_state_lock = threading.Lock()

mb.on_breaker_config(breaker_current_a=50)

# ---------------------------------------------------------------------------
# Dados simulados de postos (do Sprint 1 — mantidos)
# ---------------------------------------------------------------------------

POSTOS = {
    "P1": {
        "id": "P1", "nome": "ChargeGrid Paulista",
        "endereco": "Av. Paulista, 1578 — São Paulo, SP",
        "lat_px": 45, "lng_px": 40,
        "lat": -23.561414, "lng": -46.655881,   # Av. Paulista (MASP)
        "disponivel": True, "total_carregadores": 5,
        "carregadores_livres": 5, "distancia": "3.5 km",
        "em_throttle": False,
    },
    "P2": {
        "id": "P2", "nome": "ChargeGrid Faria Lima",
        "endereco": "Av. Brigadeiro Faria Lima, 3477 — São Paulo, SP",
        "lat_px": 25, "lng_px": 60,
        "lat": -23.586368, "lng": -46.682606,   # Av. Faria Lima (Itaim)
        "disponivel": True, "total_carregadores": 5,
        "carregadores_livres": 5, "distancia": "0.6 km",
        "em_throttle": False,
    },
    "P3": {
        "id": "P3", "nome": "ChargeGrid Berrini",
        "endereco": "Av. Eng. Luís Carlos Berrini, 1681 — São Paulo, SP",
        "lat_px": 65, "lng_px": 70,
        "lat": -23.609678, "lng": -46.694540,   # Av. Berrini (Brooklin)
        "disponivel": True, "total_carregadores": 5,
        "carregadores_livres": 5, "distancia": "3.2 km",
        "em_throttle": False,
    },
}

USER_TYPE_MAP = {
    "P": UserType.STANDARD,
    "A": UserType.SUBSCRIBER,
    "C": UserType.CORPORATE,
}

# Sufixo do conector VIP, exclusivo para assinantes (um por posto).
CONECTOR_VIP = "C5"


def _eh_conector_vip(charger_id: str) -> bool:
    """
    Indica se o conector é o VIP (C5), exclusivo para assinantes.

    Aceita tanto o id completo (\"P1-C5\") quanto o sufixo (\"C5\").
    """
    return charger_id.upper().endswith(CONECTOR_VIP)

# ---------------------------------------------------------------------------
# Validação de entrada compartilhada (#B30, #B31)
# ---------------------------------------------------------------------------

# Placa Mercosul (ABC1D23) ou padrão antigo (ABC1234 / ABC-1234).
# Aceita hífen opcional e normaliza para maiúsculas sem hífen.
_PLACA_REGEX = re.compile(r"^[A-Z]{3}-?\d[A-Z0-9]\d{2}$")


def _validar_placa(raw: str) -> str:
    """
    Normaliza e valida a placa do veículo.

    Aceita os formatos brasileiros antigo (ABC1234) e Mercosul (ABC1D23),
    com ou sem hífen, em qualquer caixa. Retorna a placa normalizada
    (maiúsculas, sem hífen).

    Raises:
        ValueError : se o formato não corresponder a uma placa válida.
    """
    placa = raw.strip().upper().replace("-", "")
    if not placa:
        raise ValueError("Placa do veículo é obrigatória.")
    # Reinsere o hífen na posição canônica só para validar o padrão
    candidato = f"{placa[:3]}-{placa[3:]}" if len(placa) == 7 else placa
    if not _PLACA_REGEX.match(candidato):
        raise ValueError(
            f"Placa '{raw}' inválida. Use o formato ABC1234 ou ABC1D23 (Mercosul)."
        )
    return placa


def _validar_hora(raw: str) -> int:
    """
    Valida e converte a hora de início (0–23).

    String vazia retorna a hora atual do sistema. Qualquer valor não inteiro
    ou fora do intervalo levanta ValueError, tratado pelo chamador como flash.

    Raises:
        ValueError : hora não inteira ou fora de [0, 23].
    """
    if not raw:
        return datetime.datetime.now().hour
    hora = int(raw)   # ValueError se não for inteiro
    if not (0 <= hora <= 23):
        raise ValueError("Hora de início deve estar entre 0 e 23.")
    return hora


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _atualizar_carregadores_livres() -> None:
    """
    Sincroniza o contador de carregadores livres, o flag 'disponivel' e
    o flag 'em_throttle' nos dados de postos com o estado real do SM.

    - disponivel=False quando todos os conectores estão ocupados (Lotado).
      O posto continua clicável — o template usa <a> em ambos os casos.
    - em_throttle=True quando há pelo menos uma sessão THROTTLED no posto,
      indicando que o controle dinâmico de carga está ativo.
    """
    chargers = sm.list_chargers()
    sessoes_ativas = {s.charger_id: s for s in sm.list_active()}

    for posto_id, posto in POSTOS.items():
        livres = sum(
            1 for cid, sid in chargers.items()
            if cid.startswith(posto_id) and sid is None
        )
        posto["carregadores_livres"] = livres
        posto["disponivel"] = livres > 0

        # Conector VIP (C5) disponível? Usado pelo filtro "Assinantes" no mapa.
        cid_vip = f"{posto_id}-{CONECTOR_VIP}"
        posto["vip_livre"] = chargers.get(cid_vip) is None

        # Verifica se alguma sessão do posto está em throttle
        posto["em_throttle"] = any(
            s.status == SessionStatus.THROTTLED
            for cid, s in sessoes_ativas.items()
            if cid.startswith(posto_id)
        )


# ---------------------------------------------------------------------------
# ── ROTAS DO SPRINT 1 (mantidas) ──────────────────────────────────────────
# ---------------------------------------------------------------------------

@app.route("/")
def mapa():
    """Página inicial: mapa com postos de recarga."""
    _atualizar_carregadores_livres()
    return render_template("mapa.html", postos=POSTOS)


@app.route("/posto/<posto_id>")
def posto(posto_id: str):
    """Carregadores do posto — agora reflete estado real do SessionManager."""
    if posto_id not in POSTOS:
        flash("Posto não encontrado!", "error")
        return redirect(url_for("mapa"))

    posto_info = POSTOS[posto_id]
    chargers_raw = sm.list_chargers()

    # #R5/#B34 — acumula energia das sessões deste posto sob o lock, evitando
    # corrida com finish_session concorrente (antes _simular_tick rodava solto).
    with _state_lock:
        for cid, sid in chargers_raw.items():
            if sid and cid.startswith(posto_id):
                sess = sm.get_session(sid)
                if sess and sess.is_active:
                    sm.accrue_energy(sid)

    # Monta estrutura de carregadores compatível com o template do Sprint 1
    carregadores = {}
    for cid, sid in chargers_raw.items():
        if not cid.startswith(posto_id):
            continue
        num = cid.split("-")[1]
        session = sm.get_session(sid) if sid else None

        carregadores[num] = {
            "id":            num,
            "nome":          f"Carregador {num}",
            "status":        "ocupado" if sid else "livre",
            "tipo":          "assinante" if num == "C5" else "publico",
            "usuario_atual": session.user_name if session else None,
            "tempo_restante": (
                f"{int(session.duration_minutes)} min"
                if session else None
            ),
            "session_id":    sid,
            "energia_kwh":   round(session.energy_kwh, 2) if session else None,
            "potencia_kw":   session.allocated_power_kw if session else None,
            # True quando este conector específico está com throttle ativo
            "em_throttle":   (
                session.status == SessionStatus.THROTTLED
                if session else False
            ),
        }

    # Flag global do posto: True se qualquer conector está throttled
    _atualizar_carregadores_livres()
    posto_em_throttle = posto_info.get("em_throttle", False)

    return render_template(
        "index.html",
        posto=posto_info,
        carregadores=carregadores,
        posto_em_throttle=posto_em_throttle,
        limite_posto_kw=LIMITE_POR_POSTO_KW,
    )


@app.route("/posto/<posto_id>/carregador/<carregador_id>")
def formulario(posto_id: str, carregador_id: str):
    """Formulário de sessão do Sprint 1 — redireciona ao dashboard Sprint 2."""
    if posto_id not in POSTOS:
        flash("Posto não encontrado!", "error")
        return redirect(url_for("mapa"))

    chargers = sm.list_chargers()
    cid_full = f"{posto_id}-{carregador_id}"

    if cid_full not in chargers:
        flash("Carregador inválido!", "error")
        return redirect(url_for("posto", posto_id=posto_id))

    if chargers[cid_full] is not None:
        flash(f"Carregador {carregador_id} está ocupado!", "error")
        return redirect(url_for("posto", posto_id=posto_id))

    return render_template(
        "formulario.html",
        posto=POSTOS[posto_id],
        carregador={"id": carregador_id, "nome": f"Carregador {carregador_id}",
                    "tipo": "publico"},
        posto_em_throttle=POSTOS[posto_id].get("em_throttle", False),
    )


@app.route("/sessao", methods=["POST"])
def processar():
    """
    Processa o formulário do Sprint 1 e cria sessão via SessionManager.
    Mantém compatibilidade com relatorio.html do Sprint 1 para sessão única.
    """
    try:
        posto_id      = request.form.get("posto_id", "P1")
        carregador_id = request.form.get("carregador_id", "C1")
        nome_usuario  = request.form.get("nome", "").strip() or "Usuário Anônimo"
        tipo_str      = request.form.get("tipo", "P").upper()
        hora          = int(request.form.get("hora", 9))
        minuto        = int(request.form.get("minuto", 0))
        duracao_min   = int(request.form.get("duracao", 30))

        if not (0 <= hora <= 23 and 0 <= minuto <= 59):
            raise ValueError("Horário inválido.")
        if not (5 <= duracao_min <= 240):
            raise ValueError("Duração deve ser entre 5 e 240 minutos.")

        user_type = USER_TYPE_MAP.get(tipo_str, UserType.STANDARD)
        cid_full  = f"{posto_id}-{carregador_id}"
        pm_posto  = _get_pm(posto_id)

        # Conector VIP (C5): exclusivo para assinantes.
        if _eh_conector_vip(cid_full) and user_type != UserType.SUBSCRIBER:
            flash(
                "O conector C5 é exclusivo para assinantes. "
                "Usuários Padrão e Corporativo devem usar os conectores C1 a C4.",
                "error",
            )
            return redirect(url_for("posto", posto_id=posto_id))

        # #B31 — valida a placa quando informada. O formulário do Sprint 1 permite
        # placa vazia (sessão de demonstração), nesse caso mantemos "N/A".
        placa_raw = request.form.get("placa", "").strip()
        vehicle_id = _validar_placa(placa_raw) if placa_raw else "N/A"

        # Cria sessão no SessionManager (protegido por lock)
        with _state_lock:
            session = sm.create_session(
                charger_id=cid_full,
                vehicle_id=vehicle_id,
                user_name=nome_usuario,
                user_type=user_type,
                requested_power_kw=11.0,
            )
            result = pm_posto.allocate(session)

            if result.rejected:
                sm.finish_session(session.session_id, status=SessionStatus.FAULTED)
                flash(result.message, "error")
                return redirect(url_for("posto", posto_id=posto_id))

            # #B28 — tarifa de demanda usa a ocupação do POSTO da sessão
            tariff = pe.calculate(
                user_type, hora=hora, minuto=minuto,
                occupancy_override=_posto_sms[posto_id].occupancy_ratio()
                if posto_id in _posto_sms else None,
            )
            sm.start_charging(session.session_id, result.granted_kw, tariff.tariff_kwh)
            if result.redistributed and result.granted_kw < 11.0:
                sm.throttle_session(session.session_id, result.granted_kw)
            mb.on_session_start(session)
            # #B24 — emite frames Modbus da redistribuição (ver dashboard_nova_sessao)
            if result.redistributed and result.throttle_events:
                mb.on_throttle(session, result)

        # Também roda a lógica do Sprint 1 para manter o relatório visual
        sessao_s1 = processar_sessao(
            nome_usuario=nome_usuario,
            tipo_usuario=tipo_str,
            hora_inicio=hora,
            minuto_inicio=minuto,
            duracao_min=duracao_min,
            carregador_id=carregador_id,
        )

        # Injeta dados do Sprint 2 no dicionário do Sprint 1
        sessao_s1["session_id_s2"]    = session.session_id
        sessao_s1["vehicle_id"]       = session.vehicle_id
        sessao_s1["potencia_alocada"] = result.granted_kw
        sessao_s1["tarifa_label"]     = pe.tariff_label(tariff)
        sessao_s1["throttle_msg"]     = result.message if result.redistributed else None
        sessao_s1["tipo_usuario_nome"] = user_type.name

        return render_template(
            "relatorio.html",
            sessao=sessao_s1,
            carregador={"id": carregador_id, "nome": f"Carregador {carregador_id}",
                        "tipo": "publico"},
            posto=POSTOS.get(posto_id, {}),
        )

    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("mapa"))
    except Exception as e:
        logger.exception("Erro inesperado em /sessao")
        flash(f"Erro: {e}", "error")
        return redirect(url_for("mapa"))


# ---------------------------------------------------------------------------
# ── ROTAS DO SPRINT 2 ──────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

@app.route("/dashboard")
def dashboard():
    """
    Painel central do Sprint 2.
    Exibe: sessões ativas, painel de potência, log Modbus, tarifas.
    """
    # #B34 — acumula energia sob o lock para evitar corrida com finish_session.
    with _state_lock:
        for s in sm.list_active():
            sm.accrue_energy(s.session_id)

    ativas = sm.list_active()
    disponíveis = sm.available_chargers()

    tariff_table_rows = []
    for ut in UserType:
        row = {"tipo": ut.name, "label": ut.value, "tarifas": {}}

        class _LowSM:
            def occupancy_ratio(self): return 0.0

        class _HighSM:
            def occupancy_ratio(self): return 0.85

        pe_low  = PricingEngine(_LowSM())
        pe_high = PricingEngine(_HighSM())

        row["tarifas"]["normal"] = pe_low.calculate(ut, hora=10).tariff_kwh
        row["tarifas"]["pico"]   = pe_low.calculate(ut, hora=19).tariff_kwh
        row["tarifas"]["pico_demanda"] = pe_high.calculate(ut, hora=19).tariff_kwh
        tariff_table_rows.append(row)

    # Dados de potência por posto — cada PM é isolado por instalação física
    NOMES_POSTOS = {
        "P1": "Paulista",
        "P2": "Faria Lima",
        "P3": "Berrini",
    }
    postos_power = []
    total_em_uso   = 0.0
    total_limite   = 0.0
    total_sessions = 0
    for pid in ["P1", "P2", "P3"]:
        pm_p = posto_pms[pid]
        em_uso = round(_posto_sms[pid].total_allocated_power_kw(), 2)
        limite = pm_p.limit_kw
        disponivel = round(limite - em_uso, 2)
        pct = round((em_uso / limite) * 100, 1) if limite > 0 else 0.0
        n_sessoes = _posto_sms[pid].active_count()
        postos_power.append({
            "id":         pid,
            "nome":       NOMES_POSTOS[pid],
            "em_uso_kw":  em_uso,
            "limite_kw":  limite,
            "disponivel_kw": disponivel,
            "ocupacao_pct": pct,
            "sessoes":    n_sessoes,
            "em_throttle": POSTOS[pid].get("em_throttle", False),
        })
        total_em_uso   += em_uso
        total_limite   += limite
        total_sessions += n_sessoes

    total_em_uso  = round(total_em_uso, 2)
    total_limite  = round(total_limite, 2)
    total_disponivel_kw = round(total_limite - total_em_uso, 2)
    total_pct = round((total_em_uso / total_limite) * 100, 1) if total_limite > 0 else 0.0

    # Histórico consolidado de todos os PowerManagers (últimas 5 decisões)
    historico_consolidado = []
    for pid in ["P1", "P2", "P3"]:
        historico_consolidado.extend(posto_pms[pid].history)
    historico_consolidado.sort(key=lambda r: r.message)  # estável sem timestamp; Sprint 3 adiciona ts

    # Detalhamento por posto: sessões ativas e decisões de potência agrupadas,
    # para que o painel mostre claramente o que acontece em cada instalação.
    postos_detalhe = []
    for pid in ["P1", "P2", "P3"]:
        sessoes_posto = sorted(
            _posto_sms[pid].list_active(),
            key=lambda s: s.charger_id,
        )
        decisoes_posto = list(posto_pms[pid].history)[-5:]
        postos_detalhe.append({
            "id":         pid,
            "nome":       NOMES_POSTOS[pid],
            "sessoes":    sessoes_posto,
            "decisoes":   decisoes_posto,
            "em_uso_kw":  round(_posto_sms[pid].total_allocated_power_kw(), 2),
            "limite_kw":  posto_pms[pid].limit_kw,
            "ocupacao_pct": round(
                (_posto_sms[pid].total_allocated_power_kw() / posto_pms[pid].limit_kw) * 100, 1
            ) if posto_pms[pid].limit_kw > 0 else 0.0,
            "em_throttle": POSTOS[pid].get("em_throttle", False),
        })

    return render_template(
        "dashboard.html",
        sessoes_ativas=ativas,
        disponiveis=disponíveis[:8],
        total_disponivel=len(disponíveis),
        # Agregado global (soma coerente dos 3 postos)
        potencia_em_uso=total_em_uso,
        potencia_limite=total_limite,
        ocupacao_pct=total_pct,
        available_kw=total_disponivel_kw,
        total_sessoes=total_sessions,
        # Por posto (para os mini-cards)
        postos_power=postos_power,
        # Detalhamento por posto (sessões + decisões agrupadas)
        postos_detalhe=postos_detalhe,
        historico_pm=historico_consolidado[-5:],
        frames_modbus=mb.get_log()[-10:],
        total_frames=mb.frame_count(),
        tariff_rows=tariff_table_rows,
        ocupacao_rede=round(sm.occupancy_ratio() * 100, 1),
    )


@app.route("/dashboard/nova-sessao", methods=["POST"])
def dashboard_nova_sessao():
    """Cria sessão via formulário do dashboard."""
    try:
        charger_id = request.form.get("charger_id", "").strip()
        vehicle_id = request.form.get("vehicle_id", "").strip().upper()
        user_name  = request.form.get("user_name", "").strip() or "Usuário Anônimo"
        tipo_str   = request.form.get("user_type", "P").upper()
        hora_str   = request.form.get("hora", "")
        pot_str    = request.form.get("potencia", "11.0")

        if not charger_id:
            flash("Selecione um carregador.", "error")
            return redirect(url_for("dashboard"))

        # #B31 — valida e normaliza a placa (formato BR antigo ou Mercosul)
        vehicle_id = _validar_placa(vehicle_id)

        # #B30 — valida a hora de início (0–23); vazio = hora atual
        hora      = _validar_hora(hora_str)
        potencia  = float(pot_str) if pot_str else 11.0
        user_type = USER_TYPE_MAP.get(tipo_str, UserType.STANDARD)
        # Extrai o posto_id do charger_id (ex: "P1-C3" → "P1")
        posto_id  = charger_id.split("-")[0] if "-" in charger_id else "P1"
        pm_posto  = _get_pm(posto_id)

        # Conector VIP (C5): exclusivo para assinantes. Usuários Padrão e
        # Corporativo devem usar os conectores públicos (C1–C4).
        if _eh_conector_vip(charger_id) and user_type != UserType.SUBSCRIBER:
            flash(
                "O conector C5 é exclusivo para assinantes. "
                "Usuários Padrão e Corporativo devem usar os conectores C1 a C4.",
                "error",
            )
            return redirect(url_for("dashboard"))

        with _state_lock:
            session = sm.create_session(charger_id, vehicle_id, user_name,
                                        user_type, potencia)
            result  = pm_posto.allocate(session)

            if result.rejected:
                sm.finish_session(session.session_id, status=SessionStatus.FAULTED)
                flash(result.message, "error")
                return redirect(url_for("dashboard"))

            # #B28 — tarifa de demanda usa a ocupação do POSTO da sessão
            tariff = pe.calculate(
                user_type, hora=hora,
                occupancy_override=_posto_sms[posto_id].occupancy_ratio()
                if posto_id in _posto_sms else None,
            )
            sm.start_charging(session.session_id, result.granted_kw, tariff.tariff_kwh)
            # Se a potência concedida é menor que a solicitada (redistribuição),
            # o novo conector também entra como THROTTLED — não só os existentes.
            if result.redistributed and result.granted_kw < potencia:
                sm.throttle_session(session.session_id, result.granted_kw)
            mb.on_session_start(session)
            # #B24 — emite os frames Modbus de redistribuição (reescritas do
            # registrador 10029 nas sessões existentes que foram throttled).
            # Sem isso, a redistribuição não aparecia no log do protocolo.
            if result.redistributed and result.throttle_events:
                mb.on_throttle(session, result)

        flash(
            f"✅ Sessão {session.session_id} iniciada — "
            f"{result.granted_kw:.1f} kW | {pe.tariff_label(tariff)}",
            "success",
        )

        if result.redistributed:
            flash(
                f"⚠️ Redistribuição automática: {result.message}",
                "warning",
            )

    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:
        logger.exception("Erro em /dashboard/nova-sessao")
        flash(f"Erro inesperado: {e}", "error")

    return redirect(url_for("dashboard"))


@app.route("/dashboard/encerrar", methods=["POST"])
def dashboard_encerrar():
    """Encerra sessão e rebalanceia potência."""
    session_id = request.form.get("session_id", "").strip()
    if not session_id:
        flash("ID de sessão inválido.", "error")
        return redirect(url_for("dashboard"))

    try:
        session = sm.get_session(session_id)
        if not session:
            flash(f"Sessão {session_id} não encontrada.", "error")
            return redirect(url_for("dashboard"))

        posto_id = session.charger_id.split("-")[0]
        pm_posto = _get_pm(posto_id)

        with _state_lock:
            # #B33 — finish_session já chama accrue_energy internamente; o
            # _simular_tick anterior era redundante e foi removido.
            sm.finish_session(session_id)
            mb.on_session_end(session)
            rb = pm_posto.rebalance()
            # #B34 — emite o Modbus de rebalanceamento ainda dentro do lock,
            # mantendo a ordem dos frames consistente com o estado das sessões.
            if rb:
                mb.on_rebalance(rb)

        if rb:
            flash(f"🔄 {rb.message}", "info")

        flash(
            f"✅ Sessão {session_id} encerrada — "
            f"{session.energy_kwh:.3f} kWh | R$ {session.total_cost_brl:.2f}",
            "success",
        )
    except Exception as e:
        logger.exception("Erro em /dashboard/encerrar")
        flash(f"Erro: {e}", "error")

    return redirect(url_for("dashboard"))


@app.route("/api/status")
def api_status():
    """
    Endpoint JSON para polling do dashboard (atualização em tempo real).
    Chamado a cada 5 segundos pelo JavaScript do dashboard.
    """
    # #B34 — acumula energia das sessões ativas sob o lock, evitando corrida
    # com finish_session concorrente em modo multi-thread.
    # #B25 — emite a leitura de medidores (MeterRead) de cada sessão ativa, de
    # modo que os registradores Modbus 10015 (potência) e 10016 (energia)
    # reflitam o estado em tempo real durante o polling — antes só atualizavam
    # no início e no fim da sessão.
    with _state_lock:
        ativas = sm.list_active()
        for s in ativas:
            sm.accrue_energy(s.session_id)
            mb.on_meter_read(s)

    sessoes_json = []
    for s in ativas:
        sessoes_json.append({
            "session_id":       s.session_id,
            "charger_id":       s.charger_id,
            "user_name":        s.user_name,
            "user_type":        s.user_type.value,
            "vehicle_id":       s.vehicle_id,
            "status":           s.status.value,
            "allocated_kw":     s.allocated_power_kw,
            "energy_kwh":       round(s.energy_kwh, 3),
            "cost_brl":         round(s.total_cost_brl, 2),
            "tariff_kwh":       round(s.tariff_kwh, 4),
            "duration_min":     round(s.duration_minutes, 1),
        })

    # Agrega dados por posto para o polling do dashboard
    postos_status = {}
    total_em_uso_api  = 0.0
    total_limite_api  = 0.0
    for pid in ["P1", "P2", "P3"]:
        pm_p   = posto_pms[pid]
        em_uso = round(_posto_sms[pid].total_allocated_power_kw(), 2)
        limite = pm_p.limit_kw
        postos_status[pid] = {
            "em_uso_kw":     em_uso,
            "limite_kw":     limite,
            "disponivel_kw": round(limite - em_uso, 2),
            "ocupacao_pct":  round((em_uso / limite) * 100, 1) if limite > 0 else 0.0,
            "sessoes":       _posto_sms[pid].active_count(),
        }
        total_em_uso_api += em_uso
        total_limite_api += limite

    total_em_uso_api  = round(total_em_uso_api, 2)
    total_limite_api  = round(total_limite_api, 2)
    total_pct_api     = round((total_em_uso_api / total_limite_api) * 100, 1) if total_limite_api > 0 else 0.0

    return jsonify({
        "sessoes_ativas":   sessoes_json,
        "total_ativas":     sm.active_count(),
        "potencia_em_uso":  total_em_uso_api,
        "potencia_limite":  total_limite_api,
        "ocupacao_pct":     total_pct_api,
        "available_kw":     round(total_limite_api - total_em_uso_api, 2),
        "postos":           postos_status,
        "total_frames":     mb.frame_count(),
        "timestamp":        datetime.datetime.now().strftime("%H:%M:%S"),
    })


@app.route("/relatorio")
def relatorio_consolidado():
    """Relatório completo de todas as sessões (ativas e encerradas)."""
    # #B34 — acumula energia sob o lock para evitar corrida com finish_session.
    with _state_lock:
        for s in sm.list_active():
            sm.accrue_energy(s.session_id)

    todas  = sm.list_all()

    # #B35 — separa receita realizada (sessões encerradas, custo final) de
    # receita projetada (sessões ativas, custo parcial que ainda cresce).
    encerradas = [s for s in todas if not s.is_active]
    ativas_lst = [s for s in todas if s.is_active]
    receita_realizada = round(sum(s.total_cost_brl for s in encerradas), 2)
    receita_projetada = round(sum(s.total_cost_brl for s in ativas_lst), 2)

    # 'decisoes' soma o histórico dos 3 PowerManagers (antes só contava P1).
    total_decisoes = sum(len(posto_pms[pid].history) for pid in ["P1", "P2", "P3"])

    totais = {
        "sessoes":            len(todas),
        "ativas":             sm.active_count(),
        "energia":            round(sum(s.energy_kwh for s in todas), 3),
        # 'receita' mantida para compatibilidade com o template = total geral
        "receita":            round(receita_realizada + receita_projetada, 2),
        "receita_realizada":  receita_realizada,
        "receita_projetada":  receita_projetada,
        "frames":             mb.frame_count(),
        "decisoes":           total_decisoes,
    }

    historico_pm_todos = []
    for pid in ["P1", "P2", "P3"]:
        historico_pm_todos.extend(posto_pms[pid].history)

    return render_template(
        "relatorio_consolidado.html",
        sessoes=todas,
        totais=totais,
        historico_pm=historico_pm_todos,
        reg_snapshot=mb.register_snapshot(),
    )


@app.route("/modbus-log")
def modbus_log():
    """Página dedicada ao log de frames Modbus TCP."""
    return render_template(
        "modbus_log.html",
        frames=mb.get_log(),
        summary=mb.print_summary(),
        total_frames=mb.frame_count(),
        reg_snapshot=mb.register_snapshot(),
    )


@app.route("/testes")
def testes():
    """Página de execução de testes automatizados no navegador."""
    return render_template("testes.html")


@app.route("/api/testes/run", methods=["POST"])
def api_testes_run():
    """
    Executa os testes automatizados do pytest e retorna o resultado como JSON.
    Permite rodar os testes diretamente no navegador sem precisar do terminal.
    """
    import subprocess
    import sys

    modulo = request.json.get("modulo", "") if request.is_json else ""
    cmd = [
        sys.executable, "-m", "pytest",
        "-v", "--tb=short", "--no-header",
        "--color=no",
    ]
    if not modulo:
        # Sem filtro: roda o arquivo inteiro.
        cmd.append("test_chargegrid.py")
    elif " or " in modulo:
        # Expressão -k (várias classes agrupadas): roda o arquivo filtrando por -k.
        cmd.extend(["test_chargegrid.py", "-k", modulo])
    else:
        # Classe única: usa a sintaxe ::Classe (sem o arquivo base, que anularia
        # o filtro e coletaria todos os testes).
        cmd.append(f"test_chargegrid.py::{modulo}")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        output   = proc.stdout + proc.stderr
        passed   = output.count(" PASSED")
        failed   = output.count(" FAILED")
        errors   = output.count(" ERROR")
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        output     = "Timeout: os testes demoraram mais de 60 segundos."
        passed     = failed = errors = 0
        returncode = -1
    except Exception as e:
        output     = f"Erro ao executar pytest: {e}"
        passed     = failed = errors = 0
        returncode = -1

    return jsonify({
        "output":     output,
        "passed":     passed,
        "failed":     failed,
        "errors":     errors,
        "returncode": returncode,
        "ok":         returncode == 0,
    })


# ---------------------------------------------------------------------------
# Seed de demonstração — popula postos com sessões já em andamento
# ---------------------------------------------------------------------------

def _seed_demo() -> None:
    """
    Pré-popula o estado com sessões de demonstração, para que os cenários de
    ocupação e throttling apareçam imediatamente no mapa e no dashboard, sem
    precisar criá-los manualmente.

    Cenários montados:
      • Berrini (P3): 5 conectores ocupados → posto LOTADO e em THROTTLE.
          C5 (VIP) = assinante (Yan); C3 = corporativo (Felipe); demais = padrão.
      • Paulista (P1): 4 conectores (C1–C4) carregando, C5 (VIP) livre.
          4×11 kW excede o limite de 33 kW → cenário de THROTTLE pré-montado.
          Composição: 2 padrão, 1 corporativo (Allan) e 1 assinante (Amanda).

    Cada sessão recebe um tempo de início recuado (alguns minutos atrás), de
    modo que já exibam minutos de recarga e energia acumulada. A função é
    idempotente: se já houver sessões ativas, não faz nada (evita duplicar
    em reloads do Flask no modo debug).
    """
    if sm.active_count() > 0:
        return

    import random

    def _placa_aleatoria() -> str:
        """Gera uma placa no padrão Mercosul (ABC1D23)."""
        letras = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        return (
            "".join(random.choice(letras) for _ in range(3))
            + str(random.randint(0, 9))
            + random.choice(letras)
            + f"{random.randint(0, 99):02d}"
        )

    # (charger, nome, tipo, minutos já carregando)
    plano = [
        # Berrini — 5 conectores: lota e entra em throttle
        ("P3-C1", "Patrick",  UserType.STANDARD,   18),
        ("P3-C2", "Carolina", UserType.STANDARD,   14),
        ("P3-C3", "Felipe",   UserType.CORPORATE,  11),
        ("P3-C4", "Henrique", UserType.STANDARD,    7),
        ("P3-C5", "Yan",      UserType.SUBSCRIBER,  4),   # VIP → assinante
        # Paulista — 4 conectores (C1–C4) carregando; C5 (VIP) fica LIVRE.
        # 4×11 kW projetado excede 33 kW → cenário de throttle pré-montado.
        # Composição: 2 comuns + 1 corporativo + 1 assinante.
        ("P1-C1", "Damaceno", UserType.STANDARD,   22),
        ("P1-C2", "Caio",     UserType.STANDARD,   16),
        ("P1-C3", "Allan",    UserType.CORPORATE,   9),
        ("P1-C4", "Amanda",   UserType.SUBSCRIBER,  5),
    ]

    agora = datetime.datetime.now()

    for charger_id, nome, tipo, minutos in plano:
        posto_id = charger_id.split("-")[0]
        pm_posto = _get_pm(posto_id)
        try:
            with _state_lock:
                session = sm.create_session(
                    charger_id, _placa_aleatoria(), nome, tipo, 11.0
                )
                result = pm_posto.allocate(session)
                if result.rejected:
                    sm.finish_session(session.session_id,
                                      status=SessionStatus.FAULTED)
                    continue

                tariff = pe.calculate(
                    tipo,
                    occupancy_override=_posto_sms[posto_id].occupancy_ratio(),
                )
                sm.start_charging(session.session_id,
                                  result.granted_kw, tariff.tariff_kwh)
                if result.redistributed and result.granted_kw < 11.0:
                    sm.throttle_session(session.session_id, result.granted_kw)
                mb.on_session_start(session)
                if result.redistributed and result.throttle_events:
                    mb.on_throttle(session, result)

                # Recua o relógio da sessão para simular tempo já decorrido,
                # depois acumula a energia correspondente a esse intervalo.
                inicio = agora - datetime.timedelta(minutes=minutos)
                session.start_time         = inicio
                session.last_energy_update = inicio
                sm.accrue_energy(session.session_id)
        except ValueError as exc:
            logger.warning("Seed de demonstração ignorou %s: %s", charger_id, exc)

    _atualizar_carregadores_livres()
    logger.info("Seed de demonstração aplicado: %d sessões ativas.",
                sm.active_count())


# Aplica o seed ao importar o módulo (vale tanto para `python app.py`
# quanto para servidores WSGI que importam `app`). Não roda sob pytest,
# para não poluir o estado esperado pelos testes.
import sys as _sys
if "pytest" not in _sys.modules:
    _seed_demo()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import socket

    # Descobre o IP local da máquina para exibir no terminal
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip_local = s.getsockname()[0]
        s.close()
    except Exception:
        ip_local = "127.0.0.1"

    porta = 5001
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║      ⚡ ChargeGrid Intelligence — GoodWe HCA G2 ⚡      ║")
    print("║      Sprint 2 | FIAP + GoodWe EV Challenge 2026         ║")
    print("╠══════════════════════════════════════════════════════════╣")
    print(f"║  🌐 Local :  http://localhost:{porta}                       ║")
    print(f"║  🌐 Rede  :  http://{ip_local}:{porta}{'':>{30 - len(ip_local)}}║")
    print("║  📋 Rotas :  / · /dashboard · /testes · /modbus-log     ║")
    print("║  🛑 Parar :  Ctrl+C                                      ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    app.run(debug=True, host="0.0.0.0", port=porta)
