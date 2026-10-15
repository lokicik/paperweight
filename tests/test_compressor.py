"""Compression keeps text, links, outlines and the original; it reports what it changed."""
import hashlib
import io
from pathlib import Path
import random
import zipfile
import tempfile
import unittest

from PIL import Image

from paperweight import compressor
from paperweight.analyzer import fitz, inspect_pdf


def hatching(page, label):
    """Dense vector line work: expensive as drawing commands, cheap as an image."""
    shape = page.new_shape()
    for row in range(120, 700, 4):
        for col in range(60, 520, 2):
            shape.draw_line((col, row), (col + 1.6, row))
    shape.finish(color=(0.1, 0.3, 0.6), width=0.4)
    shape.commit()
    page.insert_text((72, 80), f'{label}: caption stays selectable', fontsize=12)
    page.insert_text((300, 400), 'Label inside the drawing', fontsize=9)


class CompressorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.source = Path(cls.temp.name) / 'book.pdf'
        with fitz.open() as doc, fitz.open() as other:
            hatching(doc.new_page(), 'Figure 1')
            hatching(doc.new_page(), 'Figure 2')
            hatching(other.new_page(), 'Reusable artwork')
            doc.new_page().show_pdf_page(fitz.Rect(0, 0, 595, 842), other, 0)
            rotated = doc.new_page()
            hatching(rotated, 'Figure 4')
            rotated.set_rotation(90)
            doc.new_page().insert_text((72, 72), 'Plain text page')
            doc[0].insert_link({'kind': fitz.LINK_GOTO, 'from': fitz.Rect(72, 60, 200, 85), 'page': 2})
            doc.set_toc([[1, 'Figures', 1], [2, 'Rotated', 4]])
            doc.save(cls.source)
        cls.digest = hashlib.sha256(cls.source.read_bytes()).digest()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_compression(self, name, options):
        output = Path(self.temp.name) / f'{name}.pdf'
        report = compressor.compress(self.source, output, Path(self.temp.name) / name, options, workers=1)
        self.assertEqual(self.digest, hashlib.sha256(self.source.read_bytes()).digest())
        return output, report

    def test_quality_mode_converts_heavy_pages_and_keeps_text(self):
        output, report = self.run_compression('quality', {'mode': 'quality', 'preset': 'high'})
        self.assertEqual(report['bytes'], output.stat().st_size)
        self.assertLess(report['bytes'], report['originalBytes'] * 0.8)
        self.assertEqual([r['page'] for r in report['converted']], [1, 2, 4])
        self.assertIn('form_text', {k['reason'] for k in report['kept']})
        self.assertTrue(report['checks']['passed'])
        with fitz.open(self.source) as a, fitz.open(output) as b:
            for pa, pb in zip(a, b):
                self.assertEqual(pa.get_text(), pb.get_text())
                self.assertEqual(pa.rotation, pb.rotation)
            self.assertEqual(a.get_toc(), b.get_toc())
            self.assertEqual(b[0].get_links()[0]['page'], 2)
            # Converted pages draw one image under the original text.
            self.assertEqual(len(b[0].get_images()), 1)
            self.assertFalse(b[0].get_drawings())
            # Untouched pages keep their vector artwork.
            self.assertTrue(b[2].get_drawings() or b[2].get_xobjects())

    def test_target_met_without_lossy_changes(self):
        output, report = self.run_compression('target', {'mode': 'target', 'preset': 'compact', 'targetBytes': 10**9})
        self.assertTrue(report['targetMet'])
        self.assertEqual(report['converted'], [])
        with fitz.open(output) as doc:
            self.assertTrue(doc[0].get_drawings())

    def test_target_redraws_only_the_pages_it_needs(self):
        _, full = self.run_compression('all', {'mode': 'quality', 'preset': 'high'})
        target = (full['losslessBytes'] + full['bytes']) // 2
        _, report = self.run_compression('some', {'mode': 'target', 'preset': 'compact', 'targetBytes': target})
        self.assertTrue(report['targetMet'])
        self.assertEqual(report['preset'], 'high')
        self.assertLess(0, len(report['converted']))
        self.assertLess(len(report['converted']), len(full['converted']))

    def test_unreachable_target_is_reported(self):
        _, report = self.run_compression('tiny', {'mode': 'target', 'preset': 'high', 'targetBytes': 1000})
        self.assertFalse(report['targetMet'])
        self.assertTrue(report['converted'])
        self.assertTrue(report['checks']['passed'])

    def test_few_color_images_become_indexed_without_pixel_changes(self):
        source = Path(self.temp.name) / 'scan.pdf'
        rnd = random.Random(7)
        colors = [bytes(c) for c in ((255, 255, 255), (32, 28, 32), (85, 154, 219), (251, 223, 219))]
        samples = b''.join(rnd.choice(colors) for _ in range(400 * 500))
        pix = fitz.Pixmap(fitz.csRGB, 400, 500, samples, False)
        with fitz.open() as doc:
            doc.new_page().insert_image(fitz.Rect(0, 0, 595, 842), pixmap=pix)
            doc.save(source, deflate=True)
        output = Path(self.temp.name) / 'scan-out.pdf'
        report = compressor.compress(source, output, Path(self.temp.name) / 'scan', {'mode': 'target', 'preset': 'high', 'targetBytes': 10**9}, workers=1)
        self.assertEqual(report['lossless']['imagesIndexed'], 1)
        with fitz.open(source) as a, fitz.open(output) as b:
            before, after = a[0].get_images()[0][0], b[0].get_images()[0][0]
            self.assertIn('/Indexed', b.xref_get_key(after, 'ColorSpace')[1])
            self.assertEqual(fitz.Pixmap(a, before).samples, fitz.Pixmap(b, after).samples)
        self.assertLess(report['bytes'], source.stat().st_size)

    def test_photos_shown_below_the_preset_resolution_are_reencoded_at_their_own(self):
        source = Path(self.temp.name) / 'photos.pdf'
        rnd = random.Random(5)
        photo = Image.new('RGB', (600, 850))
        photo.putdata([(x * 255 // 600 + rnd.randrange(4), y * 255 // 850, 128 + rnd.randrange(4)) for y in range(850) for x in range(600)])
        jpeg = io.BytesIO()
        photo.save(jpeg, 'JPEG', quality=98)
        with fitz.open() as doc:
            page = doc.new_page()
            page.insert_image(page.rect, stream=jpeg.getvalue())
            page.insert_text((72, 72), 'Caption stays selectable', fontsize=12)
            doc.save(source)
        output = Path(self.temp.name) / 'photos-out.pdf'
        report = compressor.compress(source, output, Path(self.temp.name) / 'photos', {'mode': 'quality', 'preset': 'high'}, workers=1)
        self.assertEqual([p['change'] for p in report['converted']], ['images'])
        self.assertTrue(report['checks']['passed'])
        self.assertLess(report['bytes'], source.stat().st_size * 0.85)
        with fitz.open(source) as a, fitz.open(output) as b:
            self.assertEqual(a[0].get_text(), b[0].get_text())
            before, after = (Image.frombytes('RGB', (p.width, p.height), p.samples) for p in (fitz.Pixmap(a, a[0].get_images()[0][0]), fitz.Pixmap(b, b[0].get_images()[0][0])))
            self.assertEqual(before.size, after.size)
            self.assertLess(compressor.mse(before, after), 20)

    def test_unusual_images_keep_their_look(self):
        source = Path(self.temp.name) / 'unusual.pdf'
        rnd = random.Random(9)
        photo = Image.new('RGB', (500, 400))
        photo.putdata([(x // 2 + rnd.randrange(3), y // 2, 200 - x // 4) for y in range(400) for x in range(500)])
        alpha = photo.copy().convert('RGBA')
        alpha.putalpha(Image.new('L', (500, 400), 180))
        png, cmyk, shared = io.BytesIO(), io.BytesIO(), io.BytesIO()
        alpha.save(png, 'PNG')
        photo.convert('CMYK').save(cmyk, 'JPEG', quality=97)
        photo.save(shared, 'JPEG', quality=98)
        with fitz.open() as doc:
            page = doc.new_page()
            page.insert_image(page.rect, stream=png.getvalue())  # with a soft mask
            page.insert_text((72, 72), 'Transparent photo', fontsize=12)
            page = doc.new_page()
            page.insert_image(page.rect, stream=cmyk.getvalue())
            for label in ('Shared photo one', 'Shared photo two'):
                page = doc.new_page()
                page.insert_image(page.rect, stream=shared.getvalue())
                page.insert_text((72, 72), label, fontsize=12)
            page = doc.new_page()
            page.insert_image(fitz.Rect(50, 50, 545, 450), stream=png.getvalue())
            page.set_rotation(90)
            doc.save(source)
        output = Path(self.temp.name) / 'unusual-out.pdf'
        report = compressor.compress(source, output, Path(self.temp.name) / 'unusual', {'mode': 'quality', 'preset': 'high'}, workers=1)
        self.assertTrue(report['checks']['passed'])
        with fitz.open(source) as a, fitz.open(output) as b:
            for pa, pb in zip(a, b):
                self.assertEqual(pa.get_text(), pb.get_text())
                self.assertEqual(pa.rotation, pb.rotation)
                before, after = (Image.frombytes('RGB', (p.width, p.height), p.samples) for p in (pa.get_pixmap(dpi=100), pb.get_pixmap(dpi=100)))
                self.assertLess(compressor.mse(before, after), 40, f'page {pa.number + 1}')
            # An image two pages share is never re-encoded for one of them.
            self.assertEqual(b[2].get_images()[0][0], b[3].get_images()[0][0])

    def test_page_check_notices_a_missing_image(self):
        source, copy = Path(self.temp.name) / 'look.pdf', Path(self.temp.name) / 'look-copy.pdf'
        photo, png = Image.new('RGB', (300, 300), (40, 90, 150)), io.BytesIO()
        photo.save(png, 'PNG')
        for path, with_image in ((source, True), (copy, False)):
            with fitz.open() as doc:
                page = doc.new_page()
                if with_image:
                    page.insert_image(fitz.Rect(100, 100, 400, 400), stream=png.getvalue())
                page.insert_text((72, 72), 'Same text on both', fontsize=12)
                doc.save(path)
        checks = compressor.validate(source, copy, lambda *a: None, lambda: None)
        self.assertEqual(checks['textDifferences'], [])
        self.assertEqual(checks['lookDifferences'], [1])
        self.assertFalse(checks['passed'])
        self.assertTrue(compressor.validate(source, source, lambda *a: None, lambda: None)['passed'])

    def test_redrawn_pages_drop_resources_only_the_old_drawing_used(self):
        source = Path(self.temp.name) / 'resources.pdf'
        with fitz.open() as doc:
            page = doc.new_page()
            shape = page.new_shape()
            for row in range(120, 700, 4):
                for col in range(60, 520, 2):
                    shape.draw_line((col, row), (col + 1.6, row))
            # Transparency gives the drawing an ExtGState of its own.
            shape.finish(color=(0.1, 0.3, 0.6), width=0.4, stroke_opacity=0.5)
            shape.commit()
            page.insert_text((72, 80), 'Caption stays selectable', fontsize=12)
            doc.save(source)
        with fitz.open(source) as doc:
            self.assertEqual(doc.xref_get_key(doc[0].xref, 'Resources/ExtGState')[0], 'dict')
        output = Path(self.temp.name) / 'resources-out.pdf'
        report = compressor.compress(source, output, Path(self.temp.name) / 'resources', {'mode': 'quality', 'preset': 'high'}, workers=1)
        self.assertEqual([p['change'] for p in report['converted']], ['redrawn'])
        with fitz.open(output) as doc:
            page = doc[0]
            resources = int(doc.xref_get_key(page.xref, 'Resources')[1].split()[0])
            self.assertEqual(doc.xref_get_key(resources, 'ExtGState')[0], 'null')
            self.assertTrue(page.get_fonts())
            self.assertEqual(page.get_text().strip(), 'Caption stays selectable')

    def test_lossy_palette_stays_within_the_error_budget(self):
        image = Image.new('RGB', (256, 256))
        image.putdata([(x, y, (x + y) // 2) for y in range(256) for x in range(256)])
        self.assertIsNone(compressor.palette_png(image, 2))
        data, error = compressor.palette_png(image, 30)
        decoded = Image.open(io.BytesIO(data))
        self.assertEqual(decoded.mode, 'P')
        self.assertLessEqual(error, 30)
        self.assertAlmostEqual(compressor.mse(image, decoded.convert('RGB')), error, places=6)

    def test_photos_gain_png_predictors_without_pixel_changes(self):
        source = Path(self.temp.name) / 'photo.pdf'
        rnd = random.Random(3)
        samples = bytes(v for y in range(300) for x in range(400)
                        for v in ((x + rnd.randrange(3)) % 256, (y + rnd.randrange(3)) % 256, (x + y) // 3 % 256))
        pix = fitz.Pixmap(fitz.csRGB, 400, 300, samples, False)
        with fitz.open() as doc:
            doc.new_page().insert_image(fitz.Rect(0, 0, 400, 300), pixmap=pix)
            doc.save(source, deflate=True)
        output = Path(self.temp.name) / 'photo-out.pdf'
        report = compressor.compress(source, output, Path(self.temp.name) / 'photo', {'mode': 'quality', 'preset': 'lossless'}, workers=1)
        with fitz.open(source) as a, fitz.open(output) as b:
            before, after = a[0].get_images()[0][0], b[0].get_images()[0][0]
            self.assertIn('/Predictor 15', b.xref_get_key(after, 'DecodeParms')[1])
            self.assertEqual(fitz.Pixmap(a, before).samples, fitz.Pixmap(b, after).samples)
        self.assertLess(report['bytes'], source.stat().st_size * 0.8)

    def test_lossless_cleanup_removes_chosen_extras_only(self):
        source = Path(self.temp.name) / 'extras.pdf'
        with fitz.open(self.source) as doc:
            page = doc[0]
            thumb = doc.get_new_xref()
            doc.update_object(thumb, '<</Type/XObject/Subtype/Image/Width 64/Height 64/ColorSpace/DeviceGray/BitsPerComponent 8>>')
            doc.update_stream(thumb, bytes(range(256)) * 16)
            doc.xref_set_key(page.xref, 'Thumb', f'{thumb} 0 R')
            private = doc.get_new_xref()
            doc.update_object(private, '<<>>')
            doc.update_stream(private, b'illustrator editing data ' * 2000)
            doc.xref_set_key(page.xref, 'PieceInfo', f'<</Illustrator<</LastModified(D:2026)/Private {private} 0 R>>>>')
            doc.embfile_add('notes.txt', b'attached notes ' * 2000)
            doc.set_xml_metadata('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/></x:xmpmeta>')
            doc.save(source)
        report = inspect_pdf(source)
        self.assertEqual({r['id'] for r in report['removable']}, {'editing', 'thumbnails', 'attachments', 'docmeta'})
        self.assertTrue(all(r['bytes'] > 0 for r in report['removable']))
        output = Path(self.temp.name) / 'extras-out.pdf'
        result = compressor.compress(source, output, Path(self.temp.name) / 'extras', {
            'mode': 'quality', 'preset': 'lossless', 'remove': ['editing', 'thumbnails', 'attachments']}, workers=1)
        self.assertEqual(result['converted'], [])
        self.assertTrue(result['checks']['passed'])
        with fitz.open(output) as doc:
            self.assertEqual(doc.embfile_count(), 0)
            self.assertEqual(doc.xref_get_key(doc[0].xref, 'Thumb')[0], 'null')
            self.assertEqual(doc.xref_get_key(doc[0].xref, 'PieceInfo')[0], 'null')
            self.assertNotEqual(doc.xref_get_key(doc.pdf_catalog(), 'Metadata')[0], 'null')
            self.assertTrue(doc[0].get_drawings())

    def test_split_when_the_limit_cannot_be_met(self):
        limit = self.source.stat().st_size * 6 // 10
        output, report = self.run_compression('split', {'mode': 'target', 'preset': 'lossless', 'targetBytes': limit, 'split': True, 'name': 'book.pdf'})
        self.assertFalse(report['targetMet'])
        parts = report['parts']
        self.assertGreaterEqual(len(parts), 2)
        self.assertTrue(all(p['fits'] and p['bytes'] <= limit for p in parts))
        self.assertEqual([p['firstPage'] for p in parts], [1] + [p['lastPage'] + 1 for p in parts[:-1]])
        self.assertEqual(parts[-1]['lastPage'], 5)
        with zipfile.ZipFile(output.with_suffix('.zip')) as bundle:
            self.assertEqual(bundle.namelist(), [p['name'] for p in parts])
            last = fitz.open(stream=bundle.read(parts[-1]['name']), filetype='pdf')
        with fitz.open(self.source) as original:
            for offset, page in enumerate(last):
                self.assertEqual(page.get_text(), original[parts[-1]['firstPage'] - 1 + offset].get_text())
            if parts[-1]['firstPage'] <= 4:
                self.assertIn('Rotated', [entry[1] for entry in last.get_toc()])

    def test_kept_pages_are_never_redrawn(self):
        _, report = self.run_compression('keep', {'mode': 'quality', 'preset': 'high', 'keepPages': [1]})
        self.assertEqual([r['page'] for r in report['converted']], [2, 4])
        self.assertIn({'reason': 'excluded', 'label': 'You chose to keep', 'pages': 1}, report['kept'])

    def test_rejects_password_protected_input(self):
        locked = Path(self.temp.name) / 'locked.pdf'
        with fitz.open(self.source) as doc:
            doc.save(locked, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw='test', owner_pw='owner')
        with self.assertRaisesRegex(ValueError, 'password protected'):
            compressor.compress(locked, Path(self.temp.name) / 'locked-out.pdf', Path(self.temp.name) / 'locked', {})

    def test_content_stream_parsing_keeps_operands_exactly(self):
        data = (b'q 1 0 0 1 72 720 cm % a comment\n/GS1 gs BT /F1 9 Tf [(Nested (paren) \\) text) -250 <48656C6C6F>] TJ ET Q\n'
                b'/P <</MCID 3>> BDC 0 0 m 10 10 l S EMC true false null 5 d0')
        operations = compressor.parse(data)
        self.assertEqual([op for _, op in operations], [b'q', b'cm', b'gs', b'BT', b'Tf', b'TJ', b'ET', b'Q', b'BDC', b'm', b'l', b'S', b'EMC', b'd0'])
        self.assertEqual(operations[5][0], b'[(Nested (paren) \\) text) -250 <48656C6C6F>]')
        self.assertEqual(operations[8][0], b'/P <</MCID 3>>')
        self.assertEqual(compressor.parse(compressor.serialize(operations)), operations)
        trailing = compressor.parse(b'1 0 0 1 0 0 % note\ncm 0 g')
        self.assertEqual(compressor.parse(compressor.serialize(trailing)), trailing)
        # Inline image data is binary; those streams go through pypdf.
        inline = compressor.parse(b'q BI /W 2 /H 1 /CS /G /BPC 8 ID \x00\xff EI Q (x) Tj')
        self.assertEqual([op for _, op in inline], [b'q', b'INLINE IMAGE', b'Q', b'Tj'])
        self.assertTrue(inline[1][0].startswith(b'BI') and inline[1][0].endswith(b'EI'))

    def test_text_layer_drops_artwork_but_keeps_text_state(self):
        operations = compressor.parse(
            b'q 1 0 0 RG 0 0 m 10 10 l S Q q 0 0 100 100 re W n BT /F1 9 Tf (A) Tj ET Q '
            b'q /Im1 Do Q 5 5 m 6 6 l S BT /F1 9 Tf 0 3 Td (B) Tj ET')
        kept = compressor.retain_text(operations)
        ops = [op for _, op in kept]
        self.assertEqual(ops.count(b'Tj'), 2)
        self.assertNotIn(b'Do', ops)
        self.assertNotIn(b'S', ops)
        self.assertIn(b'W', ops)


if __name__ == '__main__':
    unittest.main()
