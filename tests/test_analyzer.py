"""Focused checks for shared accounting, rendering and source preservation."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from paperweight.analyzer import fitz, inspect_pdf, object_info, preview, read_object, references


class ExplorerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'shared.pdf'
        pix = fitz.Pixmap(fitz.csRGB, (0, 0, 64, 64), False)
        pix.clear_with(160)
        with fitz.open() as doc:
            first = doc.new_page()
            first.insert_text((50, 50), 'One shared image, two pages')
            self.image = first.insert_image(fitz.Rect(50, 80, 250, 280), pixmap=pix)
            second = doc.new_page()
            second.insert_text((50, 50), 'Second page')
            second.insert_image(fitz.Rect(50, 80, 150, 180), xref=self.image)
            second.add_text_annot((300, 300), 'An annotation appearance')
            doc.save(self.path, deflate=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_shared_accounting_and_preservation(self):
        before = hashlib.sha256(self.path.read_bytes()).digest()
        report = inspect_pdf(self.path)
        self.assertEqual(report['bytes'], self.path.stat().st_size)
        self.assertEqual(sum(c['bytes'] for c in report['categories']), report['bytes'])
        self.assertEqual(sum(o['bytes'] for o in report['objects']), report['bytes'])
        images = [o for o in report['objects'] if o['id'] == self.image]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]['pages'], [1, 2])
        for page in report['pages']:
            self.assertIn(self.image, page['objects'])
            self.assertGreaterEqual(page['sharedBytes'], images[0]['bytes'])
            self.assertEqual(page['exclusiveBytes'] + page['sharedBytes'], page['referencedBytes'])
        self.assertIn('/Image', object_info(self.path, self.image)['dictionary'])
        for kind, number in [('image', self.image), ('page', 2)]:
            data = preview(self.path, kind, number)
            self.assertTrue(data.startswith(b'\x89PNG\r\n\x1a\n'))
            self.assertGreater(fitz.Pixmap(data).width, 0)
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).digest())

    def test_compressed_object_containers(self):
        packed = Path(self.temp.name) / 'packed.pdf'
        with fitz.open(self.path) as doc:
            doc.save(packed, use_objstms=1, deflate=True)
        report = inspect_pdf(packed)
        self.assertEqual(sum(c['bytes'] for c in report['categories']), packed.stat().st_size)
        self.assertTrue(any(c['id'] == 'containers' for c in report['categories']))
        self.assertEqual(next(o for o in report['objects'] if o['id'] == self.image)['pages'], [1, 2])

    def test_parallel_reading_matches_sequential(self):
        from paperweight import analyzer
        sequential = inspect_pdf(self.path)
        limit, analyzer.PARALLEL_OBJECTS = analyzer.PARALLEL_OBJECTS, 0
        try:
            self.assertEqual(inspect_pdf(self.path), sequential)
        finally:
            analyzer.PARALLEL_OBJECTS = limit

    def test_missing_objects_read_as_null(self):
        class Broken:
            def xref_object(self, x):
                raise RuntimeError('code=7: cannot find object in xref (110 0 R)')
        self.assertEqual(read_object(Broken(), 110), ('null', set(), 'null', set(), None, None, None))

    def test_ignores_reference_like_strings(self):
        self.assertEqual(references('<< /Actual 7 0 R /Text (12 0 R (33 0 R) \\(99)) /Hex <313220302052> % 66 0 R\n /Other 8 0 R >>'), {7, 8})
        self.assertEqual(references('[ (a\\) 3 0 R) 5 0 R <0102> 6 0 R << /K 9 0 R >> ]'), {5, 6, 9})

    def test_image_mask_preview(self):
        mask_path = Path(self.temp.name) / 'mask.pdf'
        with fitz.open(self.path) as doc:
            mask = doc.get_new_xref()
            doc.update_object(mask, '<< /Type /XObject /Subtype /Image /Width 8 /Height 8 /BitsPerComponent 1 /ImageMask true >>')
            doc.update_stream(mask, b'\x55\xaa' * 4)
            doc.save(mask_path)
        self.assertTrue(preview(mask_path, 'image', mask).startswith(b'\x89PNG'))

    def test_encrypted_and_invalid_documents(self):
        locked = Path(self.temp.name) / 'locked.pdf'
        with fitz.open(self.path) as doc:
            doc.save(locked, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw='test', owner_pw='owner')
        with self.assertRaisesRegex(ValueError, 'password protected'):
            inspect_pdf(locked)
        invalid = Path(self.temp.name) / 'invalid.pdf'
        invalid.write_bytes(b'not a PDF')
        with self.assertRaises(Exception):
            inspect_pdf(invalid)


if __name__ == '__main__':
    unittest.main()
