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

## Alternativas (se preferir não cadastrar)

- Manter como está: só Google Flights na tabela.
- Os 5 botões de busca (no fim da página) já abrem Skyscanner/Kayak/Decolar/Trip.com com sua busca preenchida — você compara manualmente.
