# Como colocar arte REALISTA (3D) nos jogos — sem mexer em código

Eu desenho tudo em vetor (SVG). Arte 3D realista (tigre, coelho etc. com pelo, luz e sombra de verdade) é **imagem renderizada**, que precisa vir de um gerador de imagens, de um designer 3D ou de um banco de arte **com licença comercial**. O site já está pronto para receber essas imagens: **basta colocar os arquivos na pasta certa** e o jogo passa a usá-las sozinho (o que você não colocar continua com o desenho atual).

## Onde colocar

`static/img/<id-do-jogo>/` — um PNG/WebP **com fundo transparente** por item:

| Arquivo | O que é | Tamanho ideal |
|---|---|---|
| `mascote.png` | Mascote grande do topo do jogo | 800×800 |
| `s0.png` … `s5.png` | Símbolos do mais fraco ao mais forte (jogos 3×3) | 512×512 |
| `s6.png` | Símbolo curinga (WILD) dos jogos 3×3 | 512×512 |
| `s0.png` … `s7.png` | Símbolos dos jogos 6×5 (s7 = símbolo do jackpot) | 512×512 |
| `fundo.jpg` | Cenário de fundo (vertical) | 1080×1920 |
| `capa.png` | Capa do cartão no lobby (quadrada) | 800×720 |

Ids dos jogos: `tigre, coelho, dragao, boi, cobra, panda, cavalo, rato, ovo, gemas, moedas, jackpot, frutas, pesca, oeste, livro, olimpo, doce`.
Exemplo: `static/img/coelho/mascote.png`, `static/img/coelho/s0.png`…

Depois de enviar, abra `https://SEU-SITE/api/skins` (logado) para conferir se o servidor enxergou os arquivos.

## Prompts prontos (para Midjourney, Leonardo, Ideogram, DALL·E, Firefly…)

Estilo base (cole em todos): *"3D rendered casino slot game character, Pixar quality, realistic fur and materials, glossy highlights, vibrant saturated colors, soft studio lighting, centered, front view, isolated on pure transparent background, high detail, no text"*

- **Tigre:** *"charming realistic tiger cub mascot wearing a red Chinese hat and gold coin necklace, smiling, holding a gold ingot"*
- **Coelho:** *"cute realistic white rabbit mascot with big eyes and pink ears, holding a golden carrot, lucky clover on head"*
- **Dragão:** *"friendly emerald dragon mascot with golden horns and pearl, realistic scales"*
- **Boi:** *"strong ox mascot with golden nose ring and red headband, realistic fur"*
- **Cobra / Panda / Cavalo / Rato:** mesmo padrão, trocando o animal e um acessório dourado.
- **Símbolos:** *"3D glossy slot machine symbol: [gold coin | ruby gem | red envelope | golden bell | strawberry …], isolated, transparent background, game asset"* — gere 6 a 8 do mesmo estilo.
- **Fundo:** *"vertical mobile game background, [tema], depth of field, bokeh light, no characters, 9:16"*

## Cuidados de licença (importante)
- Imagens de IA: confira nos termos da ferramenta se o plano permite **uso comercial**.
- Bancos de imagem (como as que você me enviou): compre a **licença comercial** do pacote antes de usar no site.
- **Não** use arte de jogos existentes (PG Soft, Pragmatic, Play'n GO etc.): é protegida e dá problema jurídico.
