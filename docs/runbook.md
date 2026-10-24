# Runbook — bazarr_autotranslate

## Triagem

```sh
ssh root@192.168.1.2
docker ps -a --filter name=bazarr_autotranslate
docker logs bazarr_autotranslate --tail 50
```

- **Loop de erro de conexão** → Bazarr é a dependência dura
  (`docker ps --filter name=bazarr`, UI em `192.168.1.2:6767`). Com o Bazarr
  de volta, o daemon se recupera sozinho no próximo scan (5 min).
- **Legenda não aparece pra um item específico** → provavelmente cooldown
  (1h por vídeo) ou `MIN_SCORE` alto demais pro que existe online. Conferir
  no log o estágio em que o item parou; o pipeline é o do README.
- **Tradução travada** → Lingarr (`docker logs lingarr`); transcrição
  travada → provider WhisperAI configurado no Bazarr (subgen). O daemon só enfileira
  via API do Bazarr — o gargalo real aparece no log DELES.
- 🔴 **"Translation finished" no log do daemon NÃO prova tradução.** O
  `PATCH /api/subtitles?action=translate` do Bazarr é assíncrono e devolve
  204 na hora, mesmo quando a tradução falha depois. Prova = `.pt-BR.srt` novo
  ao lado do vídeo + `grep -E 'lingarr.*(fail|error)|Translation failed'` vazio
  em `/mnt/user/appdata/bazarr/log/bazarr.log`. Causas já vistas e fixes em
  `docs/architecture.md` § Cadeia a jusante.
- **401/403 no log** → `BAZARR_API_KEY` rotacionada. Atualizar no
  `environment` da Stack no Komodo (via `GetStack`/`UpdateStack` ou UI —
  nunca no `.env` do disco, o deploy sobrescreve) e redeployar.

Restart simples: `docker restart bazarr_autotranslate` — sem efeito colateral
(estado é em memória; itens em andamento re-entram no próximo scan).

## Deploy

Merge na `main` → `build.yml` publica `latest` + `<sha-curto>` → a Stack no
Komodo (`poll_for_updates` + `auto_update`) redeploya sozinha em alguns
minutos. Sem janela de deploy: reiniciar não perde nada além de um ciclo.

Validar de verdade (não só o CI verde):

```sh
docker inspect bazarr_autotranslate --format '{{.Config.Image}} {{.State.StartedAt}}'
```

## Rollback

```sh
# 1. achar o sha-curto anterior no registro (imagens bazarr-autotranslate:<sha>)
# 2. na Stack do Komodo: IMAGE_TAG=<sha-curto> e Deploy
```

Gotcha do parque: com `IMAGE_TAG` pinada em sha, os merges seguintes **não**
entram sozinhos — o auto_update volta a valer só depois de devolver
`IMAGE_TAG=latest` e deployar (ver `padrao-cicd.md` §2, "Tags").

## CI

`build.yml` tem `workflow_dispatch` — falha transitória re-roda por dispatch,
não precisa de commit vazio. Debug de CI: `padrao-cicd.md` §8.
