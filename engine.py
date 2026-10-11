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
from dataclasses import dataclass, asdict
from fractions import Fraction

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
TEXT_SUBS = {'subrip', 'ass', 'ssa', 'mov_text', 'webvtt', 'text'}


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
    subs = [s for s in streams if s.get('codec_type') == 'subtitle']
    for language, label in [('pt', 'português'), ('en', 'inglês')]:
        candidates = [s for s in subs if lang(s) == language]
        if not candidates:
            notes.append(f'Não há legenda identificada em {label}; não será criada nem traduzida.')
            continue
        selected = max(candidates, key=lambda s: subtitle_score(s, language))
        if selected.get('codec_name') not in TEXT_SUBS:
            notes.append(f'Legenda em {label} é uma imagem e será omitida (esta versão não faz OCR).')
            continue
        chosen.append(selected['index'])
        languages.append(language)
        if selected.get('codec_name') != 'subrip':
            notes.append(f'Legenda em {label} será simplificada para SRT, sem estilos avançados.')
        if selected.get('disposition', {}).get('forced'):
            notes.append(f'A única legenda selecionada em {label} é parcial/forçada.')
    notes.append(f'Legendas: {len(subs)} no original → {len(chosen)} na cópia (até uma PT e uma EN).')
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
                duration, notes, blockers)


def ffmpeg_args(plan, output, progress, seconds=None):
    args = [tool('ffmpeg'), '-hide_banner', '-loglevel', 'warning', '-nostdin', '-n',
            '-i', plan.source]
    if seconds:
        args += ['-t', str(seconds)]
    args += ['-map', f'0:{plan.video}']
    if plan.audio is not None:
        args += ['-map', f'0:{plan.audio}']
    for index in plan.subtitles:
        args += ['-map', f'0:{index}']
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
    args += ['-c:s', 'srt']
    for i, language in enumerate(plan.subtitle_languages):
        args += [f'-metadata:s:s:{i}', 'language=' + ('por' if language == 'pt' else 'eng'),
                 f'-metadata:s:s:{i}', 'title=' + ('Portugues' if language == 'pt' else 'English'),
                 f'-disposition:s:{i}', 'default' if i == 0 else '0']
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
