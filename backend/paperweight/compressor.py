"""Measured, text-preserving PDF compression. The source file is never modified.

Heavy pages keep their text as PDF text; their remaining artwork is drawn once
as an image underneath. Every converted page is checked before it is used, and
the finished copy is compared with the original page by page.
"""
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import ExitStack
import io
import math
import os
from pathlib import Path
import re
import shutil
import struct
import time
import zipfile
import zlib

from PIL import Image, ImageChops, ImageStat
import pymupdf as fitz
from pypdf.generic import ContentStream, DecodedStreamObject

PRESETS = {
    'high': {'label': 'High', 'dpi': 200, 'quality': 85},
    'balanced': {'label': 'Balanced', 'dpi': 150, 'quality': 80},
    'compact': {'label': 'Compact', 'dpi': 110, 'quality': 75},
    'lossless': {'label': 'Lossless', 'dpi': None, 'quality': None},
}
ORDER = ['high', 'balanced', 'compact']
# Data that never changes how pages look. Object-level XMP metadata is always removed.
REMOVALS = {'editing', 'thumbnails', 'attachments', 'docmeta'}
MIN_PAGE_BYTES = 4_000
MAX_PIXELS = 60_000_000

SHOW = {b'Tj', b'TJ', b"'", b'"'}
MARK = {b'BMC', b'BDC', b'EMC', b'MP', b'DP'}
PATH = {b'm', b'l', b'c', b'v', b'y', b'h', b're'}
PAINT = {b'S', b's', b'f', b'F', b'f*', b'B', b'B*', b'b', b'b*', b'n'}
REASONS = {
    'small': 'Too small to benefit',
    'text_cost': 'Text alone costs about as much',
    'small_gain': 'Image would not be smaller',
    'form_text': 'Text inside reusable artwork',
    'text_clip': 'Text used as a clipping shape',
    'unsupported': 'Drawing structure not supported',
    'text_difference': 'Text would change',
    'too_large': 'Page too large to render',
    'blank': 'Nothing to redraw',
    'bitonal': 'Already compact black-and-white scan',
    'excluded': 'You chose to keep',
}


class Cancelled(Exception):
    pass


# One content-stream operation: any operands, then an operator. Literal
# strings may nest two levels; anything unusual falls back to pypdf.
_OPERAND = (rb'(?:/[^\s/\[\]()<>{}%]*|<<|>>|<[^<>]*>|\[|\]|'
            rb'\((?:[^()\\]|\\.|\((?:[^()\\]|\\.|\((?:[^()\\]|\\.)*\))*\))*\)|'
            rb'[+\-.0-9][^\s/\[\]()<>{}%]*|true(?![^\s/\[\]()<>{}%])|false(?![^\s/\[\]()<>{}%])|'
            rb'null(?![^\s/\[\]()<>{}%])|%[^\r\n]*)')
OPERATION = re.compile(rb'\s*((?:' + _OPERAND + rb'\s*)*)([A-Za-z\'"][^\s/\[\]()<>{}%]*)', re.S)
TRAILING_NAME = re.compile(rb'/[^\s/\[\]()<>{}%]*\s*$')


class Unparsed(Exception):
    pass


def tokenize(data):
    """Content stream operations as (raw operand bytes, operator).

    Operands keep their exact bytes, so writing operations back reproduces
    the drawing exactly. One regular-expression step per operation; much
    faster than building PDF objects for every operand.
    """
    operations, pos = [], 0
    for m in OPERATION.finditer(data):
        if m.start() != pos:
            raise Unparsed()
        op = m.group(2)
        if op == b'BI':
            raise Unparsed()  # inline image data needs pypdf's heuristics
        operations.append((m.group(1).strip(), op))
        pos = m.end()
    if data[pos:].strip():
        raise Unparsed()
    return operations


def parse(data):
    """Operations of a content stream as (raw operand bytes, operator)."""
    try:
        return tokenize(data)
    except Unparsed:
        stream = DecodedStreamObject()
        stream.set_data(data)
        operations = []
        for args, op in ContentStream(stream, None).operations:
            if op == b'INLINE IMAGE':
                single = ContentStream(DecodedStreamObject(), None)
                single.operations = [(args, op)]
                operations.append((single.get_data().strip(), op))
                continue
            raw = []
            for arg in args:
                out = io.BytesIO()
                arg.write_to_stream(out)
                raw.append(out.getvalue())
            operations.append((b' '.join(raw), op))
        return operations


def serialize(operations):
    # A comment among the operands would swallow an operator on the same line.
    return b'\n'.join(args if op == b'INLINE IMAGE' else (args + (b'\n' if b'%' in args else b' ') + op if args else op)
                      for args, op in operations) + b'\n'


def without_text(operations):
    """Drawing operations with text painting removed; used to render the artwork."""
    return [(args, op) for args, op in operations if op not in SHOW]


def retain_text(operations):
    """Keep text and the state it depends on; drop paths, shadings and XObjects.

    Graphics scopes without text are discarded entirely. Clipping paths that
    may contain text are kept, painted paths are not.
    """
    frames = [([], False)]
    for args, op in operations:
        if op == b'q':
            frames.append(([], False))
            continue
        if op == b'Q':
            if len(frames) == 1:
                raise ValueError('unbalanced graphics state')
            ops, has_text = frames.pop()
            outer, outer_has = frames[-1]
            if has_text:
                outer.extend([(b'', b'q'), *ops, (b'', b'Q')])
            else:
                outer.extend(x for x in ops if x[1] in MARK)
            frames[-1] = (outer, outer_has or has_text)
            continue
        if op in {b'Do', b'INLINE IMAGE', b'sh'}:
            continue
        ops, has_text = frames[-1]
        ops.append((args, op))
        frames[-1] = (ops, has_text or op in SHOW)
    if len(frames) != 1:
        raise ValueError('unbalanced graphics state')
    kept, path, clip = [], [], False
    for args, op in frames[0][0]:
        if op in PATH:
            path.append((args, op))
        elif op in {b'W', b'W*'}:
            path.append((args, op))
            clip = True
        elif op in PAINT:
            if clip:
                kept.extend(path)
                kept.append((b'', b'n'))
            path, clip = [], False
        else:
            kept.append((args, op))
    return kept


def uses_pattern(operations):
    return any(op in {b'scn', b'SCN'} and TRAILING_NAME.search(args) for args, op in operations)


def has_text(doc, xref):
    try:
        return any(op in SHOW for _, op in parse(doc.xref_stream(xref)))
    except Exception:
        return True


def encode(image, fmt, **options):
    out = io.BytesIO()
    image.save(out, format=fmt, **options)
    return out.getvalue()


def mse(a, b):
    stat = ImageStat.Stat(ImageChops.difference(a, b))
    return sum(v * v for v in stat.rms) / len(stat.rms)


def is_gray(image):
    r, g, b = image.split()
    return ImageChops.difference(r, g).getbbox() is None and ImageChops.difference(g, b).getbbox() is None


def fewest_colors(image, budget, limit=None):
    """(indexed image, error) for a small palette whose error stays within `budget`, or None.

    Octree is fast and often fits with fewer colors than median cut; median cut
    catches the images octree handles poorly. Most rendered pages fit at 64
    colors, so the search starts there. Median cut is ten times slower, and on
    sampled pages its 256-color PNG was always larger than octree's: it is
    skipped when octree's is already `limit` bytes or more."""
    octree = {}

    def fit(count, method=Image.Quantize.FASTOCTREE):
        indexed = image.quantize(colors=count, method=method, dither=Image.Dither.NONE)
        octree[count] = indexed
        error = mse(image, indexed.convert('RGB'))
        return (indexed, error) if error <= budget + 1e-8 else None

    found = fit(64)
    if found:
        return fit(16) or fit(32) or found
    found = fit(128) or fit(256)
    if found:
        return found
    if limit is not None and len(encode(octree[256], 'PNG', compress_level=6)) >= limit:
        return None
    return fit(256, Image.Quantize.MEDIANCUT)


def palette_png(image, budget=None, limit=None):
    """Indexed PNG of an RGB image with the fewest bits per pixel, or None.

    Exact when the image has at most 256 colors. Otherwise, with an error
    `budget`, a small palette whose error stays within it; `limit` is the size
    of the best other encoding (see fewest_colors).
    """
    colors = image.getcolors(256)
    if colors:
        indexed = image.convert('P', palette=Image.Palette.ADAPTIVE, colors=len(colors))
        if indexed.convert('RGB').tobytes() != image.tobytes():
            return None
        error = 0.0
    elif budget:
        found = fewest_colors(image, budget, limit)
        if found is None:
            return None
        indexed, error = found
    else:
        return None
    count = indexed.getextrema()[1] + 1
    bits = 1 if count <= 2 else 2 if count <= 4 else 4 if count <= 16 else 8
    return encode(indexed, 'PNG', optimize=True, bits=bits), error


def best_encoding(image, quality):
    """Smallest of palette PNG, JPEG and JPEG 2000 whose error stays within JPEG `quality`.

    Full-color PNG and lossless JPEG 2000 are slow and, on rendered pages, lose to
    palette PNG or lossy JPEG 2000; they are tried only when those do not fit.
    Gray images always try PNG, which suits scanned line art."""
    color = image.mode == 'RGB'
    jpeg = encode(image, 'JPEG', quality=quality, optimize=True, subsampling=0)
    reference = mse(image, Image.open(io.BytesIO(jpeg)).convert(image.mode))
    options = {'no_jp2': True, 'mct': 1 if color else 0}
    choices = [('jpeg', jpeg, reference)]
    if reference > 0:
        # OpenJPEG usually lands above the PSNR it is asked for, so start at the
        # reference's own PSNR and raise the target only when the error is too high.
        target = 10 * math.log10(255 * 255 / reference)
        for delta in (0, 1, 3):
            data = encode(image, 'JPEG2000', irreversible=True, quality_mode='dB', quality_layers=[target + delta], **options)
            error = mse(image, Image.open(io.BytesIO(data)).convert(image.mode))
            if error <= reference + 1e-8:
                choices.append(('jpx', data, error))
                break
    indexed = palette_png(image, reference, min(len(c[1]) for c in choices)) if color else None
    if indexed:
        choices.append(('png', *indexed))
    if len(choices) == 1:
        choices += [('png', encode(image, 'PNG', optimize=True), 0.0),
                    ('jpx', encode(image, 'JPEG2000', irreversible=False, **options), 0.0)]
    elif not color:
        choices.append(('png', encode(image, 'PNG', optimize=True), 0.0))
    return min(choices, key=lambda c: len(c[1]))


def png_stream(data, base='/DeviceRGB', gray_base=False):
    """PNG image data as a PDF Flate stream with PNG predictors, and the
    image dictionary keys that describe it. Palette PNGs become /Indexed over
    `base`; `gray_base` means the base has one component per entry."""
    pos, idat, header, palette = 8, [], None, None
    while pos < len(data):
        length, kind = struct.unpack('>I4s', data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        if kind == b'IHDR':
            header = struct.unpack('>IIBBBBB', chunk)
        elif kind == b'IDAT':
            idat.append(chunk)
        elif kind == b'PLTE':
            palette = chunk
        pos += 12 + length
    width, _, depth, color_type, _, _, interlace = header
    if interlace or color_type not in (0, 2, 3) or (color_type != 3 and depth != 8):
        raise ValueError('unexpected PNG layout')
    colors = 3 if color_type == 2 else 1
    keys = {'Filter': '/FlateDecode', 'BitsPerComponent': str(depth),
            'DecodeParms': f'<</Predictor 15/Colors {colors}/BitsPerComponent {depth}/Columns {width}>>'}
    if color_type == 3:
        entries = palette[0::3] if gray_base else palette
        keys['ColorSpace'] = f'[/Indexed {base} {len(palette) // 3 - 1} <{entries.hex()}>]'
    return b''.join(idat), keys


def replace_image(doc, xref, data, keys):
    doc.update_stream(xref, data, compress=False)
    for key, value in keys.items():
        doc.xref_set_key(xref, key, value)


def source_text(page):
    return page.get_text()


def effective_resources(doc, xref):
    """The resource dictionary of page object `xref` as PDF source, following inheritance."""
    node, seen = xref, set()
    while node and node not in seen:
        seen.add(node)
        kind, value = doc.xref_get_key(node, 'Resources')
        if kind == 'xref':
            return doc.xref_object(int(value.split()[0]))
        if kind == 'dict':
            return value
        kind, value = doc.xref_get_key(node, 'Parent')
        node = int(value.split()[0]) if kind == 'xref' else None
    return '<<>>'


NAMED_RESOURCES = ('ColorSpace', 'ExtGState', 'Font', 'Pattern', 'Properties')
RESOURCE_NAME = re.compile(rb'/([^\s/\[\]()<>{}%]*)')


def prune_resources(doc, resources, text):
    """Drop resource entries the new page content never names, so data only the
    old drawing used (color spaces, fonts, graphics states) can be discarded."""
    names = set(RESOURCE_NAME.findall(text))
    if any(b'#' in name for name in names):
        return  # Escaped names: matching them is not worth the risk.
    names = {name.decode('latin-1') for name in names}
    for kind in NAMED_RESOURCES:
        try:
            k, value = doc.xref_get_key(resources, kind)
            if k == 'xref':
                value = doc.xref_object(int(value.split()[0]), compressed=True)
            elif k != 'dict':
                continue
            # A scratch object to edit the entries; unreferenced, it is dropped on save.
            holder = doc.get_new_xref()
            doc.update_object(holder, value)
            keys = doc.xref_get_keys(holder)
            if any('#' in key for key in keys):
                continue
            kept = [(key, doc.xref_get_key(holder, key)) for key in keys if key in names]
            # Values are rewritten as PDF source; strings would come back decoded.
            if len(kept) < len(keys) and all(v[0] in ('xref', 'dict', 'array', 'name', 'int', 'float', 'bool') for _, v in kept):
                entries = ''.join(f'/{key} {value}' for key, (_, value) in kept)
                doc.xref_set_key(resources, kind, f'<<{entries}>>' if kept else 'null')
        except Exception:
            continue


def jpeg_header(data):
    """(width, height, components) from a JPEG's frame header."""
    pos = 2
    while pos + 9 < len(data):
        marker, length = data[pos + 1], struct.unpack('>H', data[pos + 2:pos + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack('>HH', data[pos + 5:pos + 9])
            return width, height, data[pos + 9]
        pos += 2 + length
    raise ValueError('no JPEG frame header')


def image_object(doc, fmt, data):
    """A new image XObject for a JPEG, a JPEG 2000 codestream or a PNG, built
    from the header alone: inserting it through MuPDF would decode the whole image."""
    if fmt == 'jpeg':
        width, height, components = jpeg_header(data)
        stream = data
        keys = {'Filter': '/DCTDecode', 'BitsPerComponent': '8', 'ColorSpace': '/DeviceRGB' if components == 3 else '/DeviceGray'}
    elif fmt == 'jpx':
        # Codestream: SOC, then SIZ with the image and offset sizes and the component count.
        width, height, left, top = struct.unpack('>4I', data[8:24])
        components = struct.unpack('>H', data[40:42])[0]
        width, height, stream = width - left, height - top, data
        keys = {'Filter': '/JPXDecode', 'BitsPerComponent': '8', 'ColorSpace': '/DeviceRGB' if components == 3 else '/DeviceGray'}
    else:
        width, height = struct.unpack('>II', data[16:24])
        stream, keys = png_stream(data)
        keys.setdefault('ColorSpace', '/DeviceGray' if data[25] == 0 else '/DeviceRGB')
    xref = doc.get_new_xref()
    entries = ''.join(f'/{key} {value}' for key, value in keys.items())
    doc.update_object(xref, f'<</Type/XObject/Subtype/Image/Width {width}/Height {height}{entries}>>')
    put_stream(doc, xref, stream)
    return xref


def image_matrix(page, box):
    """The placement of artwork cropped to `box` (in page coordinates) as a cm
    operand, for a page with rotation 0. The same matrix and number format
    insert_image writes, so images land on exactly the same device pixels."""
    x0, y0, x1, y1 = box
    rect = fitz.Rect(page.rect.x0 + x0, page.rect.y0 + y0, page.rect.x0 + x1, page.rect.y0 + y1)
    m = fitz.calc_image_matrix(0, 0, rect * ~page.transformation_matrix, 0, False)
    return fitz._format_g((m.a, m.b, m.c, m.d, m.e, m.f))


def apply_conversion(doc, xref, text, artwork):
    """Replace the drawing of the page object `xref` with `text` over one image
    placed by artwork['matrix']. Keeps page identity, annotations and links.
    Resources become page-owned so other pages are untouched."""
    resources = doc.get_new_xref()
    doc.update_object(resources, effective_resources(doc, xref))
    doc.xref_set_key(xref, 'Resources', f'{resources} 0 R')
    doc.xref_set_key(resources, 'XObject', '<<>>')
    doc.xref_set_key(resources, 'Shading', 'null')
    if not artwork.get('pattern'):
        doc.xref_set_key(resources, 'Pattern', 'null')
    prune_resources(doc, resources, text)
    contents = doc.get_new_xref()
    doc.update_object(contents, '<</Filter/FlateDecode>>')
    put_stream(doc, contents, zlib.compress(text, 9))
    doc.xref_set_key(xref, 'Contents', f'{contents} 0 R')
    if artwork.get('format'):
        image = image_object(doc, artwork['format'], artwork['data'])
        doc.xref_set_key(resources, 'XObject', f'<</pwImage {image} 0 R>>')
        drawing = doc.get_new_xref()
        doc.update_object(drawing, '<<>>')
        doc.update_stream(drawing, f'q {artwork["matrix"]} cm /pwImage Do Q'.encode(), compress=False)
        doc.xref_set_key(xref, 'Contents', f'[{drawing} 0 R {contents} 0 R]')


_open = {}


def opened(path):
    """One read-only document per worker process."""
    if path not in _open:
        _open.clear()
        _open[path] = fitz.open(path)
    return _open[path]


def only_scaled_up(doc, number, eligible, images, dpi):
    """True when the page's own images that can be re-encoded hold nearly all its
    bytes and every image on it is shown at or below `dpi`, so a redraw at `dpi`
    would scale them up."""
    if sum(len(doc.xref_stream_raw(x)) for x in images if native_kind(doc, x)) < eligible * 0.9:
        return False
    scale = (dpi / 72) ** 2
    shown = doc[number - 1].get_image_info()
    return bool(shown) and all(i['width'] * i['height'] <= scale * fitz.Rect(i['bbox']).get_area() for i in shown)


def prepare_page(path, number, eligible, preset, workdir, keep_page=False, images=()):
    """Plan one page in a worker and return a record.

    The page is redrawn when that is clearly smaller. Its own large `images`
    (xrefs no other page uses) are re-encoded at their own resolution when the
    page is not redrawn, or when redrawing would scale an image up; whichever
    saves more is kept. The new data travels back in `blobs`; with `keep_page`,
    a changed page is saved as a one-page PDF for previews instead."""
    if images and only_scaled_up(opened(path), number, eligible, images, PRESETS[preset]['dpi']):
        # Redrawing would only scale its pictures up; re-encoding them is what can help.
        record = {'page': number, 'eligibleBytes': eligible, 'preset': preset, 'status': 'small_gain', 'seconds': 0.0}
    else:
        record = redraw_page(path, number, eligible, preset, workdir, keep_page)
    if not images:
        return record
    started = time.monotonic()
    source = opened(path)
    page = source[number - 1]
    pixels = {i[0]: i[2] * i[3] for i in page.get_images(full=True)}
    own = {x for x in set(images) & set(pixels) if len(source.xref_stream_raw(x)) >= NATIVE_MIN_BYTES}
    if own and record['status'] == 'converted':
        # An image with more pixels than the whole page has at the preset
        # resolution is never scaled up; this spares hashing every image below.
        whole = (PRESETS[preset]['dpi'] / 72) ** 2 * page.rect.width * page.rect.height
        own = {x for x in own if pixels[x] < whole}
    if own and record['status'] == 'converted':
        shown = Counter()
        for info in page.get_image_info(xrefs=True):
            box = fitz.Rect(info['bbox'])
            shown[info['xref']] = max(shown[info['xref']], box.width * box.height)
        scale = (PRESETS[preset]['dpi'] / 72) ** 2
        own = {x for x in own if pixels[x] < scale * shown[x]}
    found = reencode_images(source, sorted(own), PRESETS[preset]['quality']) if own else []
    saving = sum(old - len(data) for _, old, _, data, _ in found)
    if not found or (record['status'] == 'converted' and saving <= record['estimatedSaving']):
        return record
    record = {'page': number, 'eligibleBytes': eligible, 'preset': preset, 'status': 'reencoded', 'redraw': record['status'],
              'format': found[0][2], 'estimatedSaving': saving, 'imageBytes': sum(len(data) for _, _, _, data, _ in found),
              'oldImageBytes': sum(old for _, old, _, _, _ in found), 'replace': [[x, keys] for x, _, _, _, keys in found],
              'seconds': round(record['seconds'] + time.monotonic() - started, 2)}
    if keep_page:
        stem = Path(workdir) / f'{preset}-{number}'
        with fitz.open(path) as copy, fitz.open() as one:
            for x, _, _, data, keys in found:
                replace_image(copy, x, data, keys)
            one.insert_pdf(copy, from_page=number - 1, to_page=number - 1)
            one.save(stem.with_suffix('.pdf'), garbage=2, deflate=True)
        record['artifact'] = str(stem)
    else:
        record['blobs'] = [data for _, _, _, data, _ in found]
    return record


def redraw_page(path, number, eligible, preset, workdir, keep_page=False):
    """Keep the page's text and draw the rest as one image; see prepare_page."""
    started = time.monotonic()
    record = {'page': number, 'eligibleBytes': eligible, 'preset': preset}

    def done(status, **extra):
        record.update(status=status, seconds=round(time.monotonic() - started, 2), **extra)
        return record

    settings = PRESETS[preset]
    source = opened(path)
    one = fitz.open()
    try:
        one.insert_pdf(source, from_page=number - 1, to_page=number - 1)
        page = one[0]
        rotation = page.rotation
        if rotation:
            page.set_rotation(0)
        if eligible < MIN_PAGE_BYTES:
            return done('small')
        # Black-and-white scans stored as JBIG2 or CCITT are smaller than any
        # redraw could be; rendering them would only cost time.
        bitonal = sum(len(one.xref_stream_raw(i[0])) for i in page.get_images(full=True) if i[8] in ('JBIG2Decode', 'CCITTFaxDecode'))
        if bitonal >= eligible * 0.8:
            return done('bitonal')
        before = source_text(page)
        if any(has_text(one, x[0]) for x in page.get_xobjects()):
            return done('form_text')
        try:
            operations = parse(page.read_contents())
            if any(op == b'Tr' and args and float(args.split()[0]) >= 4 for args, op in operations):
                return done('text_clip')
            kept = retain_text(operations)
        except Exception:
            return done('unsupported')
        text = serialize(kept)
        text_bytes = len(zlib.compress(text, 9))
        if text_bytes >= eligible * 0.9:
            return done('text_cost')
        zoom = settings['dpi'] / 72
        if page.rect.width * page.rect.height * zoom * zoom > MAX_PIXELS:
            return done('too_large')
        drawing = one.get_new_xref()
        one.update_object(drawing, '<<>>')
        # Only rendered, never saved: no need to compress it.
        one.update_stream(drawing, serialize(without_text(operations)), compress=False)
        page.set_contents(drawing)
        pix = page.get_pixmap(dpi=settings['dpi'], alpha=False, annots=False)
        image = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
        sx, sy = page.rect.width / pix.width, page.rect.height / pix.height
        del pix
        artwork = {'pattern': uses_pattern(kept)}
        box = ImageChops.difference(image, Image.new('RGB', image.size, 'white')).getbbox()
        image_bytes = 0
        if box:
            # Keep JPEG 8x8 block alignment and one block of white margin.
            box = (max(0, (box[0] // 8 - 1) * 8), max(0, (box[1] // 8 - 1) * 8),
                   min(image.width, ((box[2] + 7) // 8 + 1) * 8), min(image.height, ((box[3] + 7) // 8 + 1) * 8))
            image = image.crop(box)
            gray = is_gray(image)
            if gray:
                image = image.convert('L')
            fmt, data, error = best_encoding(image, settings['quality'])
            image_bytes = len(data)
            if image_bytes > eligible * 0.85 or image_bytes + text_bytes > eligible * 0.9:
                return done('small_gain', imageBytes=image_bytes, textBytes=text_bytes)
            artwork.update(format=fmt, data=data, matrix=image_matrix(page, (box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy)))
            record.update(format=fmt, gray=gray, pixels=[image.width, image.height], error=round(error, 3))
        # The image does not change the extracted text; inserting it costs a
        # full decode, so it is left out unless the page is kept for a preview.
        apply_conversion(one, page.xref, text, artwork if keep_page else {'pattern': artwork['pattern']})
        if source_text(page) != before:
            return done('text_difference')
        if keep_page:
            stem = Path(workdir) / f'{preset}-{number}'
            page.set_rotation(rotation)
            one.save(stem.with_suffix('.pdf'), garbage=2, deflate=True)
            record['artifact'] = str(stem)
        else:
            # Returned rather than written: thousands of fresh small files are
            # slow to reopen where antivirus scans each one.
            record['blobs'] = [text, artwork.get('data', b'')]
        return done('converted', imageBytes=image_bytes, textBytes=text_bytes,
                    estimatedSaving=eligible - image_bytes - text_bytes,
                    matrix=artwork.get('matrix'), pattern=artwork['pattern'])
    finally:
        one.close()


def load_blobs(record, spool):
    """A prepared page's data, read from the open spool file."""
    blobs = []
    for offset, size in record['spooled'][1]:
        spool.seek(offset)
        blobs.append(spool.read(size))
    return blobs


def apply_record(doc, record, blobs):
    """Make the change a prepared page record describes."""
    if record['status'] == 'reencoded':
        for (x, keys), data in zip(record['replace'], blobs):
            replace_image(doc, x, data, keys)
        return
    text, data = blobs
    artwork = {'pattern': record['pattern']}
    if record.get('format'):
        artwork.update(format=record['format'], matrix=record['matrix'], data=data)
    apply_conversion(doc, doc.page_xref(record['page'] - 1), text, artwork)


GENERIC_FILTERS = {'/FlateDecode', '/ASCII85Decode', '/ASCIIHexDecode', '/LZWDecode', '/RunLengthDecode'}


def lossless_filters(entry):
    """True when a stream is encoded only with general-purpose lossless filters."""
    kind, value = entry
    if kind == 'name':
        return value in GENERIC_FILTERS
    if kind == 'array':
        names = re.findall(r'/[^\s/\[\]<>()]+', value)
        return bool(names) and all(n in GENERIC_FILTERS for n in names)
    return False


def image_layout(doc, xref, data):
    """(width, height, components) of an 8-bit image whose decoded samples are
    `data`, when it has one or three components; otherwise None."""
    key = lambda k: doc.xref_get_key(xref, k)
    if key('Subtype')[1] != '/Image' or key('BitsPerComponent')[1] != '8':
        return None
    (wk, width), (hk, height) = key('Width'), key('Height')
    if wk != 'int' or hk != 'int' or int(width) * int(height) == 0:
        return None
    width, height = int(width), int(height)
    components, rest = divmod(len(data), width * height)
    return None if rest or components not in (1, 3) else (width, height, components)


def exact_palette(doc, xref, data, layout):
    """A pixel-identical indexed version of an 8-bit RGB or gray image, or None."""
    key = lambda k: doc.xref_get_key(xref, k)
    if any(key(k)[0] != 'null' for k in ('Mask', 'Decode')) or key('ImageMask')[1] == 'true':
        return None
    kind, base = key('ColorSpace')
    components = colorspace_components(doc, kind, base)
    width, height, n = layout
    if components != n or width * height < 4096:
        return None
    image = Image.frombytes('RGB' if n == 3 else 'L', (width, height), data).convert('RGB')
    indexed = palette_png(image)
    if indexed is None:
        return None
    return png_stream(indexed[0], base, gray_base=n == 1)


NATIVE_MIN_BYTES = 20_000
CHANGED = ('converted', 'reencoded')


def colorspace_components(doc, kind, base):
    """Components of a DeviceRGB, DeviceGray or ICC-based color space, else None.

    Only these are safe bases for re-encoded samples."""
    if kind == 'name':
        return {'/DeviceRGB': 3, '/DeviceGray': 1}.get(base)
    if kind not in ('xref', 'array'):
        return None
    source = doc.xref_object(int(base.split()[0])) if kind == 'xref' else base
    found = re.fullmatch(r'\s*\[\s*/ICCBased\s+(\d+)\s+0\s+R\s*\]\s*', source)
    return int(doc.xref_get_key(int(found.group(1)), 'N')[1]) if found else None


def native_kind(doc, xref):
    """('jpeg' or 'samples', components, color space value) for an 8-bit gray or
    RGB image stored as JPEG or with lossless filters that can be re-encoded
    at its own resolution, else None. Reads only the image dictionary."""
    key = lambda k: doc.xref_get_key(xref, k)
    if key('Subtype')[1] != '/Image' or key('BitsPerComponent')[1] != '8' or key('ImageMask')[1] == 'true':
        return None
    if any(key(k)[0] != 'null' for k in ('Mask', 'Decode')):
        return None
    (wk, width), (hk, height) = key('Width'), key('Height')
    # Bounded like page renders: every worker holds a few copies of the pixels.
    if wk != 'int' or hk != 'int' or int(width) * int(height) > MAX_PIXELS:
        return None
    kind, base = key('ColorSpace')
    components = colorspace_components(doc, kind, base)
    if components not in (1, 3):
        return None
    flt = key('Filter')
    names = re.findall(r'/[^\s/\[\]<>()]+', flt[1]) if flt[0] == 'array' else [flt[1]]
    # A JPEG, possibly wrapped in general-purpose filters, which MuPDF undoes too.
    if names and names[-1] == '/DCTDecode' and all(n in GENERIC_FILTERS for n in names[:-1]):
        return 'jpeg', components, base
    if flt[0] == 'null' or lossless_filters(flt):
        return 'samples', components, base
    return None


def native_image(doc, xref):
    """(Pillow image of the stored samples, color space value) for an image
    native_kind accepts, else None."""
    found = native_kind(doc, xref)
    if found is None:
        return None
    kind, components, base = found
    mode = 'RGB' if components == 3 else 'L'
    if kind == 'jpeg':
        pix = fitz.Pixmap(doc, xref)
        if pix.alpha or pix.n != components:
            return None
        return Image.frombytes(mode, (pix.width, pix.height), pix.samples), base
    data = doc.xref_stream(xref)
    layout = image_layout(doc, xref, data)
    if layout is None or layout[2] != components:
        return None
    return Image.frombytes(mode, layout[:2], data), base


# The IJG luminance quantization table at quality 50, and its mean at every quality.
IJG_LUMA = [16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55, 14, 13, 16, 24, 40, 57, 69, 56,
            14, 17, 22, 29, 51, 87, 80, 62, 18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
            49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99]
IJG_MEANS = {q: sum(min(255, max(1, int((v * (5000 / q if q < 50 else 200 - 2 * q) + 50) / 100))) for v in IJG_LUMA) / 64
             for q in range(1, 101)}


def stored_jpeg_quality(doc, xref):
    """The JPEG quality an image stream's luminance table is closest to, or None
    when the image is not a JPEG (plain or inside Flate)."""
    kind, value = doc.xref_get_key(xref, 'Filter')
    names = re.findall(r'/[^\s/\[\]<>()]+', value) if kind in ('name', 'array') else []
    if names not in (['/DCTDecode'], ['/FlateDecode', '/DCTDecode']):
        return None
    data = doc.xref_stream_raw(xref)
    try:
        if len(names) == 2:
            data = zlib.decompress(data)
        luma = Image.open(io.BytesIO(data)).quantization[0]
    except Exception:
        return None
    mean = sum(luma) / len(luma)
    return min(IJG_MEANS, key=lambda q: abs(IJG_MEANS[q] - mean))


def reencode_images(doc, xrefs, quality):
    """Re-encode images at their own resolution, within the error of JPEG
    `quality`, keeping only clearly smaller results: [(xref, old bytes, format, data, keys)]."""
    found = []
    for x in xrefs:
        old = len(doc.xref_stream_raw(x))
        if old < NATIVE_MIN_BYTES:
            continue
        try:
            # A JPEG already at or below the preset's quality cannot get smaller
            # within the error that quality allows.
            if (stored_jpeg_quality(doc, x) or 101) <= quality:
                continue
            loaded = native_image(doc, x)
            if loaded is None:
                continue
            image, base = loaded
            fmt, data, _ = best_encoding(image, quality)
            if fmt == 'png':
                data, keys = png_stream(data, base)
            else:
                keys = {'Filter': '/JPXDecode' if fmt == 'jpx' else '/DCTDecode', 'DecodeParms': 'null'}
        except Exception:
            continue
        if len(data) + len(keys.get('ColorSpace', '')) <= old * 0.85:
            found.append((x, old, fmt, data, keys))
    return found


def predicted_image(data, layout):
    """Image samples as a level-9 Flate stream with PNG predictors.

    The samples are unchanged, so the color space, any Decode array and any
    color-key mask still apply."""
    width, height, components = layout
    image = Image.frombytes('L' if components == 1 else 'RGB', (width, height), data)
    return png_stream(encode(image, 'PNG', compress_level=9))


def smaller_encoding(doc, x, palette=True):
    """A smaller lossless encoding of stream `x` as (data, keys), or None.

    Images with few colors try an exact indexed encoding at their own
    resolution (unless `palette` is false), other 8-bit images PNG
    predictors; generic streams are re-deflated at level 9."""
    (wk, width), (hk, height) = doc.xref_get_key(x, 'Width'), doc.xref_get_key(x, 'Height')
    if wk == 'int' and hk == 'int' and int(width) * int(height) > MAX_PIXELS:
        return None  # Decoding it would take more memory than a worker should hold.
    raw = len(doc.xref_stream_raw(x))
    best = (raw, None)
    data = doc.xref_stream(x)
    layout = image_layout(doc, x, data)
    indexed = exact_palette(doc, x, data, layout) if palette and layout and raw >= 16_000 else None
    # An exact palette of the same pixels practically always beats full color.
    predicted = predicted_image(data, layout) if layout and not indexed else None
    for candidate in (indexed, predicted):
        if candidate:
            # Count the predictor parameters: on tiny images they outweigh the gain.
            best = min(best, (len(candidate[0]) + len(candidate[1]['DecodeParms']), candidate), key=lambda b: b[0])
    # Plain Flate still wins on some line art; on large images it is slow and practically never does.
    if lossless_filters(doc.xref_get_key(x, 'Filter')) and (not layout or len(data) < 1 << 20):
        packed = zlib.compress(data, 9)
        best = min(best, (len(packed), (packed, {'Filter': '/FlateDecode', 'DecodeParms': 'null'})), key=lambda b: b[0])
    return best[1]


def recompress_range(path, start, end, catalog):
    """Worker: look at objects start..end-1 of the source PDF.

    Returns the objects carrying XMP metadata (other than the catalog) or
    editing data, the images used as soft masks, and smaller lossless
    encodings of generic streams as (xref, data, dictionary keys that change,
    fallback). A gray image may be a soft mask of an image in another range,
    and a soft mask must not be indexed: when its encoding is a palette, the
    fallback is the best one without."""
    doc = opened(path)
    metadata, pieces, smasks, streams = [], [], [], []
    changed = lambda x, found: found and (found[0], {k: v for k, v in found[1].items() if doc.xref_get_key(x, k)[1] != v})
    for x in range(start, end):
        try:
            if x != catalog and doc.xref_get_key(x, 'Metadata')[0] != 'null':
                metadata.append(x)
            if doc.xref_get_key(x, 'PieceInfo')[0] != 'null':
                pieces.append(x)
            kind, value = doc.xref_get_key(x, 'SMask')
            if kind == 'xref':
                smasks.append(int(value.split()[0]))
            if not doc.xref_is_stream(x) or doc.xref_get_key(x, 'Type')[1] in ('/XRef', '/ObjStm'):
                continue
            flt = doc.xref_get_key(x, 'Filter')
            if not (lossless_filters(flt) or (flt[0] == 'null' and doc.xref_get_key(x, 'Subtype')[1] == '/Image')):
                continue
            found = smaller_encoding(doc, x)
            if found:
                fallback = None
                if 'ColorSpace' in found[1] and colorspace_components(doc, *doc.xref_get_key(x, 'ColorSpace')) == 1:
                    fallback = changed(x, smaller_encoding(doc, x, palette=False)) or False
                streams.append((x, *changed(x, found), fallback))
        except Exception:
            continue
    return metadata, pieces, smasks, streams


def put_stream(doc, xref, data):
    """Replace a stream's bytes and keep its dictionary, filters included.

    Much cheaper than rewriting dictionary keys one by one."""
    pdf = fitz._as_pdf_document(doc)
    fitz.mupdf.pdf_update_stream(pdf, fitz.mupdf.pdf_new_indirect(pdf, xref, 0), fitz.mupdf.fz_new_buffer_from_copied_data(data), 1)


def replace_stream(doc, xref, data, keys):
    """put_stream plus changed dictionary keys; restores the object if that fails."""
    if not keys:
        put_stream(doc, xref, data)
        return
    before, raw = doc.xref_object(xref, compressed=True), doc.xref_stream_raw(xref)
    try:
        put_stream(doc, xref, data)
        for key, value in keys.items():
            doc.xref_set_key(xref, key, value)
    except Exception:
        doc.update_object(xref, before)
        put_stream(doc, xref, raw)
        raise


def remove_extras(doc, remove):
    """Drop data that does not change how any page looks. Editing data is
    found by the lossless workers and dropped by lossless_copy."""
    if 'attachments' in remove:
        for name in doc.embfile_names():
            doc.embfile_del(name)
        for page in doc:
            for annot in list(page.annots(types=[fitz.PDF_ANNOT_FILE_ATTACHMENT])):
                page.delete_annot(annot)
    if 'thumbnails' in remove:
        for page in doc:
            doc.xref_set_key(page.xref, 'Thumb', 'null')
    if 'docmeta' in remove:
        doc.xref_set_key(doc.pdf_catalog(), 'Metadata', 'null')


def lossless_copy(source, destination, pool, progress, cancelled, remove=frozenset()):
    """Drop object-level XMP metadata, chosen extras, and re-encode generic streams as level-9 Flate.

    Workers look at ranges of objects and do the encoding; this process only
    applies their results."""
    label = 'Cleaning up without quality loss'
    with fitz.open(source) as doc:
        remove_extras(doc, remove)
        catalog, count = doc.pdf_catalog(), doc.xref_length()
        step = max(1, -(-count // 256))
        futures = {pool.submit(recompress_range, str(source), start, min(start + step, count), catalog): min(step, count - start)
                   for start in range(1, count, step)}
        removed = recompressed = indexed = done = 0
        smasks, deferred = set(), []

        def apply(x, data, keys):
            nonlocal recompressed, indexed
            try:
                replace_stream(doc, x, data, keys)
            except Exception:
                return
            recompressed += 1
            indexed += 'ColorSpace' in keys

        progress(label, 0, count - 1)
        try:
            for future in as_completed(futures):
                cancelled()
                metadata, pieces, masks, streams = future.result()
                smasks.update(masks)
                for x, data, keys, fallback in streams:
                    if fallback is None:
                        apply(x, data, keys)
                    else:
                        deferred.append((x, data, keys, fallback))
                for x in metadata:
                    doc.xref_set_key(x, 'Metadata', 'null')
                removed += len(metadata)
                if 'editing' in remove:
                    for x in pieces:
                        doc.xref_set_key(x, 'PieceInfo', 'null')
                done += futures[future]
                progress(label, done, count - 1)
        finally:
            for future in futures:
                future.cancel()
        for x, data, keys, fallback in deferred:
            if x not in smasks:
                apply(x, data, keys)
            elif fallback:
                apply(x, *fallback)
        doc.save(destination, garbage=2, deflate=True, use_objstms=1, compression_effort=100)
    return {'metadataRemoved': removed, 'streamsRecompressed': recompressed, 'imagesIndexed': indexed}


def page_objects(path, start, end):
    """Worker: for pages start..end-1, their content streams, the objects they
    draw (images, masks, reusable artwork) and the images among those, plus
    the stored size of each of these objects."""
    doc = opened(path)
    pages, sizes = [], {}
    for number in range(start, end):
        page = doc[number]
        images = page.get_images(full=True)
        owned = {i[0] for i in images} | {i[1] for i in images if i[1]} | {x[0] for x in page.get_xobjects()}
        contents = set(page.get_contents())
        for x in contents | owned:
            if x not in sizes:
                sizes[x] = len(doc.xref_stream_raw(x) or b'') if doc.xref_is_stream(x) else 0
        pages.append((contents, owned, {i[0] for i in images}))
    return pages, sizes


def page_budgets(path, pool=None):
    """Per page: the bytes it would release if its drawing were replaced (page
    contents plus images and artwork no other page uses), and the images it
    draws that no other page uses. Pages are read in parallel when a pool is given."""
    with fitz.open(path) as doc:
        count = len(doc)
    step = max(1, -(-count // 64))
    ranges = [(start, min(start + step, count)) for start in range(0, count, step)]
    if pool is None:
        parts = [page_objects(str(path), start, end) for start, end in ranges]
    else:
        parts = pool.map(page_objects, [str(path)] * len(ranges), *zip(*ranges))
    per_page, sizes, uses = [], {}, Counter()
    for pages, part_sizes in parts:
        per_page += pages
        sizes.update(part_sizes)
    for _, owned, _ in per_page:
        uses.update(owned)
    budgets = [sum(sizes[x] for x in contents) + sum(sizes[x] for x in owned if uses[x] == 1)
               for contents, owned, _ in per_page]
    return budgets, [sorted(x for x in drawn if uses[x] == 1) for _, _, drawn in per_page]


def assemble(base, records, destination):
    with fitz.open(base) as doc, ExitStack() as stack:
        spools = {}
        for record in records:
            path = record['spooled'][0]
            if path not in spools:
                spools[path] = stack.enter_context(open(path, 'rb'))
            apply_record(doc, record, load_blobs(record, spools[path]))
        doc.save(destination, garbage=2, deflate=True, use_objstms=1, compression_effort=100)
    return Path(destination).stat().st_size


def link_signature(page):
    return sorted(repr({k: (tuple(v) if isinstance(v, (fitz.Point, fitz.Rect)) else v)
                        for k, v in sorted(link.items()) if k not in {'xref', 'id'}}) for link in page.get_links())


CHECKS = ('textDifferences', 'linkDifferences', 'boxDifferences', 'renderFailures', 'lookDifferences')
# Largest allowed mean gray difference in any 8x8 block of a 36 DPI render.
# Sound copies of the measured books stayed under 100: edges soften by a
# pixel and photos carry compression noise. A missing, blacked-out or
# recolored image region goes well beyond.
LOOK_LIMIT = 150


def looks_alike(before, after):
    """True unless some area of about 0.2 inch of two 36 DPI renders looks clearly different."""
    a, b = (Image.frombytes('RGB', (p.width, p.height), p.samples).convert('L') for p in (before, after))
    return a.size == b.size and ImageChops.difference(a, b).reduce(8).getextrema()[1] <= LOOK_LIMIT


def compare_pages(source, output, start, end):
    """Worker: compare pages start..end-1 of the copy with the original."""
    result = {'textPages': 0, **{k: [] for k in CHECKS}}
    with fitz.open(source) as a, fitz.open(output) as b:
        for i in range(start, end):
            pa, pb = a[i], b[i]
            text = source_text(pa)
            result['textPages'] += bool(text.strip())
            if text != source_text(pb):
                result['textDifferences'].append(i + 1)
            if link_signature(pa) != link_signature(pb):
                result['linkDifferences'].append(i + 1)
            if pa.mediabox != pb.mediabox or pa.cropbox != pb.cropbox or pa.rotation != pb.rotation:
                result['boxDifferences'].append(i + 1)
            try:
                before = pa.get_pixmap(dpi=36, alpha=False)
            except Exception:
                continue  # The original does not render either: nothing to compare.
            try:
                after = pb.get_pixmap(dpi=36, alpha=False)
            except Exception:
                result['renderFailures'].append(i + 1)
                continue
            if not looks_alike(before, after):
                result['lookDifferences'].append(i + 1)
    return result


def validate(source, output, progress, cancelled, pool=None):
    """Compare the copy with the original on every page, in parallel when a pool is given."""
    with fitz.open(source) as a, fitz.open(output) as b:
        if len(a) != len(b):
            raise RuntimeError('The compressed copy has a different page count.')
        count, toc = len(a), a.get_toc()
        result = {'pages': count, 'textPages': 0, **{k: [] for k in CHECKS}, 'bookmarks': len(toc), 'bookmarksMatch': toc == b.get_toc()}
    step = max(20, -(-count // 48))
    ranges = [(start, min(start + step, count)) for start in range(0, count, step)]
    label = 'Checking every page against the original'
    progress(label, 0, count)
    if pool is None:
        parts = (compare_pages(source, output, start, end) for start, end in ranges)
    else:
        futures = [pool.submit(compare_pages, str(source), str(output), start, end) for start, end in ranges]
        parts = (future.result() for future in futures)
    try:
        for (start, end), part in zip(ranges, parts):
            cancelled()
            result['textPages'] += part['textPages']
            for key in CHECKS:
                result[key] += part[key]
            progress(label, end, count)
    finally:
        if pool is not None:
            for future in futures:
                future.cancel()
    result['passed'] = result['bookmarksMatch'] and not any(result[k] for k in CHECKS)
    return result


def part_bytes(doc, first, last, toc):
    """A standalone PDF of pages first..last (0-based, inclusive), with their bookmarks."""
    with fitz.open() as part:
        part.insert_pdf(doc, from_page=first, to_page=last)
        entries, level = [], 0
        for lvl, title, page, *_ in toc:
            if first < page <= last + 1:
                # Bookmarks whose parent lies in another part move up to keep a valid outline.
                level = min(lvl, level + 1)
                entries.append([level, title, page - first])
        if entries:
            part.set_toc(entries)
        return part.tobytes(garbage=2, deflate=True, use_objstms=1)


def split_pdf(path, limit, folder, stem, progress, cancelled):
    """Split into consecutive page ranges that each stay under `limit` bytes.

    Each range is the longest that measures under the limit, found by binary
    search on real part sizes. A single page larger than the limit stays alone.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    parts = []
    with fitz.open(path) as doc:
        count, toc, first = len(doc), doc.get_toc(simple=False), 0
        while first < count:
            cancelled()
            progress('Splitting into parts', first, count)
            lo, hi, best = first, count - 1, None
            while lo <= hi:
                mid = (lo + hi) // 2
                data = part_bytes(doc, first, mid, toc)
                if len(data) <= limit:
                    best, lo = (mid, data), mid + 1
                else:
                    hi = mid - 1
            last, data = best or (first, part_bytes(doc, first, first, toc))
            parts.append({'firstPage': first + 1, 'lastPage': last + 1, 'bytes': len(data), 'data': data})
            first = last + 1
        for i, part in enumerate(parts, 1):
            name = f'{stem}-part{i}-of-{len(parts)}.pdf'
            (folder / name).write_bytes(part.pop('data'))
            part.update(name=name, fits=part['bytes'] <= limit)
            with fitz.open(folder / name) as copy:
                for offset in range(len(copy)):
                    if source_text(copy[offset]) != source_text(doc[part['firstPage'] - 1 + offset]):
                        raise RuntimeError('A split part did not match the compressed copy. Your original is unchanged.')
    return parts


def compress(source, destination, workdir, options, progress=lambda *a: None, cancelled=lambda: None, workers=None):
    """Write a smaller, verified copy of `source` to `destination`.

    options: {'mode': 'quality'|'target', 'preset': name, 'targetBytes': int,
              'remove': [extras to drop], 'keepPages': [page numbers never redrawn],
              'split': bool, 'name': original file name}
    With `split`, a copy that still exceeds the limit is also written as parts
    that each fit, zipped next to `destination`.
    In target mode `preset` is the lowest quality allowed; higher presets are tried
    first, starting with the lossless cleanup alone.
    """
    started = time.monotonic()
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    mode, preset = options.get('mode', 'quality'), options.get('preset', 'high')
    if preset not in PRESETS or mode not in ('quality', 'target'):
        raise ValueError('Unknown compression setting.')
    target = int(options['targetBytes']) if mode == 'target' else None
    if target is not None and target <= 0:
        raise ValueError('Choose a size limit above zero.')
    remove = frozenset(options.get('remove', ()))
    if not remove <= REMOVALS:
        raise ValueError('Unknown cleanup option.')
    keep = {int(n) for n in options.get('keepPages', ())}
    with fitz.open(source) as doc:
        if not doc.is_pdf or len(doc) == 0:
            raise ValueError('Choose a PDF with at least one page.')
        if doc.needs_pass:
            raise ValueError('This PDF is password protected. Open an unlocked copy to compress it.')
    original = Path(source).stat().st_size
    base = workdir / 'lossless.pdf'
    output = workdir / 'candidate.pdf'
    if preset == 'lossless':
        presets = []
    else:
        presets = [preset] if mode == 'quality' else ORDER[:ORDER.index(preset) + 1]
    selected, records, used, size = [], [], None, None
    # Leave two hardware threads for the interface and the system.
    workers = workers or max(1, min(16, (os.cpu_count() or 2) - 2))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        cleanup = lossless_copy(source, base, pool, progress, cancelled, remove)
        lossless = base.stat().st_size
        report = {'mode': mode, 'targetBytes': target, 'originalBytes': original, 'losslessBytes': lossless,
                  'lossless': cleanup, 'removed': sorted(remove)}
        budgets, own_images = page_budgets(base, pool)
        excluded = {n for n in keep if 1 <= n <= len(budgets)}
        candidates = sorted(((n + 1, b, own_images[n]) for n, b in enumerate(budgets) if b >= MIN_PAGE_BYTES and n + 1 not in excluded),
                            key=lambda c: -c[1])

        def build(pages):
            cancelled()
            progress('Building the copy', 0, 1)
            if pages:
                return assemble(base, pages, output)
            shutil.copyfile(base, output)
            return lossless

        if presets and (mode == 'quality' or lossless > target):
            for used in presets:
                records = prepare_all(pool, base, candidates, used, workdir, progress, cancelled)
                selected = sorted((r for r in records if r['status'] in CHANGED), key=lambda r: -r['estimatedSaving'])
                size = build(selected)
                if target is None or size <= target:
                    break
            if target is not None and size is not None and size <= target:
                selected, size = fewest_pages(selected, size, lossless, target, build)
        if size is None:
            size = build(selected)
        checks = validate(source, output, progress, cancelled, pool)
        bad = set(checks['textDifferences'] + checks['linkDifferences'] + checks['renderFailures'] + checks['lookDifferences'])
        if bad and bad <= {r['page'] for r in selected}:
            # Keep any page that failed the whole-document check in its original form.
            for r in selected:
                if r['page'] in bad:
                    r['status'] = 'text_difference'
            selected = [r for r in selected if r['page'] not in bad]
            build(selected)
            checks = validate(source, output, progress, cancelled, pool)
    if not checks['passed']:
        raise RuntimeError('The compressed copy did not pass the page checks. Your original is unchanged.')
    shutil.move(output, destination)
    size = Path(destination).stat().st_size
    if target is not None and size > target and options.get('split'):
        stem = Path(options.get('name') or 'document.pdf').stem[:120] or 'document'
        parts = split_pdf(destination, target, workdir / 'parts', stem, progress, cancelled)
        archive = Path(destination).with_suffix('.zip')
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_STORED) as bundle:
            for part in parts:
                bundle.write(workdir / 'parts' / part['name'], part['name'])
        report['parts'] = parts
    chosen = {r['page'] for r in selected}
    kept = Counter(r['status'] for r in records if r['page'] not in chosen and r['status'] not in CHANGED)
    kept['excluded'] += len(excluded)
    kept['small'] += len(budgets) - len(candidates) - len(excluded)
    report.update(
        bytes=size, savedBytes=original - size, preset=used or preset,
        dpi=PRESETS[used or preset]['dpi'], targetMet=None if target is None else size <= target,
        pages=len(budgets), checks=checks, seconds=round(time.monotonic() - started, 1),
        converted=sorted(({**{k: r.get(k) for k in ('page', 'eligibleBytes', 'imageBytes', 'textBytes', 'format', 'gray')},
                           'change': 'images' if r['status'] == 'reencoded' else 'redrawn',
                           'afterBytes': r['eligibleBytes'] - r['estimatedSaving']} for r in selected), key=lambda r: r['page']),
        kept=[{'reason': k, 'label': REASONS.get(k, k), 'pages': v} for k, v in kept.most_common() if v],
        unconvertedCandidates=sum(1 for r in records if r['status'] in CHANGED and r['page'] not in chosen),
    )
    return report


def prepare_all(pool, base, candidates, preset, workdir, progress, cancelled):
    """Prepare every candidate page (number, budget, own images) in parallel,
    largest first. New page data goes to one spool file in `workdir`."""
    futures = [pool.submit(prepare_page, str(base), n, b, preset, str(workdir), False, images) for n, b, images in candidates]
    records, label = [], f'Making heavy pages smaller · {PRESETS[preset]["label"]}'
    spool = Path(workdir) / f'{preset}.artwork'
    progress(label, 0, len(candidates))
    try:
        with spool.open('wb') as out:
            for future in as_completed(futures):
                cancelled()
                record = future.result()
                if 'blobs' in record:
                    spans = []
                    for blob in record.pop('blobs'):
                        spans.append((out.tell(), len(blob)))
                        out.write(blob)
                    record['spooled'] = [str(spool), spans]
                records.append(record)
                progress(label, len(records), len(candidates))
    finally:
        for future in futures:
            future.cancel()
    return records


def fewest_pages(converted, size, lossless, target, build):
    """Redraw as few pages as possible while staying under `target`.

    `converted` fits when all of it is used. Per-page estimates are calibrated
    against that measured result, then each smaller selection is measured too.
    """
    estimated = sum(r['estimatedSaving'] for r in converted)
    scale = (lossless - size) / estimated if estimated > 0 else 1
    goal, chosen, remaining = target * 0.97, [], lossless
    for r in converted:
        if remaining <= goal:
            break
        chosen.append(r)
        remaining -= r['estimatedSaving'] * scale
    for _ in range(3):
        if len(chosen) == len(converted):
            break
        measured = build(chosen)
        if measured <= target:
            return chosen, measured
        need, extra = measured - goal, converted[len(chosen):]
        for r in extra:
            if need <= 0:
                break
            chosen.append(r)
            need -= r['estimatedSaving'] * scale
    return converted, build(converted)
