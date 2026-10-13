import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from engine import analyze, prepare, probe, tool, Cancelled, NO_WINDOW, content_language, DEFAULT_OUTPUT_DIR


def media(subs=None, **video):
    return {'format': {'duration': '3', 'bit_rate': '1500000'}, 'streams': [
        {'index': 0, 'codec_type': 'video', 'codec_name': 'h264', 'profile': 'High', 'pix_fmt': 'yuv420p',
         'width': 1280, 'height': 720, 'level': 32, 'avg_frame_rate': '24000/1001', **video},
        {'index': 1, 'codec_type': 'audio', 'codec_name': 'eac3', 'channels': 6, 'sample_rate': '48000'},
        *(subs or [])]}


def subtitle(i, language, title='', forced=0, codec='subrip'):
    return {'index': i, 'codec_type': 'subtitle', 'codec_name': codec,
            'tags': {'language': language, 'title': title}, 'disposition': {'forced': forced}}


class SelectionTests(unittest.TestCase):
    def test_brazil_and_regular_english(self):
        data = media([subtitle(2, 'por', 'Portuguese'), subtitle(3, 'eng', 'SDH'),
                      subtitle(4, 'por', 'Portuguese (Brazil)'), subtitle(5, 'eng'), subtitle(6, 'spa')])
        plan = analyze('video.mkv', data)
        self.assertEqual(plan.subtitles, [4, 5])
        self.assertFalse(plan.convert_video)
        self.assertFalse(plan.convert_audio)

    def test_full_subtitle_preferred_over_forced_brazil(self):
        plan = analyze('video.mkv', media([subtitle(2, 'pt-br', forced=1), subtitle(3, 'por')]))
        self.assertEqual(plan.subtitles, [3])

    def test_no_guessed_languages_or_image_conversion(self):
        plan = analyze('video.mkv', media([subtitle(2, 'und'), subtitle(3, 'eng', codec='hdmv_pgs_subtitle')]))
        self.assertEqual(plan.subtitles, [])
        self.assertTrue(any('imagem' in n for n in plan.notes))

    def test_hevc_main10_level5_is_kept(self):
        plan = analyze('video.mkv', media(codec_name='hevc', pix_fmt='yuv420p10le', profile='Main 10', level=150, width=2160, height=1080))
        self.assertFalse(plan.convert_video)

    def test_hdr_conversion_is_blocked(self):
        plan = analyze('video.mkv', media(codec_name='av1', color_transfer='smpte2084'))
        self.assertTrue(plan.blockers)

    def test_unsupported_audio_only(self):
        data = media()
        data['streams'][1]['codec_name'] = 'dts'
        plan = analyze('video.mkv', data)
        self.assertTrue(plan.convert_audio)
        self.assertFalse(plan.convert_video)


class IntegrationTests(unittest.TestCase):
    def test_external_subtitles_and_encoding(self):
        with tempfile.TemporaryDirectory(prefix='prepara-tv-subs-', dir=Path(__file__).parent) as tmp:
            root = Path(tmp)
            source = root / 'filme.mp4'
            subprocess.run([tool('ffmpeg'), '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
                            'testsrc2=size=320x180:rate=24', '-t', '3', '-c:v', 'libx264', str(source)],
                           check=True, creationflags=NO_WINDOW)
            before = source.read_bytes()
            (root / 'filme.srt').write_text('1\n00:00:00,100 --> 00:00:02,000\n'
                                           'You are here with me. I know that this is what you want.\n', encoding='utf-8')
            subs = root / 'Subs' / 'Portuguese'
            subs.mkdir(parents=True)
            (subs / 'filme.pt-BR.srt').write_bytes('1\n00:00:00,100 --> 00:00:02,000\n'
                                                'Você não está só. Ação e coração.\n'.encode('cp1252'))
            (subs / 'filme.pt.srt').write_text('1\n00:00:00,100 --> 00:00:02,000\nOutra versão.\n', encoding='utf-8')
            (root / 'Subs' / '2_English.srt').write_text('1\n00:00:00,100 --> 00:00:02,000\nOther version.\n', encoding='utf-8')
            plan = analyze(source, probe(source))
            self.assertEqual(plan.subtitle_languages, ['pt', 'en'])
            self.assertTrue(plan.subtitle_sources[0]['path'].endswith('filme.pt-BR.srt'))
            self.assertTrue(plan.subtitle_sources[1]['path'].endswith('filme.srt'))
            self.assertFalse(plan.convert_video)
            output = prepare(plan, root / 'out', verify=True)
            streams = [s for s in probe(output)['streams'] if s['codec_type'] == 'subtitle']
            self.assertEqual([s['tags']['language'] for s in streams], ['por', 'eng'])
            pt = subprocess.check_output([tool('ffmpeg'), '-v', 'error', '-i', str(output),
                                          '-map', '0:s:0', '-f', 'srt', '-'], creationflags=NO_WINDOW).decode('utf-8')
            self.assertIn('Você não está só. Ação e coração.', pt)
            en = subprocess.check_output([tool('ffmpeg'), '-v', 'error', '-i', str(output),
                                          '-map', '0:s:1', '-f', 'srt', '-'], creationflags=NO_WINDOW).decode('utf-8')
            self.assertIn('You are here with me.', en)
            self.assertEqual(source.read_bytes(), before)

    def test_internal_and_external_selection_and_ambiguous_folder(self):
        with tempfile.TemporaryDirectory(prefix='prepara-tv-select-', dir=Path(__file__).parent) as tmp:
            root = Path(tmp)
            source = root / 'filme.mp4'
            source.touch()
            (root / 'outro.mp4').touch()
            (root / 'sub').mkdir()
            text = '1\n00:00:00,100 --> 00:00:02,000\nVocê está aqui.\n'
            for name in ('filme.pt-BR.srt', 'outro.pt-BR.srt', 'Portuguese.srt', 'filme.forced.pt-BR.srt'):
                (root / 'sub' / name).write_text(text, encoding='utf-8')
            data = media([subtitle(2, 'por'), subtitle(3, 'eng')])
            plan = analyze(source, data)
            self.assertEqual(plan.subtitle_languages, ['pt', 'en'])
            self.assertTrue(plan.subtitle_sources[0]['path'].endswith('filme.pt-BR.srt'))
            self.assertIsNone(plan.subtitle_sources[1]['path'])
            self.assertEqual(plan.subtitle_sources[1]['index'], 3)
            self.assertTrue(any('associação ambígua' in note for note in plan.notes))

    def test_language_confidence(self):
        self.assertEqual(content_language('You are here with me. I know that this is what you want.'), 'en')
        self.assertEqual(content_language('Você não está aqui comigo. Eu não sei o que você quer, mas vamos para minha casa.'), 'pt')
        self.assertEqual(content_language('Hola, yo estoy aqui con usted. Muchas gracias pero no tengo eso para ustedes.'), '')
        self.assertEqual(content_language('OK.'), '')
        self.assertEqual(str(DEFAULT_OUTPUT_DIR), r'C:\Users\Pena\Documents\Torrents\Preparados-TV')

    def test_real_remux_conversion_and_cancel(self):
        # TemporaryDirectory is solely the newly created test workspace.
        with tempfile.TemporaryDirectory(prefix='prepara-tv-test-', dir=Path(__file__).parent) as tmp:
            root = Path(tmp)
            srt = root / 'sub.srt'
            srt.write_text('1\n00:00:00,100 --> 00:00:02,000\nTeste de legenda\n', encoding='utf-8')
            source = root / 'entrada [teste].mkv'
            subprocess.run([tool('ffmpeg'), '-v', 'error', '-nostdin', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=24',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-i', str(srt), '-t', '3',
                            '-map', '0:v', '-map', '1:a', '-map', '2:s', '-map', '2:s', '-map', '2:s',
                            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'eac3', '-c:s', 'srt',
                            '-metadata:s:s:0', 'language=por', '-metadata:s:s:0', 'title=Portuguese (Brazil)',
                            '-metadata:s:s:1', 'language=eng', '-metadata:s:s:2', 'language=spa', str(source)],
                           check=True, creationflags=NO_WINDOW)
            before = source.read_bytes()
            plan = analyze(source, probe(source))
            first = prepare(plan, root / 'out', verify=True)
            second = prepare(plan, root / 'out')
            self.assertNotEqual(first, second)
            data = probe(first)
            self.assertEqual(len([s for s in data['streams'] if s['codec_type'] == 'subtitle']), 2)
            def av_hash(path):
                return subprocess.check_output([tool('ffmpeg'), '-v', 'error', '-i', str(path), '-map', '0:v:0',
                                                '-map', '0:a:0', '-c', 'copy', '-f', 'streamhash', '-'], creationflags=NO_WINDOW)
            self.assertEqual(av_hash(source), av_hash(first))
            compat = prepare(analyze(source, probe(source), mode='compat'), root / 'compat', verify=True)
            self.assertEqual(probe(compat)['streams'][1]['codec_name'], 'ac3')
            event = threading.Event()
            event.set()
            with self.assertRaises(Cancelled):
                prepare(plan, root / 'cancel', cancel=event)
            self.assertEqual(list((root / 'cancel').iterdir()), [])
            self.assertEqual(source.read_bytes(), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
