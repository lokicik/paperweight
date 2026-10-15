"""Reordering, deleting, rotating and inserting pages, combining PDFs and splitting one."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from paperweight.analyzer import fitz
from paperweight.organizer import check_pages, merge, organize, split, split_by_size, split_plan


class OrganizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        folder = Path(self.temp.name)
        self.book, self.extra = folder / 'book.pdf', folder / 'extra.pdf'
        with fitz.open() as doc:
            for n in range(5):
                doc.new_page().insert_text((72, 72), f'Book page {n + 1}')
            doc[0].insert_link({'kind': fitz.LINK_GOTO, 'from': fitz.Rect(72, 60, 200, 85), 'page': 4})
            doc.set_toc([[1, 'Start', 1], [1, 'Middle', 3], [2, 'Detail', 3], [1, 'End', 5]])
            doc.save(self.book)
        with fitz.open() as doc:
            for n in range(2):
                doc.new_page(width=300, height=400).insert_text((40, 40), f'Extra page {n + 1}')
            doc.save(self.extra)
        self.paths = {'book': str(self.book), 'extra': str(self.extra)}
        self.digests = {k: hashlib.sha256(Path(v).read_bytes()).digest() for k, v in self.paths.items()}

    def tearDown(self):
        self.temp.cleanup()

    def test_reorder_delete_rotate_and_insert(self):
        pages = [{'doc': 'book', 'page': 5}, {'doc': 'extra', 'page': 2}, {'doc': 'book', 'page': 1},
                 {'doc': 'book', 'page': 3, 'rotate': 90}, {'blank': True, 'width': 595, 'height': 842}]
        output = Path(self.temp.name) / 'out.pdf'
        result = organize('book', self.paths, pages, str(output))
        self.assertEqual(result['pages'], 5)
        with fitz.open(output) as doc:
            self.assertEqual([p.get_text().strip() for p in doc], ['Book page 5', 'Extra page 2', 'Book page 1', 'Book page 3', ''])
            self.assertEqual(doc[3].rotation, 90)
            self.assertEqual(doc[1].rect, fitz.Rect(0, 0, 300, 400))
            # The link on page 1 still points at the old page 5, now first.
            self.assertEqual(doc[2].get_links()[0]['page'], 0)
            self.assertEqual({title: page for _, title, page in doc.get_toc()}, {'Start': 3, 'Middle': 4, 'Detail': 4, 'End': 1})
        for key, path in self.paths.items():
            self.assertEqual(self.digests[key], hashlib.sha256(Path(path).read_bytes()).digest())

    def test_pages_from_other_documents_only(self):
        output = Path(self.temp.name) / 'only-extra.pdf'
        organize('book', self.paths, [{'doc': 'extra', 'page': 1}, {'doc': 'extra', 'page': 1}], str(output))
        with fitz.open(output) as doc:
            self.assertEqual([p.get_text().strip() for p in doc], ['Extra page 1', 'Extra page 1'])

    def test_page_lists_are_validated(self):
        counts = {'book': 5}
        for pages in ([], [{'doc': 'book', 'page': 6}], [{'doc': 'gone', 'page': 1}],
                      [{'doc': 'book', 'page': 1, 'rotate': 45}], [{'blank': True, 'width': 1, 'height': 1}], 'all'):
            with self.assertRaises(ValueError):
                check_pages(pages, counts)
        check_pages([{'doc': 'book', 'page': 2, 'rotate': 270}, {'blank': True, 'width': 612, 'height': 792}], counts)


    def test_merge_keeps_every_page_link_and_bookmark(self):
        output = Path(self.temp.name) / 'combined.pdf'
        result = merge([str(self.book), str(self.extra)], ['book.pdf', 'extra.pdf'], str(output))
        self.assertEqual((result['pages'], result['files']), (7, 2))
        with fitz.open(output) as doc:
            self.assertEqual([p.get_text().strip() for p in doc][4:6], ['Book page 5', 'Extra page 1'])
            self.assertEqual(doc[0].get_links()[0]['page'], 4)
            self.assertEqual(doc.get_toc(), [[1, 'book', 1], [2, 'Start', 1], [2, 'Middle', 3], [3, 'Detail', 3], [2, 'End', 5], [1, 'extra', 6]])
        for key, path in self.paths.items():
            self.assertEqual(self.digests[key], hashlib.sha256(Path(path).read_bytes()).digest())

    def test_split_plans(self):
        outline = [[1, 'Start', 1], [1, 'Middle', 3], [2, 'Detail', 4], [1, 'End', 5]]
        self.assertEqual(split_plan(5, 'every', 2), [(1, 2, None), (3, 4, None), (5, 5, None)])
        self.assertEqual(split_plan(5, 'ranges', [[4, 5], [1, 1]]), [(4, 5, None), (1, 1, None)])
        self.assertEqual(split_plan(5, 'bookmarks', 1, outline), [(1, 2, 'Start'), (3, 4, 'Middle'), (5, 5, 'End')])
        self.assertEqual(split_plan(5, 'bookmarks', 2, outline), [(1, 2, 'Start'), (3, 3, 'Middle'), (4, 4, 'Detail'), (5, 5, 'End')])
        self.assertEqual(split_plan(5, 'bookmarks', 1, [[1, 'Late', 3]]), [(1, 2, None), (3, 5, 'Late')])
        for mode, value in (('every', 0), ('every', 6), ('ranges', [[2, 1]]), ('ranges', [[1, 6]]), ('ranges', []), ('bookmarks', 1), ('pages', 1)):
            with self.assertRaises(ValueError):
                split_plan(5, mode, value, [])

    def test_split_writes_checked_parts_with_their_bookmarks(self):
        folder = Path(self.temp.name) / 'parts'
        parts = split(str(self.book), [(1, 2, 'Start'), (3, 5, 'Middle: and/the rest')], folder, 'book')
        self.assertEqual([p['name'] for p in parts], ['book - 1 Start.pdf', 'book - 2 Middle and the rest.pdf'])
        self.assertEqual([(p['firstPage'], p['lastPage']) for p in parts], [(1, 2), (3, 5)])
        with fitz.open(folder / parts[1]['name']) as doc:
            self.assertEqual([p.get_text().strip() for p in doc], ['Book page 3', 'Book page 4', 'Book page 5'])
            self.assertEqual(doc.get_toc(), [[1, 'Middle', 1], [2, 'Detail', 1], [1, 'End', 3]])
        self.assertEqual(self.digests['book'], hashlib.sha256(self.book.read_bytes()).digest())

    def test_split_by_size(self):
        parts = split_by_size(str(self.book), self.book.stat().st_size // 2, Path(self.temp.name) / 'sized', 'book')
        self.assertGreater(len(parts), 1)
        self.assertEqual(sum(p['lastPage'] - p['firstPage'] + 1 for p in parts), 5)


if __name__ == '__main__':
    unittest.main()
