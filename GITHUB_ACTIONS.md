# Monitor na nuvem (GitHub Actions)

> **Página com botão “Reexecutar busca” (atualiza preços na hora):** veja [VERCEL.md](VERCEL.md) — deploy grátis na Vercel.

Roda **sozinho**, **grátis**, **sem PC ligado**. Checa preços a cada 30 min entre **22h e 01h** (Brasília) e manda WhatsApp se o total estiver entre **R$ 10.000 e R$ 12.000**.

---

## Passo 1 — Ativar o CallMeBot (WhatsApp)

1. Salve nos contatos: **+34 644 51 95 23** (nome: CallMeBot)
2. Pelo WhatsApp do **11986185400**, envie para esse número:

   ```
   I allow callmebot to send me messages
   ```

3. Em ~2 min você recebe a **API key** (ex: `1234567`)

---

## Passo 2 — Criar repositório no GitHub

1. Acesse https://github.com/new
2. Nome sugerido: `flyfinder-agent`
3. Marque **Private** (recomendado — tem seu número de WhatsApp no workflow)
4. **Não** marque README/license (já temos arquivos locais)
5. Clique **Create repository**

---

## Passo 3 — Subir o código

No PowerShell, na pasta do projeto:

```powershell
cd D:\www\flyfinder-agent

git init
git add buscar_passagens.py requirements.txt .gitignore .github prompt_pesquisa_passagens.md GITHUB_ACTIONS.md
git commit -m "Monitor de preços multi-cidade com alerta WhatsApp"
git branch -M main
git remote add origin https://github.com/SEU_USUARIO/flyfinder-agent.git
git push -u origin main
```

Troque `SEU_USUARIO` pelo seu usuário do GitHub.

---

## Passo 4 — Configurar os secrets

No GitHub: repositório → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

### 4.1 — `CALLMEBOT_APIKEY` (obrigatório, envia WhatsApp)

- Name: `CALLMEBOT_APIKEY`
- Secret: a key numérica que o CallMeBot enviou no WhatsApp

### 4.2 — `GIST_TOKEN` (opcional mas recomendado — link da página HTML no WhatsApp renderizando corretamente)

Sem este secret, o alerta ainda chega, mas o link aponta para `catbox.moe` que serve HTML como texto puro (mostra código-fonte em vez da página).

1. Acesse https://github.com/settings/tokens?type=beta
2. **Generate new token** → **Fine-grained** ou clássico tanto faz
3. Para token clássico: marque **só** o escopo `gist` (nada mais)
4. **Generate token** → copie o valor (`ghp_...`)
5. Cadastre como secret no repositório:
   - Name: `GIST_TOKEN`
   - Secret: o `ghp_...` copiado

> **Segurança:** nunca coloque API keys ou tokens no código. Só nos secrets do GitHub. Tokens podem ser revogados a qualquer momento em https://github.com/settings/tokens.

---

## Passo 5 — Ativar o workflow

1. Aba **Actions** do repositório
2. Se aparecer “Workflows aren’t being run…” → clique **I understand my workflows, go ahead and enable them**
3. Clique em **Monitor de Preços** na lista à esquerda

Pronto. O cron roda sozinho nos horários agendados.

---

## Testar manualmente (sem esperar 22h)

1. **Actions** → **Monitor de Preços** → **Run workflow**
2. Para só testar o WhatsApp:
   - marque **testar_whatsapp** = `true`
3. Para testar busca de preço agora (qualquer horário):
   - marque **ignorar_horario** = `true`
4. **Run workflow**

Veja o resultado em **Actions** → clique na execução → **checar-preco**.

---

## Horários (referência)

| Horário Brasília | Horário UTC (cron GitHub) |
|------------------|---------------------------|
| 22:00            | 01:00                     |
| 22:30            | 01:30                     |
| 23:00            | 02:00                     |
| 23:30            | 02:30                     |
| 00:00            | 03:00                     |
| 00:30            | 03:30                     |

O script usa fuso **America/Sao_Paulo** para a janela 22h–01h.

---

## Alterar viagem / preços / número

Edite o arquivo `.github/workflows/monitor-precos.yml` (bloco `run:`) e faça commit, ou peça ajuda para ajustar:

- Trechos: `--trecho GRU-IBZ:16/07/2026` etc.
- Classe: `--classe premium`
- Faixa: `--preco-min 10000 --preco-max 12000`
- WhatsApp: `--whatsapp 11986185400`

---

## Limites do plano grátis

- GitHub Actions: ~2.000 min/mês em repo privado (este job usa ~5 min/dia → ~150 min/mês)
- CallMeBot: limite de mensagens por dia (uso pessoal costuma ser suficiente)

---

## Problemas comuns

| Problema | Solução |
|----------|---------|
| Workflow não aparece em Actions | Faça push da pasta `.github/workflows/` |
| Secret não encontrado | Nome exato: `CALLMEBOT_APIKEY` |
| WhatsApp não chega | Refaça o opt-in no CallMeBot; confira a key |
| “fora da janela” no log | Normal fora de 22h–01h BRT; use **ignorar_horario** para teste |
| Preço ~R$ 19k, sem alerta | Esperado — só alerta entre R$ 10k e R$ 12k |
