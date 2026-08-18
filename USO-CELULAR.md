# Codex (GPT) e Claude Code no celular

Sim — **se você abrir este repo no Codex ou no Claude Code**, não é só chat.
Eles sobem um ambiente Linux e rodam comando. Dá pra instalar e renderizar.

Não precisa que eu te mande a skill Edvid à parte: ela **já está neste repo**
(`SKILL.md` + `helpers/` + `CLAUDE.md`). A única skill extra da Fase 2
(legendas/Remotion) também já veio junto: `skills/remotion-best-practices/`.

## Como abrir

1. App **ChatGPT → Codex** *ou* app **Claude → Claude Code**.
2. **Clone / Open from GitHub:** `agencia-digd/edvid` (conta da org; o repo é privado).
3. Primeira mensagem:

   *“Lê o CLAUDE.md e o SKILL.md. Instala o que faltar (uv sync, ffmpeg, node). Quando estiver pronto, eu mando o vídeo.”*

4. Anexa o take (ou cola o link do Instagram/Drive).
5. *“Edita isso num Reels. Propõe a estratégia e espera eu aprovar o corte.”*

## O que o ambiente precisa (ele mesmo instala)

| Peça | Onde está |
|---|---|
| Método + regras da casa | `SKILL.md`, `CLAUDE.md` |
| Scripts (corte, whisper, social) | `helpers/` |
| Remotion (Fase 2) | `skills/remotion-best-practices/` |
| `ffmpeg`, `uv`, Node 18+ | o agente instala no Linux da sessão |
| WhisperX (~2 GB na 1ª vez) | `uv sync` neste repo |

Primeira sessão é **lenta** (baixa modelo de transcrição). As seguintes, não.

## O que ainda pode travar no celular

- **Vídeo grande:** anexa um take de 30–90s, não um 4K de 10 min.
- **Preview visual** (timeline no browser): no celular costuma ficar ruim. Você aprova pelo mp4 que o agente te devolve.
- **Tempo da sessão cloud:** se a 1ª instalação estourar, manda *“continua o uv sync”* no mesmo thread.
- **Repo privado:** a conta do app precisa ver `agencia-digd`. Se o Codex/Claude não listar o repo, me fala o user que eu te coloco.

## O que falar

- *“Inventaria e me propõe a estratégia. Não corta ainda.”*
- *“Edita o arquivo X num Reels de ~20s. Espera eu aprovar.”*
- *“Aprova o corte. Segue a Fase 2.”*
- *“Gera o social (final-social.mp4).”*

Telegram **não** entra sozinho. Só edita o arquivo que você nomeou.
