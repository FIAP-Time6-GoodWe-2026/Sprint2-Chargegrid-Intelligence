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

A rede simulada tem **3 postos** (Paulista, Faria Lima e Berrini), cada um com
**5 conectores** e limite de instalação de **33 kW** — capacidade total para 15
sessões simultâneas e 99 kW gerenciados.

---

## ✨ Funcionalidades

- **Gerenciamento de múltiplas sessões** — controle de ciclo de vida isolado por
  posto (criação, recarga, throttle, encerramento), com até 15 sessões simultâneas.
- **Controle inteligente de demanda** — limite de potência por instalação (33 kW),
  com throttle automático e redistribuição proporcional **por prioridade de tipo
  de usuário** (Assinante > Corporativo > Padrão).
- **Tarifação dinâmica de 3 eixos** — horário de pico, ocupação do posto e tipo de
  usuário, com taxa mínima por sessão.
- **Simulação Modbus TCP** — frames TX/RX com os registradores reais do GoodWe
  HCA G2, log em buffer circular.
- **Mapa interativo** — Leaflet + OpenStreetMap com os postos em coordenadas reais
  de São Paulo, marcadores coloridos por status e filtros (incluindo filtro de
  conector de assinante livre).
- **Dashboard em tempo real** — sessões e decisões de potência **agrupadas por
  posto**, painel de potência agregado e por posto, com atualização automática
  (minutos, energia e custo sobem sozinhos, sem recarregar a página).
- **Relatórios** — consolidado de sessões com receita realizada e projetada, e
  snapshot dos registradores Modbus.
- **Conector VIP** — o conector C5 de cada posto é exclusivo para assinantes;
  usuários Padrão e Corporativo são bloqueados nele.
- **Ambiente de demonstração** — ao iniciar, o sistema já popula cenários prontos
  de ocupação e throttling (ver seção abaixo).
- **Interface em português** com modo claro/escuro.
- **Suíte de testes integrada** — 61 testes automatizados executáveis pelo navegador.

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

> **Nota:** o mapa interativo usa tiles do OpenStreetMap e precisa de conexão com
> a internet. Sem conexão, um aviso é exibido e a navegação pela lista de postos
> continua funcionando normalmente.

---

## 🎬 Ambiente de demonstração (estado inicial)

Para facilitar a avaliação, o sistema **já inicia com cenários montados**, sem
necessidade de cadastro manual. O seed roda na inicialização (e é desativado
automaticamente durante os testes, para não interferir neles):

- **Berrini (P3)** — os **5 conectores ocupados** → posto **lotado e em throttle**.
  Patrick, Carolina e Henrique (Padrão), Felipe (Corporativo, C3) e Yan
  (Assinante, no conector VIP C5). Demonstra o controle de demanda e a
  **prioridade por tipo**: o assinante recebe a maior fatia de potência.
- **Paulista (P1)** — **4 conectores ocupados (C1–C4)**, com o conector de
  assinante (C5) **livre**. Composição: 2 Padrão, 1 Corporativo e 1 Assinante.
  Como 4 × 11 kW excede os 33 kW, o posto também demonstra **throttling**.
- **Faria Lima (P2)** — **vazio**, disponível para criar sessões ao vivo durante
  a demonstração.

As placas são geradas aleatoriamente no padrão Mercosul, e cada sessão inicia com
alguns minutos de recarga já acumulados.

---

## 🖥️ Telas e navegação

| Tela | Rota | O que mostra |
|------|------|--------------|
| **Mapa** | `/` | Mapa real de SP com os 3 postos; marcadores coloridos por status (verde = disponível, laranja = throttle, vermelho = lotado); lista lateral com distância e vagas; filtros (só disponíveis, por distância, assinantes) |
| **Posto** | `/posto/<id>` | Resumo do posto (disponíveis, ocupados, capacidade de 33 kW) e os 5 conectores, com quem está carregando e em que potência |
| **Carregador** | `/posto/<id>/carregador/<cid>` | Formulário de sessão do Sprint 1 |
| **Dashboard** | `/dashboard` | Painel de potência (agregado + por posto); sessões ativas e decisões de potência **agrupadas por posto**; formulário de nova sessão; tabela de tarifas dinâmicas; log Modbus recente |
| **Relatório** | `/relatorio` | Tabela de todas as sessões; totais; receita **realizada vs. projetada** |
| **Log Modbus** | `/modbus-log` | Frames TX/RX do protocolo, com registrador, função e ADU em hexadecimal |
| **Testes** | `/testes` | Execução dos 61 testes pelo navegador, por suíte |

Também existe a API interna `GET /api/status` (estado em JSON, usada pela
atualização em tempo real) e `POST /api/testes/run` (executa as suítes de teste).

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

Para desacoplar o `PowerManager` do `SessionManager`, é usado um **Protocol**
(PEP 544, `SessionStore`): o controle de potência depende de uma interface
estrutural, não da implementação concreta. O código passa sem erros nem avisos no
**Pyright**.

---

## ⚙️ Regras de negócio

### Controle de potência
- Limite por posto: **33 kW** (3 conectores a 11 kW cabem; o 4º dispara throttle).
- Piso por conector: **4,2 kW**.
- Pesos de prioridade na redistribuição: Padrão **1,00**, Corporativo **1,10**,
  Assinante **1,20**. Quando o limite é excedido, a potência é redistribuída entre
  **todas** as sessões (incluindo a que está entrando) na proporção dos pesos.

### Tarifação (R$/kWh)
| Componente | Fator | Condição |
|------------|:-----:|----------|
| Tarifa base        | R$ 1,20 | sempre |
| Horário de pico    | × 1,50  | 18h–22h59 |
| Alta demanda       | × 1,30  | ocupação do posto ≥ 70% |
| Desconto assinante | − 15%   | tipo Assinante |
| Desconto corporativo | − 10% | tipo Corporativo |
| Taxa mínima        | R$ 2,00 | piso por sessão |

A tarifa é fixada no instante da conexão e permanece constante durante a sessão
(equivalente a uma tarifa contratada). O eixo de demanda usa a ocupação do
**posto** específico, não a média global da rede.

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

Eventos que geram frames: início de sessão, throttle, leitura periódica de
medidores, rebalanceamento e encerramento. O log é um buffer circular (máx. 500
frames em memória), com contador acumulado para a métrica histórica total.

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

Robustez também coberta: validação de placa (BR antigo e Mercosul) e de horário,
proteção contra dupla ocupação de conector, idempotência de encerramento, e
segurança em concorrência (`threading.Lock`).

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
├── templates/              9 telas HTML (interface web)
└── README.md
```

---

## 🛠️ Stack

- **Backend:** Python 3 · Flask
- **Frontend:** HTML5 · CSS3 · JavaScript (vanilla)
- **Mapa:** Leaflet + OpenStreetMap
- **Testes:** pytest (61 casos) · type checking com Pyright
- **Protocolo:** Modbus TCP (simulado, registradores reais HCA G2)

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
