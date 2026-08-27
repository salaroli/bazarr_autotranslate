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

## Sem backup, de propósito

Estado = fila em memória + cooldowns em memória + logs. Perder o container
custa no máximo um ciclo de scan (5 min). Por isso também está no skip do
plugin Appdata Backup (não há volume que valha o restart noturno das 04:00).
