# Prepara Vídeo TV

Aplicativo local para Windows, pensado para a Samsung UN40NU7100G.

## Abrir e usar

1. Dê dois cliques em **Iniciar.vbs**.
2. Clique em **Adicionar vídeos** e selecione um ou vários arquivos.
3. Use **Analisar arquivos** para conhecer o tratamento previsto.
4. Para um primeiro teste, marque **Gerar apenas amostra dos primeiros 3 minutos**.
5. Clique em **Preparar para a TV**. Depois use **Abrir resultados**.

Sem pasta de destino definida, cada original recebe uma pasta vizinha `Preparados-TV`. Saídas existentes recebem um novo nome numerado; originais nunca são sobrescritos. Cada vídeo recebe um relatório JSON local. O aplicativo não faz uploads nem downloads.

## O que faz

- Mantém o primeiro vídeo principal, um áudio principal e até duas legendas internas: uma em português e uma em inglês.
- Prefere legendas completas, português brasileiro identificado nos metadados e inglês sem SDH. Não adivinha o idioma de faixas sem identificação.
- Você pode preferir o áudio inglês ou português; se faltar, mantém o áudio principal disponível e informa isso.
- Converte legendas de texto para SRT; estilos de ASS/SSA são perdidos. Legendas em imagem são omitidas com aviso: não há OCR. Arquivos externos de legenda ainda não são importados.
- Remove capítulos, anexos e metadados globais na cópia. Idiomas das legendas são identificados na saída.
- Pode analisar sem criar vídeos, processar lotes, gerar amostras e cancelar. Cópias já concluídas permanecem disponíveis após cancelamento.

## Modos

**Automático:** remonta o MKV; copia H.264/HEVC e áudio dentro de um perfil conservador; converte outros formatos ou parâmetros fora desse perfil. “Fora do perfil” não significa necessariamente incompatível: alguns formatos que a TV aceita também são convertidos por cautela. Converter vídeo pode demorar e reduz qualidade.

**Só simplificar:** copia vídeo e áudio sem recompressão e reduz as legendas. É o tratamento validado nos testes de Dark Matter com PT-BR + inglês. Não corrige incompatibilidades do áudio/vídeo.

**Compatibilidade ampliada:** converte vídeo SDR para H.264 de 8 bits, até 1920×1080 e 30 fps, e áudio para AC-3 até 5.1. Pode reduzir resolução, fluidez e qualidade. Atmos não é preservado na conversão de áudio. Se o arquivo for HDR/Dolby Vision identificado e exigir conversão de vídeo, o programa bloqueia o processamento: esta versão não implementa conversão de cores HDR para SDR.

## Limites e evidência

O teste na TV mostrou que a amostra com 44 legendas travou e as amostras sem legendas, apenas PT-BR e PT-BR + inglês funcionaram. Isso aponta para o conjunto de legendas, mas não comprova que quantidade seja a única causa nem que exista um número máximo universal.

A análise consulta metadados; ela não reproduz o decodificador da TV, não mede picos reais de bitrate nem testa o pendrive. Metadados podem estar errados. Não promete detectar ou reparar todo arquivo danificado. A verificação opcional decodifica toda a cópia no computador, interrompendo a entrega se houver erro reportado. Mesmo assim, é necessário testar na TV.

O perfil conservador usa H.264/HEVC 4:2:0 até nível 5.1, resolução até 3840×2160, H.264 UHD até 30 fps, demais casos até 60 fps e taxa informada até 50 Mb/s. Base: [especificações Samsung 2018](https://developer.samsung.com/smarttv/develop/specifications/media-specifications/2018-tv-video-specifications.html), especialmente o grupo UHD STANDARD2. Essas regras são heurísticas de preparação, não certificação do aparelho.

## Dependências e manutenção

Usa Python com Tk e os executáveis FFmpeg/FFprobe já instalados neste computador. Não é um executável independente para qualquer computador. O inicializador usa o Python local de Pena. Os executáveis FFmpeg são procurados no PATH, em `bin` ao lado do programa ou na instalação WinGet de Gyan.FFmpeg.

Código: `app.py` (interface) e `engine.py` (análise/processamento). Testes: `python -m unittest -v test_engine`. O teste de integração cria mídia sintética em pasta temporária própria, verifica preservação das faixas, conversão, nomes únicos, cancelamento e manutenção do original.
