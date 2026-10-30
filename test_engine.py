import json
from pathlib import Path
import subprocess
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from engine import analyze, prepare, probe, tool, Cancelled, NO_WINDOW, content_language, DEFAULT_OUTPUT_DIR, ffmpeg_args


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
        self.assertEqual(plan.subtitles, [4, 5, 3])
        self.assertFalse(plan.convert_video)
        self.assertFalse(plan.convert_audio)

    def test_full_subtitle_preferred_over_forced_brazil(self):
        plan = analyze('video.mkv', media([subtitle(2, 'pt-br', forced=1), subtitle(3, 'por')]))
        self.assertEqual(plan.subtitles, [3])

    def test_no_guessed_languages_but_keep_images(self):
        plan = analyze('video.mkv', media([subtitle(2, 'und'), subtitle(3, 'eng', codec='hdmv_pgs_subtitle')]))
        self.assertEqual(plan.subtitles, [3])
        self.assertTrue(any('copiada diretamente' in n for n in plan.notes))
        self.assertFalse(any('simplificada' in n for n in plan.notes))

    def test_mixed_codecs_all_english_and_only_one_portuguese(self):
        for pt_codec, en_codec in [('hdmv_pgs_subtitle', 'dvd_subtitle'), ('dvd_subtitle', 'hdmv_pgs_subtitle')]:
            with self.subTest(pt_codec=pt_codec):
                data = media([subtitle(2, 'por', codec=pt_codec), subtitle(3, 'pt-br', forced=1),
                              subtitle(4, 'eng', 'English SDH', codec=en_codec),
                              subtitle(5, 'en', 'English full'),
                              subtitle(6, 'eng', 'English forced', forced=1, codec=en_codec),
                              subtitle(7, 'spa', codec=pt_codec), subtitle(8, 'und', codec=en_codec)])
                data['streams'][4]['disposition']['hearing_impaired'] = 1
                plan = analyze('video.mkv', data, mode='copy')
                self.assertEqual(plan.subtitles, [2, 5, 4, 6])
                self.assertEqual(plan.subtitle_languages, ['pt', 'en', 'en', 'en'])
                with patch('engine.tool', return_value='ffmpeg'):
                    args = ffmpeg_args(plan, 'out.mkv', 'progress.txt')
                for option in ('-c:v', '-c:a', '-c:s:0', '-c:s:2', '-c:s:3'):
                    self.assertEqual(args[args.index(option) + 1], 'copy')
                self.assertEqual(args[args.index('-c:s:1') + 1], 'srt')
                self.assertEqual(args[args.index('-disposition:s:2') + 1], 'hearing_impaired')
                self.assertEqual(args[args.index('-disposition:s:3') + 1], 'forced')
                self.assertIn('title=English SDH', args)
                self.assertEqual([args[i+1] for i, value in enumerate(args) if value == '-map'],
                                 ['0:0', '0:1', '0:2', '0:5', '0:4', '0:6'])

    def test_unsupported_codec_does_not_hide_supported_forced_track(self):
        plan = analyze('video.mkv', media([subtitle(2, 'por', codec='unknown'),
                                         subtitle(3, 'por', forced=1, codec='dvd_subtitle')]))
        self.assertEqual(plan.subtitles, [3])

    def test_copy_mode_keeps_nonstandard_video_and_audio(self):
        data = media([subtitle(2, 'eng', codec='hdmv_pgs_subtitle')], codec_name='av1')
        data['streams'][1]['codec_name'] = 'dts'
        plan = analyze('video.mkv', data, mode='copy')
        self.assertFalse(plan.convert_video)
        self.assertFalse(plan.convert_audio)

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
    def test_real_pgs_and_vobsub_copy(self):
        with tempfile.TemporaryDirectory(prefix='prepara-tv-image-', dir=Path(__file__).parent) as tmp:
            root = Path(tmp)
            # Synthetic 8x8 white bitmap, visible from 0.1 to 2 seconds.
            def segment(pts, kind, payload=b''):
                return b'PG' + struct.pack('>IIBH', pts, 0, kind, len(payload)) + payload
            def composition(number, count):
                return struct.pack('>HHBHBBBB', 320, 180, 0x10, number, 0x80 if number == 0 else 0,
                                   0, 0, count)
            rle = (bytes([1]) * 8 + b'\x00\x00') * 8
            sup = root / 'bitmap.sup'
            sup.write_bytes(
                segment(9000, 0x16, composition(0, 1) + struct.pack('>HBBHH', 0, 0, 0, 100, 100)) +
                segment(9000, 0x17, struct.pack('>BBHHHH', 1, 0, 100, 100, 8, 8)) +
                segment(9000, 0x14, bytes([0, 0, 0, 16, 128, 128, 0, 1, 235, 128, 128, 255])) +
                segment(9000, 0x15, struct.pack('>HBB', 0, 0, 0xc0) + (4 + len(rle)).to_bytes(3, 'big') +
                        struct.pack('>HH', 8, 8) + rle) + segment(9000, 0x80) +
                segment(180000, 0x16, composition(1, 0)) + segment(180000, 0x80))
            source = root / 'images.mkv'
            subprocess.run([tool('ffmpeg'), '-v', 'error', '-nostdin',
                            '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=24',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-i', str(sup),
                            '-t', '3', '-map', '0:v', '-map', '1:a',
                            '-map', '2:s', '-map', '2:s', '-map', '2:s', '-map', '2:s', '-map', '2:s',
                            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'eac3', '-c:s', 'copy',
                            '-c:s:1', 'dvdsub', '-c:s:3', 'dvdsub',
                            '-metadata:s:s:0', 'language=por', '-metadata:s:s:1', 'language=eng',
                            '-metadata:s:s:1', 'title=English SDH', '-disposition:s:1', 'hearing_impaired',
                            '-metadata:s:s:2', 'language=eng', '-metadata:s:s:2', 'title=English full',
                            '-metadata:s:s:3', 'language=por', '-disposition:s:3', 'forced',
                            '-metadata:s:s:4', 'language=eng', '-metadata:s:s:4', 'title=English forced',
                            '-disposition:s:4', 'forced', str(source)], check=True, creationflags=NO_WINDOW)
            before = source.read_bytes()
            plan = analyze(source, probe(source), mode='copy')
            self.assertEqual(plan.subtitles, [2, 4, 3, 6])
            output = prepare(plan, root / 'out', verify=True)
            streams = probe(output)['streams']
            subs = [s for s in streams if s['codec_type'] == 'subtitle']
            self.assertEqual([s['codec_name'] for s in subs],
                             ['hdmv_pgs_subtitle', 'hdmv_pgs_subtitle', 'dvd_subtitle', 'hdmv_pgs_subtitle'])
            self.assertEqual([s['tags']['language'] for s in subs], ['por', 'eng', 'eng', 'eng'])
            self.assertEqual(subs[1]['tags']['title'], 'English full')
            self.assertEqual(subs[2]['disposition']['hearing_impaired'], 1)
            self.assertEqual(subs[3]['disposition']['forced'], 1)
            def packet_hashes(path, index):
                raw = subprocess.check_output([tool('ffprobe'), '-v', 'error', '-select_streams', str(index),
                                               '-show_packets', '-show_data_hash', 'sha256',
                                               '-show_entries', 'packet=data_hash', '-of', 'json', str(path)],
                                              creationflags=NO_WINDOW)
                return [p['data_hash'] for p in json.loads(raw)['packets']]
            # Verify compressed audio/video and every selected bitmap packet byte-for-byte.
            for original, copied in zip([plan.video, plan.audio, *plan.subtitles], streams):
                expected = packet_hashes(source, original)
                self.assertTrue(expected)
                self.assertEqual(expected, packet_hashes(output, copied['index']))
            self.assertEqual(source.read_bytes(), before)

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
            self.assertEqual(plan.subtitle_languages, ['pt', 'en', 'en'])
            self.assertTrue(plan.subtitle_sources[0]['path'].endswith('filme.pt-BR.srt'))
            self.assertTrue(plan.subtitle_sources[1]['path'].endswith('filme.srt'))
            self.assertFalse(plan.convert_video)
            output = prepare(plan, root / 'out', verify=True)
            streams = [s for s in probe(output)['streams'] if s['codec_type'] == 'subtitle']
            self.assertEqual([s['tags']['language'] for s in streams], ['por', 'eng', 'eng'])
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
