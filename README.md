# Monitor de stock — Pokémon 30th Celebration

Verifica 64 lojas a cada ~10 minutos e envia push para o telemóvel quando um
produto 30th Celebration / 30th Anniversary passa a ter stock. Corre de graça
no GitHub — não precisas de ter o computador ligado.

## Instalação (≈10 minutos, uma vez)

### 1. App de notificações
1. Instala a app **ntfy** (Android / iPhone).
2. Toca em **+** e subscreve um tópico com um nome difícil de adivinhar,
   por exemplo `gabriel-30th-x7k2q`. (Quem souber o nome recebe os mesmos avisos.)
3. No iPhone, permite notificações e, se quiseres, ativa "Instant delivery".

### 2. Repositório no GitHub
1. Cria conta em github.com (se não tiveres).
2. **New repository** → nome `stock-30th` → **Public** (repositórios públicos
   têm minutos ilimitados de GitHub Actions; o teu tópico fica secreto à parte).
3. **Add file → Upload files** e arrasta *todo o conteúdo* do zip, incluindo a
   pasta `.github` (no Mac, `Cmd+Shift+.` mostra pastas ocultas). Commit.

### 3. Segredo com o teu tópico
**Settings → Secrets and variables → Actions → New repository secret**
- Nome: `NTFY_TOPIC` — Valor: o tópico do passo 1 (ex.: `gabriel-30th-x7k2q`)

### 4. Testar
**Actions** → *Monitor 30th Celebration* → **Run workflow** → marca
*"Enviar só uma notificação de teste"* → Run. Deve chegar um push em segundos.

Depois corre de novo **sem** marcar o teste. Na 1.ª execução recebes um resumo
do que já tem stock; daí em diante, só avisos de novidades.

## Ver o que está a acontecer
O ficheiro `report.md` no repositório é atualizado a cada execução: mostra,
loja a loja, que método funcionou, quantos produtos 30th encontrou, quantos
têm stock, e erros (ex.: loja que bloqueia robôs).

## Como funciona
Para cada loja deteta a plataforma e usa a fonte mais fiável:
- **Shopify** (a maioria das lojas TCG) — dados oficiais de stock da loja.
- **WooCommerce** — API pública de produtos (`is_in_stock`).
- **PrestaShop** — pesquisa interna em JSON.
- **Outras** (Continente, El Corte Inglés, Toys"R"Us…) — lê a página de
  pesquisa e procura "esgotado / adicionar ao carrinho". Menos fiável e algumas
  destas lojas bloqueiam acessos automáticos.

Pré-vendas/pré-reservas contam como "disponível" — normalmente é o que queres
apanhar. Cartas avulsas, sleeves, binders e graded são ignoradas para não haver
spam.

## Ajustes
- **Frequência**: em `.github/workflows/monitor.yml`, linha `cron`
  (`*/5 * * * *` = 5 min, o mínimo do GitHub).
- **Lojas**: edita `stores.csv`.
- **Palavras / exclusões**: topo de `monitor.py` (`MATCH_PATTERNS`, `EXCLUDE_PATTERNS`).
- **Telegram em vez de/além de ntfy**: adiciona os segredos
  `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`.

## Notas
- O GitHub atrasa por vezes os agendamentos alguns minutos em horas de pico.
- Se o repositório ficar 60 dias sem atividade, o GitHub pausa o agendamento;
  aqui isso não acontece porque o monitor faz commit do estado.
