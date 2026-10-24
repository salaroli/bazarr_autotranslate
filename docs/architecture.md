# Arquitetura — estado atual

O que o daemon **faz** está no `README.md` (pipeline de legendas). Este doc
cobre como o serviço vive no parque.

## Classificação

App própria, single-service, **interna e stateless** (o único volume é log).
No espectro de tiers do `~/dev/docs/padrao-cicd.md`: build próprio como tier 1,
sem os anexos de game server (não há janela de deploy — reiniciar no meio de
uma tradução só re-enfileira o item no próximo scan, o cooldown por vídeo
absorve).

## Peças

| Peça | Onde |
|---|---|
| Código | este repo (`main.py` + módulos, imagem única) |
| Build | `build.yml`: push na `main` → imagem `latest` + `<sha-curto>` no registro (`192.168.1.2:3000` — migração pro HTTPS é TODO do parque) |
| Deploy | Stack `bazarr_autotranslate` no Komodo com `poll_for_updates` + `auto_update` — merge na `main` redeploya sozinho, sem workflow de deploy |
| Config/segredo | `environment` da Stack no Komodo (Mongo — não o `.env` do disco); `BAZARR_API_KEY` vive lá, nunca no repo |
| Updates | `renovate.yml` diário (base image e deps via `config:recommended`) |
| Rede | `mediaproxy` (externa) — fala com o `bazarr` por nome de container |
| Logs | `/mnt/nvme/appdata/bazarr-autotranslate` (bind, só log) |

## Dependências de runtime

Tudo via API do Bazarr (`BAZARR_BASE_URL`): o daemon **não** fala direto com
Lingarr nem WhisperAI — pede pro Bazarr, que orquestra os providers dele.
Bazarr fora do ar = daemon inofensivo em loop de erro; Lingarr/Whisper fora
do ar = estágio correspondente do pipeline falha e o item fica pra próxima.

## Cadeia a jusante (estado de 2026-10-24)

O daemon não fala com estes serviços, mas é o resultado deles que ele cobra:

| Estágio | Quem faz | Config que importa |
|---|---|---|
| Tradução | Bazarr `translator_type: lingarr` → Lingarr (`:9877`, repo `salaroli/lingarr`) → **Gemini** `gemini-3.5-flash-lite`, fallback **por linha** pro Google Tradutor gratuito (`SERVICE_TYPE=["gemini","google"]`) | `lingarr_url` no Bazarr **sem `/` final**; prompt na tabela `settings` do Lingarr |
| Transcrição | Bazarr provider `whisperai` → **subgen** (`:9876`, repo `salaroli/subgen`) na RTX 2080 SUPER | `NVIDIA_VISIBLE_DEVICES` = UUID da 2080 |

Até 2026-08-29 a tradução ia pro ollama local e o subgen usava uma 3090; as
duas GPUs morreram e a cadeia ficou quebrada em silêncio até 24/10.

Armadilhas medidas (o porquê de cada config acima):

- **`lingarr_url` com `/` final** vira `//api/translate/content`; o Lingarr
  devolve o HTML da SPA com **200** e o Bazarr loga `Expecting value: line 1
  column 1`. Nada traduz, e nada aponta pra URL.
- **O Gemini recusa algumas falas com `PROHIBITED_CONTENT`** (palavrão/sexo,
  ex. Rick and Morty) e esse filtro **não desliga** com `safetySettings`
  `BLOCK_NONE`/`OFF`. Sem o fallback, uma linha recusada derruba o episódio
  inteiro (`Invalid or empty response from Gemini API`).
- **Prompt do Lingarr:** só existem `{lineToTranslate}`, `{sourceLanguage}`,
  `{targetLanguage}`, `{contextBefore}`, `{contextAfter}`. Placeholder
  desconhecido vai literal pro modelo. Não envolver a fala em tags: o Gemini
  copia as tags na saída (medido 28/32 linhas ruins).
- **Contexto (desde 24/10):** o Lingarr manda 2 falas antes e 2 depois
  (`ai_context_before/after`), mas elas só chegam ao modelo se o prompt tiver
  `{contextBefore}`/`{contextAfter}` — e até 24/10 não tinha, então o contexto
  nunca foi usado. Agora o `ai_prompt` termina com um bloco de contexto e a
  regra "traduza TODAS as linhas da mensagem". A/B cego em 150 falas (juiz
  Sonnet, controle Gemini×Gemini = 22×22): esse formato venceu 42×20, erros de
  gênero 11→2, zero fala cortada. O layout que a doc do Lingarr recomenda
  (contexto no `ai_user_prompt` com `[Context-Before]`/`[Target-to-Translate]`)
  e contexto sem a regra das linhas **cortavam a 2ª linha da legenda** (33/89
  e 40/89) — não usar. Em episódio real (Locked Up S01E03/E10): 0 vazamento,
  legendas que perdiam uma linha 113→27 e 86→23, trocas de gênero julgadas
  18 certas × 5 erradas (os erros são quase todos o vocativo "darling" em
  telefonema). Linhas que o Gemini bloqueia vão pro Google, que **não usa
  contexto** — essas seguem sem correção de gênero. Backups do prompt em
  `/mnt/user/appdata/lingarr/ai_prompt.bak-20261024*.txt`.
- **Correção manual de fala:** editar o `.pt-BR.srt` **e** o `target` da
  linha em `translation_request_lines` (senão uma retradução reaproveita o
  erro). O daemon só traduz quando falta o `.pt-BR.srt`, então não sobrescreve.
  Feito em 24/10 em Locked Up S01E03 pos 492 e S01E10 pos 472 (backups dos
  `.srt` em `/mnt/user/appdata/lingarr/*.pt-BR.srt.bak-20261024`).
- Legendas traduzidas **antes** de 24/10 continuam com o prompt antigo (sem
  contexto); só S01E03/E10 de Locked Up foram refeitas.
- **O Lingarr reaproveita requisição `Completed` com as mesmas linhas-fonte.**
  Mudou prompt/modelo e quer refazer: apagar as `translation_request_lines`
  da requisição antiga, senão volta o resultado velho em ~10 s.
- **Envs do Lingarr sobrescrevem a tabela `settings` a cada boot**
  (`StartupService.ApplySettingsFromEnvironment`); o que estiver no compose
  vence a UI.
- Ruído conhecido, sem efeito: `Integration request failed: NotFound` no
  Lingarr (o Bazarr manda ID de série onde o Lingarr espera ID de episódio).

## Sem backup, de propósito

Estado = fila em memória + cooldowns em memória + logs. Perder o container
custa no máximo um ciclo de scan (5 min). Por isso também está no skip do
plugin Appdata Backup (não há volume que valha o restart noturno das 04:00).
