# CLAUDE.md — Edvid (fork dig.D)

Você está no repositório da skill Edvid usada pela Dani/dig.D.

Antes de editar qualquer vídeo: leia `SKILL.md` inteiro e cumpra as Hard Rules.

## Começo de sessão

1. Pergunte qual arquivo e o que ela quer (Reels / longform / só inventário). Vídeo que chegou no Telegram **não** é ordem de editar.
2. Rode Fase 1 (transcreve → estratégia → espera confirmação → corte + grade).
3. Mostre `cut.mp4`. Sem aprovação, **não** começa Fase 2.
4. Estilo: aba do preview (`awaitingStyle`), não lista no chat.
5. Depois do `final.mp4`, rode `encode_social.py`.

## Regras desta casa (além do SKILL.md)

- QA de caption obrigatório (`helpers/caption_qa.py --write`) no pack `assets/brand/digd/`.
- Sem Treblo: `helpers/pick_bed.py`. `musicAI` sem chave não entrega mudo.
- CTA final sem split.
- Outputs só em `<videos_dir>/edit/`.

## Se ela está no celular

Explique o plano e peça as decisões (estratégia, estilo, marcações). Não finja que o iPhone renderizou o mp4. O render roda no Mac/VPS com esta skill instalada.
