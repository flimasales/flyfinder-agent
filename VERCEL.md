# Deploy na Vercel (página com botão “Reexecutar busca”)

A Vercel hospeda a **mesma página HTML** do projeto, com botão que **refaz a busca e atualiza os preços** (sem abrir GitHub Actions).

URL exemplo após deploy: `https://flyfinder-agent.vercel.app`

---

## Passo 1 — Conectar o repositório

1. Acesse https://vercel.com/new
2. **Import Git Repository** → escolha `flimasales/flyfinder-agent`
3. Framework Preset: **Other** (detecta Python automaticamente)
4. **Deploy** (primeiro deploy pode falhar até configurar env — normal)

---

## Passo 2 — Variáveis de ambiente

No projeto Vercel: **Settings** → **Environment Variables**

| Nome | Valor (sua viagem) |
|------|---------------------|
| `VIAGEM_TRECHOS` | `GRU-IBZ:16/07/2026,CDG-GRU:01/08/2026` |
| `VIAGEM_CLASSE` | `premium` |
| `VIAGEM_MAX_ESCALAS` | `2` |
| `VIAGEM_PAX` | `1` |

Opcional:

| Nome | Uso |
|------|-----|
| `WORKFLOW_URL` | Link do GitHub Actions (só se abrir HTML estático) |
| `TRAVELPAYOUTS_TOKEN` | Adiciona Skyscanner / Kiwi / Trip.com / Aviasales na tabela (cadastre-se em https://www.travelpayouts.com → Tools → API; é grátis) |

**Não** coloque `CALLMEBOT_APIKEY` nem `GIST_TOKEN` na Vercel — esses ficam só no **GitHub Actions** (alertas WhatsApp).

---

## Passo 3 — Redeploy

**Deployments** → último deploy → **⋯** → **Redeploy**

A primeira visita pode demorar ~10–15s (cold start + busca Google Flights).

---

## Como usar

| Onde abrir | Botão “Reexecutar busca” |
|------------|--------------------------|
| `https://seu-projeto.vercel.app` | ✅ Atualiza preços na hora |
| `http://localhost:8765` (`--servir`) | ✅ Igual |
| `file:///.../resultado.html` | ❌ Só HTML estático |
| Link do WhatsApp (htmlpreview) | ❌ Use a URL da Vercel nos favoritos |

---

## Limites

- **Plano Hobby:** timeout máximo **10s** por requisição — se a busca passar disso, use **Pro** (60s no `vercel.json`) ou rode `--servir` local.
- Cada clique no botão = nova consulta ao Google Flights (não abuse).

---

## Alterar rota / datas depois

Edite as env vars na Vercel e faça **Redeploy**, ou altere o padrão em `buscar_passagens.py` → `viagem_from_env()`.

---

## WhatsApp + Vercel juntos

| Função | Onde roda |
|--------|-----------|
| Alerta 22h–01h no WhatsApp | GitHub Actions (`CALLMEBOT_APIKEY`) |
| Página com botão de atualizar | Vercel (este guia) |

No alerta WhatsApp, você pode colar a **URL da Vercel** manualmente na mensagem depois, ou abrir a Vercel nos favoritos do celular.
