# Travelpayouts API — múltiplas fontes de preço

Com a integração, a tabela passa a mostrar ofertas de:

- ✅ **Google Flights** (via `fast-flights`)
- ✅ **Skyscanner** (via Travelpayouts)
- ✅ **Kiwi.com** (via Travelpayouts)
- ✅ **Trip.com** (via Travelpayouts)
- ✅ **Aviasales / Jetradar / Mytrip / Kupibilet** (via Travelpayouts)

Cada linha da tabela ganha uma coluna **Fonte** com um chip colorido.

---

## Passo 1 — Criar conta na Travelpayouts (grátis)

1. https://www.travelpayouts.com/ → **Sign up** (use e-mail comum)
2. Confirme o e-mail
3. Faça login

---

## Passo 2 — Pegar o token da Data API

1. Acesse https://www.travelpayouts.com/programs/100/tools/api
   - Se não aparecer, vá em **Tools** → **API** no menu lateral
2. Em **API token** copie o valor (algo como `e451ad62a0e8468732b6e1ada1e58223`)

> Esse token é diferente do "marker" (afiliado de cliques). Não precisamos do marker nem do script `emrldtp.cc`.

---

## Passo 3 — Configurar nos lugares certos

### Local

```powershell
$env:TRAVELPAYOUTS_TOKEN="seu_token"
python buscar_passagens.py --trecho GRU-IBZ:16/07/2026 --classe premium --html
```

### Vercel

**Settings → Environment Variables → New**
- Nome: `TRAVELPAYOUTS_TOKEN`
- Valor: `seu_token`

Depois: **Redeploy**.

### GitHub Actions

Repositório → **Settings → Secrets → New repository secret**
- Nome: `TRAVELPAYOUTS_TOKEN`
- Valor: `seu_token`

O workflow já lê desse secret automaticamente.

---

## Como funciona

| Sem token | Com token |
|-----------|-----------|
| Tabela mostra só **Google Flights** | Mostra **Google + Skyscanner + Kiwi + Trip.com + outros** |
| Coluna "Fonte" sempre = Google | Mostra a fonte real (chip colorido) |
| Total estimado = só Google | Total estimado pega o **menor** entre todas as fontes |

---

## Limitações honestas

- A Travelpayouts mostra preços **dos últimos 48h** (cache), não busca ao vivo. Preço pode estar levemente desatualizado, mas é referencial.
- Plano grátis: até 1.000 requests/dia (mais que suficiente).
- Os links **Reservar** continuam abrindo o Google Flights filtrado pela cia — pra reservar de verdade, clique e siga lá.
- Travelpayouts é um programa de afiliados (russo). Tudo legítimo, mas você não precisa monetizar nada — só usa a API.

---

## Drive — tracker de afiliado (ganhar comissão)

O **Drive** é um script de tracking diferente do API token: ele registra cliques nos links da sua página pra você **ganhar comissão** quando alguém comprar via Travelpayouts.

### Como ativar

1. No painel Travelpayouts, vá em **Drive** (menu lateral) e copie o ID numérico do snippet (no exemplo `src='https://emrldtp.cc/NTMxODg2.js?t=531886'`, o ID é **531886**)
2. Configure como env var em **um** dos seguintes lugares:

| Onde | Nome | Valor |
|------|------|-------|
| Vercel | `TRAVELPAYOUTS_DRIVE_ID` | `531886` |
| GitHub Actions secret | `TRAVELPAYOUTS_DRIVE_ID` | `531886` |
| Local PowerShell | `$env:TRAVELPAYOUTS_DRIVE_ID="531886"` | — |

3. Redeploy / próximo run e o script é injetado automaticamente no `<head>` de toda página gerada.

### Como verificar se está funcionando

- Abra a página (Vercel ou local), View Source (`Ctrl+U`), procure por `emrldtp.cc` — deve aparecer no `<head>`.
- Volte no painel Travelpayouts → Drive → **Verify installation** — vai detectar.

### Para ganhar comissão de verdade

O script só rastreia cliques. Pra **converter em comissão**, os links que aparecem na página precisam ser de afiliado (com seu **marker**), não diretos.

Hoje os links da nossa página são diretos para Google Flights / Skyscanner / Kayak / etc. Pra trocar pra links de afiliado:

- Use a **WhiteLabel** ou os **shortlinks tp.media** do Travelpayouts
- Ou parametrize os deep links com `marker=SEU_MARKER`

Posso fazer isso depois, é só pedir. Por enquanto o Drive já fica instalado pra rastrear visitas e cliques.

---

## Alternativas (se preferir não cadastrar)

- Manter como está: só Google Flights na tabela.
- Os 5 botões de busca (no fim da página) já abrem Skyscanner/Kayak/Decolar/Trip.com com sua busca preenchida — você compara manualmente.
