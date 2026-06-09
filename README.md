# ⚡ ChargeGrid Intelligence

> Sistema Inteligente de Gerenciamento de Recarga para eletropostos comerciais
> **EV Challenge 2026 — Parceria FIAP + GoodWe · Sprint 2**

ChargeGrid Intelligence é uma solução de software para o ecossistema de mobilidade
elétrica. Resolve um problema concreto do segmento comercial e de varejo de
eletropostos: a ausência de mecanismos integrados para **orquestrar potência
elétrica**, **registrar ciclos de recarga**, **faturar sessões** e **comunicar
informações** ao usuário e à plataforma de gestão.

O hardware de referência é o carregador AC **GoodWe GW11K-HCA-20** (11 kW, linha
HCA G2), e a integração é simulada via protocolo industrial **Modbus TCP**.

---

## ✨ Funcionalidades

- **Gerenciamento de múltiplas sessões** — controle de ciclo de vida isolado por
  posto (criação, recarga, throttle, encerramento), com até 15 sessões simultâneas.
- **Controle inteligente de demanda** — limite de potência por instalação (33 kW),
  com throttle automático e redistribuição proporcional **por prioridade de tipo
  de usuário** (Assinante > Corporativo > Padrão).
- **Tarifação dinâmica de 3 eixos** — horário de pico, ocupação da rede e tipo de
  usuário, com taxa mínima por sessão.
- **Simulação Modbus TCP** — frames TX/RX com os registradores reais do GoodWe
  HCA G2, log em buffer circular.
- **Mapa interativo** — Leaflet + OpenStreetMap com os postos em coordenadas reais
  de São Paulo, marcadores coloridos por status e filtros.
- **Dashboard em tempo real** — potência por posto, sessões ativas e tarifas, com
  atualização automática (sem recarregar a página).
- **Relatórios** — consolidado de sessões com receita realizada e projetada, e
  snapshot dos registradores Modbus.
- **Conector VIP** — o conector C5 de cada posto é exclusivo para assinantes.
- **Suíte de testes integrada** — 61 testes automatizados executáveis pelo navegador.

---

## 🏗️ Arquitetura

Arquitetura modular em camadas, sem dependências circulares. Cada módulo tem uma
responsabilidade única:

```
models.py            Entidades e enums (ChargingSession, SessionStatus, UserType)
   ↑
session_manager.py   Ciclo de vida das sessões e acúmulo de energia
   ↑
power_manager.py     Controle de demanda: limite, throttle, rebalanceamento
   ↑
modbus_simulator.py  Simulação do protocolo Modbus TCP (registradores HCA G2)

pricing_engine.py    Tarifação dinâmica (depende apenas de models)
logica_recarga.py    Lógica de simulação do Sprint 1
   ↑
app.py               Camada web Flask — 12 rotas, orquestra os módulos
templates/           9 telas HTML (interface web)
test_chargegrid.py   61 testes automatizados
```

A camada de domínio (regras de negócio) é totalmente independente da camada web,
o que facilita a evolução futura (persistência em banco, hardware real, etc.) sem
reescrever as regras já validadas.

---

## 🚀 Como executar

**Pré-requisitos:** Python 3.10+ e duas bibliotecas.

```bash
# 1. Instalar as dependências
pip install flask pytest

# 2. Iniciar a aplicação
python app.py

# 3. Acessar no navegador
#    http://localhost:5001
```

Ao iniciar, o sistema já vem com **postos pré-populados** para demonstração
(Berrini lotado e em throttle; Paulista com 4 sessões e o conector de assinante
livre), permitindo visualizar imediatamente os cenários de ocupação e throttling.

> **Nota:** o mapa interativo usa tiles do OpenStreetMap e precisa de conexão com
> a internet. Sem conexão, um aviso amigável é exibido e a navegação pela lista
> de postos continua funcionando normalmente.

---

## 🧪 Testes

```bash
# Rodar todos os testes
pytest test_chargegrid.py -v

# Ou execute pela interface, na rota /testes
```

A suíte tem **61 testes** organizados em 10 classes:

| Suíte | Casos | Cobertura |
|-------|:-----:|-----------|
| PricingEngine            | 13 | Os três eixos de tarifa e fronteiras de horário |
| PowerManager · Alocação  |  6 | Limites, throttle e recusa física |
| PowerManager · Rebalance |  4 | Restauração e prioridade por tipo |
| SessionManager           |  8 | Energia em tempo real, idempotência, taxa mínima |
| Flask Routes             |  8 | Todas as rotas, incluindo posto lotado |
| Regressão e Auditoria    | 18 | Não-reincidência dos bugs corrigidos |
| Conector VIP             |  4 | Exclusividade do conector C5 |

---

## ⚙️ Regras de negócio

### Controle de potência
- Limite por posto: **33 kW** (3 conectores a 11 kW cabem; o 4º dispara throttle).
- Piso por conector: **4,2 kW**.
- Pesos de prioridade na redistribuição: Padrão **1,00**, Corporativo **1,10**,
  Assinante **1,20**.

### Tarifação (R$/kWh)
| Componente | Fator | Condição |
|------------|:-----:|----------|
| Tarifa base        | R$ 1,20 | sempre |
| Horário de pico    | × 1,50  | 18h–22h59 |
| Alta demanda       | × 1,30  | ocupação do posto ≥ 70% |
| Desconto assinante | − 15%   | tipo Assinante |
| Desconto corporativo | − 10% | tipo Corporativo |
| Taxa mínima        | R$ 2,00 | piso por sessão |

---

## 🔌 Integração Modbus (registradores HCA G2)

| Registrador | Função | Acesso |
|:-----------:|--------|:------:|
| 10017 | Status da estação | Leitura |
| 10015 | Potência de recarga (÷10 = kW) | Leitura |
| 10016 | Energia da sessão (÷10 = kWh) | Leitura |
| 10029 | Potência máxima de recarga | Leitura/Escrita |
| 10025 | Gestão dinâmica de carga | Leitura/Escrita |
| 10060 | Liga/desliga recarga | Leitura/Escrita |
| 10026 | Corrente do disjuntor (A) | Leitura/Escrita |

---

## 📁 Estrutura do projeto

```
chargeGrid_sprint2/
├── app.py                  Aplicação Flask (ponto de entrada)
├── models.py               Entidades e enums
├── session_manager.py      Ciclo de vida das sessões
├── power_manager.py        Controle de demanda de potência
├── pricing_engine.py       Tarifação dinâmica
├── modbus_simulator.py     Simulação do protocolo Modbus TCP
├── logica_recarga.py       Lógica de simulação do Sprint 1
├── test_chargegrid.py      61 testes automatizados
├── templates/              9 telas HTML
└── README.md
```

---

## 🛠️ Stack

- **Backend:** Python 3 · Flask
- **Frontend:** HTML5 · CSS3 · JavaScript (vanilla)
- **Mapa:** Leaflet + OpenStreetMap
- **Testes:** pytest
- **Protocolo:** Modbus TCP (simulado)

---

## 👥 Equipe

Projeto desenvolvido para o **EV Challenge 2026** (FIAP + GoodWe).

| Nome | RM |
|------|----|
| Giovanne Gomes Petenuci | 574091 |
| Arthur Vettorazzo de Souza | 569445 |
| Gustavo Zibini Belizario | 561376 |
| Alan Junio Araujo de Souza | 574112 |
| Brayan Barbosa Dos Santos | 573682 |
| Luiz Otávio Brito Freixo | 569977 |

---

## 📄 Licença

Projeto acadêmico desenvolvido no contexto do EV Challenge 2026 (FIAP + GoodWe).
