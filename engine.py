"""Preparação local de vídeos. Não modifica os arquivos de entrada."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, asdict, field
from fractions import Fraction
import unicodedata

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
TEXT_SUBS = {'subrip', 'ass', 'ssa', 'mov_text', 'webvtt', 'text'}
IMAGE_SUBS = {'hdmv_pgs_subtitle', 'dvd_subtitle'}
DEFAULT_OUTPUT_DIR = Path(r'C:\Users\Pena\Documents\Torrents\Preparados-TV')
VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.mov', '.m4v', '.ts', '.m2ts', '.webm', '.wmv'}
SUBTITLE_EXTENSIONS = {'.srt', '.ass', '.ssa', '.vtt'}


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text.casefold())
                   if not unicodedata.combining(c))


def named_language(name):
    name = normalized(name)
    patterns = [
        ('pt-br', r'pt[ ._-]?br|pob|brazil(?:ian)?|brasil(?:eiro)?|brazilian[ ._-]portuguese'),
        ('pt', r'pt(?:[ ._-]pt)?|por|portugu[eê]s|portuguese'),
        ('en', r'en(?:[ ._-](?:us|gb))?|eng|english|ingles'),
        ('other', r'es|spa|spanish|espanol|fr|fre|fra|french|francais|de|deu|ger|german|it|ita|italian|jpn|japanese|arabic|rus|russian'),
    ]
    for language, pattern in patterns:
        if re.search(r'(?<![a-z0-9])(?:' + pattern + r')(?![a-z0-9])', name):
            return language
    return ''


def read_subtitle(path):
    with path.open('rb') as f:
        raw = f.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('legenda maior que o limite de análise de 8 MB')
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        return raw.decode('utf-16'), None  # FFmpeg reconhece o BOM.
    try:
        return raw.decode('utf-8-sig'), None
    except UnicodeDecodeError:
        return raw.decode('cp1252'), 'CP1252'


def content_language(text):
    # Heurística local: comparar palavras frequentes, incluindo idiomas próximos
    # para não confundir uma legenda espanhola/francesa com português/inglês.
    text = re.sub(r'<[^>]*>|\{[^}]*\}', ' ', text)
    words = re.findall(r'[a-z]+', normalized(text))
    vocabularies = {
        'pt': 'nao voce voces que eu uma para com estou esta isso meu minha sim mas por aqui como ele ela nos entao tambem muito obrigado porque tenho vamos foi seu sua',
        'en': 'the you your i is are was were have has do did dont what that this with for not it we they he she my me yes but can would could should will just from know',
        'es': 'no yo tu usted ustedes una para con estoy esta eso mi pero aqui como el ella nosotros entonces tambien mucho gracias porque tengo vamos fue su',
        'fr': 'je tu vous nous ils elles pas une pour avec suis est ca mon mais ici comme alors aussi merci pourquoi avez tres oui',
        'de': 'ich du sie wir nicht das der die ein eine und ist mit habe aber hier danke ja was',
        'it': 'io tu voi noi non una per con sono questo mio ma qui come lui lei allora anche grazie molto',
    }
    scores = {}
    for language, vocab in vocabularies.items():
        vocabulary = set(vocab.split())
        scores[language] = sum(word in vocabulary for word in words)
    ranked = sorted(scores, key=scores.get, reverse=True)
    best, runner_up = ranked[:2]
    if best in {'pt', 'en'} and scores[best] >= 8 and scores[best] >= 1.7 * max(1, scores[runner_up]):
        return best
    return ''


def external_subtitles(source, notes):
    source = Path(source)
    if not source.is_file():
        return []
    try:
        siblings = list(source.parent.iterdir())
        videos = [p for p in siblings if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS]
        files = [p for p in siblings if p.is_file() and p.suffix.lower() in SUBTITLE_EXTENSIONS]
        for folder in siblings:
            if folder.is_dir() and folder.name.casefold() in {'subs', 'sub'}:
                # Não seguir links de diretório; incluir pastas por filme/idioma.
                for directory, dirs, names in os.walk(folder, followlinks=False):
                    dirs[:] = sorted(d for d in dirs if not (Path(directory) / d).is_symlink())
                    files.extend(Path(directory) / name for name in names
                                 if Path(name).suffix.lower() in SUBTITLE_EXTENSIONS)
    except OSError as exc:
        notes.append(f'Não foi possível buscar legendas externas: {exc}')
        return []
    result = []
    for path in sorted(set(files), key=lambda p: str(p).casefold()):
        relative = str(path.relative_to(source.parent).with_suffix(''))
        matching = [v for v in videos if normalized(v.stem) in normalized(relative)]
        owner = max(matching, key=lambda v: len(v.stem), default=None)
        if (owner is not None and owner != source) or (owner is None and len(videos) != 1):
            notes.append(f'Legenda externa ignorada por associação ambígua ou de outro vídeo: {relative}.')
            continue
        try:
            text, encoding = read_subtitle(path)
            # O nome do filme pode conter palavras como "English" ou "It".
            label = re.sub(re.escape(source.stem), '', relative, flags=re.IGNORECASE)
            filename_label = re.sub(re.escape(source.stem), '', path.stem, flags=re.IGNORECASE)
            language = named_language(filename_label)
            if not language:
                for parent in reversed(path.relative_to(source.parent).parts[:-1]):
                    language = named_language(re.sub(re.escape(source.stem), '', parent, flags=re.IGNORECASE))
                    if language:
                        break
            method = 'nome'
            if not language:
                language = content_language(text)
                method = 'texto (estimativa)'
            if language not in {'pt-br', 'pt', 'en'}:
                notes.append(f'Legenda externa omitida: {relative} (idioma não identificado com confiança ou fora de PT/EN).')
                continue
            data = probe(path)
            stream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'subtitle'), None)
            if stream is None or stream.get('codec_name') not in TEXT_SUBS:
                notes.append(f'Legenda externa sem faixa de texto utilizável: {relative}.')
                continue
            candidate = dict(stream)
            candidate['tags'] = {'language': language, 'title': label}
            candidate['disposition'] = {'forced': bool(re.search(r'(?i)(?<![a-z])(?:forced|forcada)(?![a-z])', normalized(label)))}
            candidate['external_path'] = str(path)
            candidate['encoding'] = encoding
            candidate['matched_name'] = owner is not None
            result.append(candidate)
            notes.append(f'Legenda externa encontrada: {relative}; idioma {language}, identificado pelo {method}.')
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            notes.append(f'Não foi possível ler a legenda externa {relative}: {exc}')
    return result


class Cancelled(Exception):
    pass


def tool(name):
    nearby = Path(__file__).parent / 'bin' / (name + '.exe')
    found = str(nearby) if nearby.exists() else shutil.which(name)
    if not found:
        root = Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft/WinGet/Packages'
        found = next((str(p) for p in root.glob(f'Gyan.FFmpeg*/ffmpeg*/bin/{name}.exe')), None)
    if not found:
        raise RuntimeError(f'{name} não encontrado. Instale FFmpeg ou coloque seus executáveis na pasta bin do aplicativo.')
    return found


def probe(path):
    result = subprocess.run([tool('ffprobe'), '-v', 'error', '-show_format', '-show_streams',
                             '-of', 'json', str(path)], capture_output=True,
                            encoding='utf-8', errors='replace', timeout=60, creationflags=NO_WINDOW)
    if result.returncode:
        raise RuntimeError('Não foi possível analisar o arquivo: ' + result.stderr[-1500:])
    return json.loads(result.stdout)


def lang(stream):
    value = stream.get('tags', {}).get('language', '').lower().replace('_', '-')
    if value in {'por', 'pt', 'pt-br', 'pt-pt', 'pob'}:
        return 'pt'
    if value in {'eng', 'en', 'en-us', 'en-gb'}:
        return 'en'
    return value


def subtitle_score(s, language):
    title = s.get('tags', {}).get('title', '').lower()
    brazil = any(x in title for x in ('brazil', 'brasil', 'pt-br')) or s.get('tags', {}).get('language', '').lower() in {'pt-br', 'pob'}
    # Preferir uma faixa completa; só depois preferir a variante brasileira.
    return (not bool(s.get('disposition', {}).get('forced')),
            s.get('codec_name') in TEXT_SUBS,
            brazil if language == 'pt' else True,
            not any(x in title for x in ('sdh', 'hearing', 'commentary')),
            bool(s.get('matched_name')),
            not bool(s.get('external_path')),
            -s['index'])


@dataclass
class Plan:
    source: str
    video: int
    audio: int | None
    subtitles: list[int]
    subtitle_languages: list[str]
    convert_video: bool
    convert_audio: bool
    channels: int
    duration: float
    notes: list[str]
    blockers: list[str]
    subtitle_sources: list[dict] = field(default_factory=list)


def analyze(path, data, mode='auto', audio_preference='original'):
    streams = data.get('streams', [])
    videos = [s for s in streams if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')]
    if not videos:
        raise ValueError('Arquivo sem faixa de vídeo utilizável.')
    v = videos[0]
    audios = [s for s in streams if s.get('codec_type') == 'audio']
    a = max(audios, key=lambda s: (audio_preference != 'original' and lang(s) == audio_preference,
                                  bool(s.get('disposition', {}).get('default')), -s['index']), default=None)
    notes, blockers, chosen, languages = [], [], [], []
    internal_subs = [s for s in streams if s.get('codec_type') == 'subtitle']
    external_subs = external_subtitles(path, notes)
    subs = internal_subs + external_subs
    subtitle_sources = []
    for language, label in [('pt', 'português'), ('en', 'inglês')]:
        candidates = [s for s in subs if lang(s) == language]
        if not candidates:
            notes.append(f'Não há legenda identificada em {label}; não será criada nem traduzida.')
            continue
        supported = [s for s in candidates if s.get('codec_name') in TEXT_SUBS | IMAGE_SUBS]
        if not supported:
            notes.append(f'Não há legenda em {label} com codec suportado; será omitida.')
            continue
        ranked = sorted(supported, key=lambda s: subtitle_score(s, language), reverse=True)
        for selected in (ranked[:1] if language == 'pt' else ranked):
            chosen.append(selected['index'])
            subtitle_sources.append({'path': selected.get('external_path'), 'index': selected['index'],
                                     'encoding': selected.get('encoding'), 'codec': selected.get('codec_name'),
                                     'title': selected.get('tags', {}).get('title', ''),
                                     'forced': bool(selected.get('disposition', {}).get('forced')),
                                     'hearing_impaired': bool(selected.get('disposition', {}).get('hearing_impaired'))})
            languages.append(language)
            origin = selected.get('external_path') or f'faixa interna {selected["index"]}'
            notes.append(f'Legenda selecionada em {label}: {origin}.')
            if selected.get('codec_name') in IMAGE_SUBS:
                notes.append(f'Legenda em {label} é uma imagem (PGS/VobSub) e será copiada diretamente para o MKV, sem OCR.')
            elif selected.get('codec_name') != 'subrip':
                notes.append(f'Legenda em {label} será simplificada para SRT, sem estilos avançados.')
            if selected.get('disposition', {}).get('forced'):
                notes.append(f'Legenda selecionada em {label} ({origin}) é parcial/forçada.')
    notes.append(f'Legendas: {len(internal_subs)} internas + {len(external_subs)} externas identificadas → {len(chosen)} na cópia (até uma PT e todas as EN suportadas).')
    if len(audios) > 1:
        notes.append(f'Áudio: manter apenas a faixa {a["index"]}, idioma {lang(a) or "não identificado"}, de {len(audios)} disponíveis.')
    if not a:
        notes.append('Arquivo sem áudio.')
    elif audio_preference != 'original' and lang(a) != audio_preference:
        notes.append('Áudio no idioma preferido não encontrado; usando a faixa principal.')

    codec = v.get('codec_name', '')
    pix = v.get('pix_fmt', '')
    level = v.get('level', -1)
    width, height = v.get('width', 0), v.get('height', 0)
    try:
        fps = float(Fraction(v.get('avg_frame_rate') or v.get('r_frame_rate') or '0/1'))
    except (ValueError, ZeroDivisionError):
        fps = 0
    # Perfil conservador de saída: suporte oficial 2018 + limites comuns à linha UHD.
    reasons = []
    if codec not in {'h264', 'hevc'}:
        reasons.append(f'codec {codec or "desconhecido"} fora do perfil conservador H.264/HEVC')
    if codec == 'h264' and (pix != 'yuv420p' or v.get('profile') not in {'Baseline', 'Constrained Baseline', 'Main', 'High'}):
        reasons.append('perfil de cor/compressão H.264 fora do perfil conservador')
    if codec == 'hevc' and (pix not in {'yuv420p', 'yuv420p10le'} or v.get('profile') not in {'Main', 'Main 10'}):
        reasons.append('perfil HEVC fora de Main/Main 10 4:2:0')
    if width > 3840 or height > 2160:
        reasons.append('resolução acima de 3840 × 2160')
    if fps > (30 if codec == 'h264' and (width > 1920 or height > 1080) else 60):
        reasons.append('taxa de quadros acima do perfil adotado')
    if level > (153 if codec == 'hevc' else 51):
        reasons.append('nível de compressão acima de 5.1')
    bitrate = int(v.get('bit_rate') or data.get('format', {}).get('bit_rate') or 0)
    if bitrate > 50_000_000:
        reasons.append('taxa de dados informada acima de 50 Mb/s')
    if fps == 0 or level == -1:
        notes.append('Alguns parâmetros do vídeo são desconhecidos; a análise não confirma compatibilidade.')
    convert_video = mode == 'compat' or (mode == 'auto' and bool(reasons))
    if reasons:
        notes.append('Vídeo: ' + '; '.join(reasons) + '.')
    hdr = v.get('color_transfer') in {'smpte2084', 'arib-std-b67'} or any('DOVI' in s.get('side_data_type', '') for s in v.get('side_data_list', []))
    if hdr and convert_video:
        blockers.append('Conversão de HDR/Dolby Vision exige tratamento de cores não implementado. Use “Só simplificar” para preservar o vídeo, sem garantia de compatibilidade.')
    audio_ok = a is None or (a.get('codec_name') in {'aac', 'ac3', 'eac3', 'mp3'}
                            and int(a.get('channels') or 2) <= 6
                            and int(a.get('sample_rate') or 48000) <= 48000)
    convert_audio = a is not None and (mode == 'compat' or (mode == 'auto' and not audio_ok))
    if not audio_ok:
        notes.append('Áudio fora do perfil conservador AAC/AC-3/E-AC-3/MP3 até 5.1 e 48 kHz.')
    notes.append('Vídeo: converter para H.264 SDR até 1080p/30 (com perda de qualidade).' if convert_video else 'Vídeo: copiar sem recompressão.')
    notes.append('Áudio: converter para AC-3 (Atmos não é preservado).' if convert_audio else 'Áudio: copiar sem recompressão.')
    duration = float(data.get('format', {}).get('duration') or v.get('duration') or 0)
    return Plan(str(path), v['index'], a['index'] if a else None, chosen, languages,
                convert_video, convert_audio, 6 if a and int(a.get('channels') or 2) >= 6 else 2,
                duration, notes, blockers, subtitle_sources)


def ffmpeg_args(plan, output, progress, seconds=None):
    args = [tool('ffmpeg'), '-hide_banner', '-loglevel', 'warning', '-nostdin', '-n',
            '-i', plan.source]
    mappings = []
    sources = plan.subtitle_sources or [{'path': None, 'index': index} for index in plan.subtitles]
    input_number = 0
    for source in sources:
        if source['path']:
            input_number += 1
            if source.get('encoding'):
                args += ['-sub_charenc', source['encoding']]
            args += ['-i', source['path']]
            mappings.append(f'{input_number}:{source["index"]}')
        else:
            mappings.append(f'0:{source["index"]}')
    if seconds:
        args += ['-t', str(seconds)]
    args += ['-map', f'0:{plan.video}']
    if plan.audio is not None:
        args += ['-map', f'0:{plan.audio}']
    for mapping in mappings:
        args += ['-map', mapping]
    args += ['-map_metadata', '-1', '-map_chapters', '-1']
    if plan.convert_video:
        args += ['-c:v', 'libx264', '-preset', 'medium', '-crf', '20', '-pix_fmt', 'yuv420p',
                 '-profile:v', 'high', '-level:v', '4.1', '-maxrate', '12M', '-bufsize', '24M',
                 '-vf', r'scale=w=min(1920\,iw):h=min(1080\,ih):force_original_aspect_ratio=decrease:force_divisible_by=2',
                 '-fpsmax', '30']
    else:
        args += ['-c:v', 'copy']
    if plan.convert_audio:
        args += ['-c:a', 'ac3', '-b:a', '448k', '-ar', '48000', '-ac', str(plan.channels)]
    else:
        args += ['-c:a', 'copy']
    for i, source in enumerate(sources):
        args += [f'-c:s:{i}', 'copy' if source.get('codec') in IMAGE_SUBS else 'srt']
    for i, language in enumerate(plan.subtitle_languages):
        source = sources[i]
        title = source.get('title') or ('Portugues' if language == 'pt' else 'English')
        disposition = (['default'] if i == 0 else [])
        disposition += [flag for flag in ('forced', 'hearing_impaired') if source.get(flag)]
        args += [f'-metadata:s:s:{i}', 'language=' + ('por' if language == 'pt' else 'eng'),
                 f'-metadata:s:s:{i}', 'title=' + title,
                 f'-disposition:s:{i}', '+'.join(disposition) or '0']
    args += ['-progress', str(progress), '-nostats', str(output)]
    return args


def run_job(command, log_path, cancel, progress_path=None, duration=0, callback=lambda value: None):
    with open(log_path, 'w', encoding='utf-8') as log:
        proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=log, creationflags=NO_WINDOW)
        try:
            while proc.poll() is None:
                if cancel.wait(0.2):
                    raise Cancelled('Cancelado; o original foi preservado.')
                if progress_path and duration:
                    try:
                        with open(progress_path, 'rb') as p:
                            p.seek(max(0, os.path.getsize(progress_path) - 4096))
                            matches = re.findall(rb'out_time_us=(\d+)', p.read())
                        if matches:
                            callback(min(100, int(matches[-1]) / 1_000_000 / duration * 100))
                    except OSError:
                        pass
            if cancel.is_set():
                raise Cancelled('Cancelado; o original foi preservado.')
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
    if proc.returncode:
        raise RuntimeError(Path(log_path).read_text(encoding='utf-8', errors='replace')[-3000:] or f'Falha do FFmpeg ({proc.returncode}).')


def prepare(plan, output_dir, cancel=None, callback=lambda value: None, seconds=None, verify=False):
    if plan.blockers:
        raise ValueError('\n'.join(plan.blockers))
    cancel = cancel or threading.Event()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = '-amostra-TV' if seconds else '-TV'
    stem = Path(plan.source).stem[:160] + suffix
    final = output_dir / (stem + '.mkv')
    n = 2
    while final.exists() or final.with_suffix('.relatorio.json').exists():
        final = output_dir / f'{stem}-{n}.mkv'
        n += 1
    token = uuid.uuid4().hex
    temp = output_dir / f'.{token}.parcial.mkv'
    progress = output_dir / f'.{token}.progresso.txt'
    log = output_dir / f'.{token}.log'
    check_log = output_dir / f'.{token}.verificacao.log'
    duration = min(plan.duration, seconds) if seconds and plan.duration else (seconds or plan.duration)
    required = Path(plan.source).stat().st_size * (duration / plan.duration if plan.duration else 1)
    if plan.convert_video:
        required = max(required, duration * 12_448_000 / 8)
    if shutil.disk_usage(output_dir).free < required * 1.10 + 32 * 1024**2:
        raise RuntimeError('Espaço livre insuficiente para a estimativa de saída. Escolha outra pasta.')
    try:
        if cancel.is_set():
            raise Cancelled()
        run_job(ffmpeg_args(plan, temp, progress, seconds), log, cancel, progress, duration, callback)
        output_probe = probe(temp)
        output_streams = output_probe.get('streams', [])
        if len([s for s in output_streams if s['codec_type'] == 'subtitle']) != len(plan.subtitles):
            raise RuntimeError('A verificação da saída encontrou uma quantidade inesperada de legendas.')
        actual = float(output_probe.get('format', {}).get('duration') or 0)
        if duration and abs(actual - duration) > 3:
            raise RuntimeError('Duração da saída diferente da esperada; arquivo não liberado como concluído.')
        verification = 'Estrutura e duração verificadas. Reprodução na TV ainda não verificada.'
        if verify:
            callback(-1)
            run_job([tool('ffmpeg'), '-hide_banner', '-v', 'error', '-xerror', '-nostdin', '-i', str(temp),
                     '-map', '0:v:0', '-map', '0:a:0?', '-f', 'null', '-'], check_log, cancel)
            if check_log.exists() and check_log.stat().st_size:
                raise RuntimeError('Erros na verificação de decodificação: ' + check_log.read_text(errors='replace')[-2000:])
            verification = 'Decodificação completa da saída sem erros reportados. Reprodução na TV ainda não verificada.'
        if cancel.is_set():
            raise Cancelled()
        report = asdict(plan) | {'output': str(final), 'verification': verification,
                                 'sample_seconds': seconds, 'warnings_ffmpeg': log.read_text(encoding='utf-8', errors='replace'),
                                 'created_local': time.strftime('%Y-%m-%d %H:%M:%S')}
        # No Windows, rename falha se o destino já existir: não sobrescrever saídas concorrentes.
        temp.rename(final)
        try:
            with final.with_suffix('.relatorio.json').open('x', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            raise RuntimeError(f'Vídeo salvo em {final}, mas não foi possível gravar o relatório: {exc}')
        callback(100)
        return final
    finally:
        for artifact in (temp, progress, log, check_log):
            artifact.unlink(missing_ok=True)
