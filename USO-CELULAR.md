# Usar no Claude do celular

Este repo é a skill **Edvid** — editor de vídeo por conversa, com o que a dig.D mudou em cima do original (Creator Factory).

## O que o celular faz e o que não faz

No iPhone/Android o Claude **lê** esta skill e te guia (estratégia, corte, estilo).  
O **render** (transcrever, cortar, legendas, mp4) precisa de um computador com `ffmpeg` + `uv` + Node — o Mac da Dani já tem.

Dois jeitos de trabalhar no celular:

### A — Claude no celular + Mac renderizando (o que funciona hoje)

1. No celular: app Claude → **Projects** → projeto novo → **Add from GitHub** → `agencia-digd/edvid`.
2. Manda o vídeo (ou o link) e fala o que quer: *“edita isso num Reels, 20s, estilo split”*.
3. O Claude do celular segue o `SKILL.md` e te devolve o plano / EDL / o que falta decidir.
4. Quando for **gerar o mp4**, abre o Claude Code no Mac (ou manda pra Dora) na pasta do vídeo e fala: *“continua o Edvid, o plano está no projeto”*.

### B — Claude Code no Mac, você só manda áudio/vídeo pelo celular

1. No Mac: `cd ~/Videos/meu-take && claude`
2. Primeira vez: a skill já está em `~/.claude/skills/edvid` (é esta).
3. *“edita o arquivo X num Reels”*.
4. Você acompanha o preview e marca o que recusa.

Não existe ingest automático do Telegram: vídeo que chega no chat **não** entra na edição até você pedir pelo nome do arquivo.

## O que falar (prompts que funcionam)

- *“Inventaria essas tomadas e me propõe a estratégia. Não corta ainda.”*
- *“Edita o IMG_3136 num Reels de ~20s. Espera eu aprovar o corte.”*
- *“Aprova o corte. Abre a aba Estilo.”*
- *“Deleta de 4.2 a 5.4. Recorta.”*
- *“Gera o social (final-social.mp4).”*

## O que a dig.D mudou no principal

Isto **não** é o Edvid virgem do Fill. Em cima do original:

1. **Telegram não auto-ingere** — só edita quando você pede.
2. **QA de legenda** (`caption_qa.py`) — DGD→dig.D, área→arte, sem realçar “um/que”.
3. **Brand pack** em `assets/brand/digd/` — ilustra tese, não substantivo (sem hoodie/matcha).
4. **Trilha Mixkit local** — Treblo pago não é obrigatório. Sem chave, não entrega mudo.
5. **`encode_social.py`** depois de todo `final.mp4` — arquivo leve pro Instagram.
6. **CTA sem split** no último pedido (*toca aqui / fala com a gente*).

## Instalação (só no computador, uma vez)

Mac:

```bash
brew install uv ffmpeg node git
# fecha e reabre o terminal
cd /caminho/deste/repo
uv sync
# symlink da skill (se ainda não estiver)
ln -sfn "$(pwd)" ~/.claude/skills/edvid
```

Aí o Claude Code no Mac encontra a skill sozinho.

## Pastas

- Material bruto: qualquer pasta sua.
- Resultado: `<pasta>/edit/` (`cut.mp4` → aprovação → `final.mp4` → `final-social.mp4`).
- Originais **nunca** são alterados.
