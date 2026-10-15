"""Read-only PDF accounting. Stream payloads are counted once, by xref."""
from concurrent.futures import ProcessPoolExecutor
import multiprocessing
import os
import re
from collections import defaultdict
from pathlib import Path
import pymupdf as fitz

CATEGORIES = {
    'images': ('Images', 'Photos, scans and illustrations stored as pictures, including their transparency masks.'),
    'content': ('Text & drawings', 'The instructions that draw each page: text, lines and shapes.'),
    'fonts': ('Fonts', 'Typefaces embedded so the PDF looks the same on every computer.'),
    'forms': ('Shared drawings', 'Artwork defined once and placed on one or more pages.'),
    'metadata': ('Metadata', 'Descriptive data about the document or its parts, such as editing history.'),
    'attachments': ('Attachments', 'Files attached inside the PDF.'),
    'containers': ('Packed objects', 'Small objects compressed together. Their individual sizes can’t be separated.'),
    'other': ('Other data', 'Color profiles, character maps and other supporting data.'),
    'structure': ('PDF structure', 'Indexes and bookkeeping that hold the file together, including older saved versions.'),
}
CODECS = {'JPXDecode': 'JPEG 2000', 'DCTDecode': 'JPEG', 'FlateDecode': 'Flate', 'JBIG2Decode': 'JBIG2', 'CCITTFaxDecode': 'CCITT'}

REFERENCE=re.compile(r'(?<![\w./])(\d+)\s+\d+\s+R\b')
LITERAL=re.compile(r'\((?:[^()\\]|\\.)*\)',re.S)  # innermost literal string
HEX=re.compile(r'(?<!<)<(?!<)[^>]*>')
COMMENT=re.compile(r'%[^\r\n]*')

def references(s):
    # Ignore literal/hex strings and comments; a string containing "12 0 R"
    # must not create a false resource reference. Most objects have none.
    if '(' in s or '%' in s or '<' in s.replace('<<',''):
        while True:  # strip nested strings from the inside out
            stripped=LITERAL.sub(' ',s)
            if stripped==s:break
            s=stripped
        s=HEX.sub(' ',COMMENT.sub(' ',s))
    return {int(x) for x in REFERENCE.findall(s)}

PARALLEL_OBJECTS=20_000

# In MuPDF's object text, top-level keys sit on lines indented by exactly two spaces.
TOP_KEY=re.compile(r'^  /(Type|Subtype|Filter|FontFile[23]?) (.*)$',re.M)

def read_object(doc,x):
    """Object text, its references, type, font programs, and for streams the
    stored size, subtype and filter. Keys are read from the object text:
    on files with many objects, separate key lookups dominate."""
    # A cross-reference entry whose object is missing reads as null, as in viewers.
    try:obj=doc.xref_object(x)
    except Exception:return 'null',set(),'null',set(),None,None,None
    found=dict(TOP_KEY.findall(obj)) if '\n  /' in obj else {}
    # Arrays are compacted the way xref_get_key writes them.
    key=lambda k:re.sub(r'\s+','',found[k]) if found.get(k,'').startswith('[') else found.get(k,'null').strip()
    typ=key('Type');fonts=set()
    if typ=='/FontDescriptor':
        for k in ('FontFile','FontFile2','FontFile3'):fonts|=references(key(k))
    if not doc.xref_is_stream(x):return obj,references(obj),typ,fonts,None,None,None
    return obj,references(obj),typ,fonts,len(doc.xref_stream_raw(x) or b''),key('Subtype'),key('Filter')

def read_range(path,start,end):
    with fitz.open(path) as doc:return [(x,read_object(doc,x)) for x in range(start,end)]

def read_all(path,doc,progress=None):
    """Read every object; split across processes for files with many objects."""
    total=doc.xref_length()-1
    if total<=PARALLEL_OBJECTS:
        for x in range(1,total+1):
            if progress and (x==1 or x%250==0):progress('Reading the file',x,total)
            yield x,read_object(doc,x)
        return
    workers=max(2,min(16,(os.cpu_count() or 2)-2));step=-(-total//(workers*4));done=0
    with ProcessPoolExecutor(max_workers=workers,mp_context=multiprocessing.get_context('spawn')) as pool:
        futures=[pool.submit(read_range,str(path),start,min(start+step,total+1)) for start in range(1,total+1,step)]
        for future in futures:
            rows=future.result();done+=len(rows)
            if progress:progress('Reading the file',done,total)
            yield from rows

def inspect_pdf(path, progress=None):
    path=Path(path)
    with fitz.open(path) as doc:
        if not doc.is_pdf:raise ValueError('Choose a PDF file.')
        if doc.needs_pass:raise ValueError('This PDF is password protected. Open an unlocked copy for this prototype.')
        if len(doc)==0:raise ValueError('This PDF has no pages.')
        objects={};types={};streams={};font_programs=set();page_contents=set();owners=defaultdict(set);edges={};names={}
        for x,(obj,refs,typ,fonts,size,subtype,codec) in read_all(path,doc,progress):
            objects[x]=obj;edges[x]=refs;types[x]=typ;font_programs|=fonts
            if size is not None:streams[x]=size;names[x]=(subtype,codec)
        closure_cache={}
        def stream_closure(start):
            if start in closure_cache:return closure_cache[start]
            seen=set();found=set();todo=[start]
            while todo:
                x=todo.pop()
                if x in seen or types.get(x) in ('/Page','/Pages','/Catalog'):continue
                seen.add(x)
                if x in streams:found.add(x)
                todo.extend(edges.get(x,set())-seen)
            closure_cache[start]=found;return found
        pages=[];page_xrefs=[]
        for i,page in enumerate(doc):
            if progress and i % 20 == 0:
                progress('Following each page', i, len(doc))
            contents=set(page.get_contents());page_contents.update(contents);page_xrefs.append(page.xref)
            # Include annotation appearances and thumbnails as page references.
            # Parent page-tree references are stopped by stream_closure.
            roots=references(objects[page.xref]) | contents
            parent=page.xref;parents=set()
            while parent and parent not in parents:
                parents.add(parent);kind,val=doc.xref_get_key(parent,'Resources')
                if kind!='null':roots.update(references(val));break
                refs=references(doc.xref_get_key(parent,'Parent')[1]);parent=next(iter(refs),None)
            refs=set()
            for root in roots:refs.update(stream_closure(root))
            for x in refs:owners[x].add(i+1)
            pages.append({'number':i+1,'label':page.get_label() or str(i+1),'objects':sorted(refs),'width':round(page.rect.width,2),'height':round(page.rect.height,2)})
        rows=[]
        if progress:progress('Adding up sizes', 0, 1)
        for x,n in streams.items():
            (subtype,rawcodec),typ=names[x],types[x]
            cat=('images' if subtype=='/Image' else 'fonts' if x in font_programs else 'content' if x in page_contents else 'forms' if subtype=='/Form' else 'containers' if typ=='/ObjStm' else 'metadata' if typ=='/Metadata' else 'attachments' if typ=='/EmbeddedFile' else 'other')
            rawcodec=rawcodec.strip('/');codec=CODECS.get(rawcodec,rawcodec if rawcodec!='null' else 'Uncompressed')
            rows.append({'id':x,'category':cat,'bytes':n,'pages':sorted(owners[x]),'codec':codec})
        total=path.stat().st_size;payload=sum(streams.values());remainder=total-payload
        if remainder<0:raise ValueError('The stream sizes overlap in this PDF. Exact accounting is unavailable; try a repaired copy.')
        rows.append({'id':0,'category':'structure','bytes':remainder,'pages':[],'codec':'Mixed'})
        for p in pages:
            p['exclusiveBytes']=sum(streams[x] for x in p['objects'] if len(owners[x])==1)
            p['sharedBytes']=sum(streams[x] for x in p['objects'] if len(owners[x])>1)
            p['referencedBytes']=p['exclusiveBytes']+p['sharedBytes']
        categories=[{'id':c,'label':label,'description':description,'bytes':sum(r['bytes'] for r in rows if r['category']==c),'count':sum(1 for r in rows if r['category']==c)} for c,(label,description) in CATEGORIES.items()]
        categories=[c for c in categories if c['bytes']]
        # Data that can be dropped without changing how pages look; sizes are
        # the streams reachable only through each entry point.
        def reachable(roots):
            found=set()
            for root in roots:found|=stream_closure(root) if root not in streams else {root}
            return found
        catalog=doc.pdf_catalog()
        editing=reachable({r for x,obj in objects.items() if '/PieceInfo' in obj and (v:=doc.xref_get_key(x,'PieceInfo'))[0]!='null' for r in references(v[1])})
        thumbs=reachable({r for x in page_xrefs for r in references(doc.xref_get_key(x,'Thumb')[1])})
        docmeta=reachable(references(doc.xref_get_key(catalog,'Metadata')[1]))
        removable=[{'id':'editing','bytes':sum(streams[x] for x in editing),'count':len(editing)},
                   {'id':'thumbnails','bytes':sum(streams[x] for x in thumbs),'count':len(thumbs)},
                   {'id':'attachments','bytes':sum(r['bytes'] for r in rows if r['category']=='attachments'),'count':doc.embfile_count()},
                   {'id':'docmeta','bytes':sum(streams[x] for x in docmeta),'count':len(docmeta)}]
        removable=[r for r in removable if r['count']]
        # Bookmarks down to level 4, for splitting at chapters.
        outline=[[level,title,page] for level,title,page in doc.get_toc() if level<=4 and page>=1]
        return {'name':path.name,'bytes':total,'pageCount':len(doc),'objectCount':len(streams),'streamBytes':payload,'remainderBytes':remainder,'sharedBytes':sum(streams[x] for x in streams if len(owners[x])>1),'categories':categories,'removable':removable,'objects':sorted(rows,key=lambda r:r['bytes'],reverse=True),'pages':pages,'outline':outline,'accounting':'Encoded stream payloads are counted once. File structure includes all remaining bytes. Page costs include referenced resources, which may be unused; shared page costs cannot be added together.','repaired':doc.is_repaired}

def object_info(path,xref):
    with fitz.open(path) as doc:
        if not 0<xref<doc.xref_length():return {'dictionary':'Structural bytes have no single object dictionary.'}
        info={'dictionary':doc.xref_object(xref)[:16000]}
        if doc.xref_get_key(xref,'Subtype')[1]=='/Image':
            # Read on demand: per-image details are slow to collect for every image up front.
            value=lambda k:doc.xref_get_key(xref,k)[1]
            info['image']={'width':int(value('Width')) if value('Width').isdigit() else None,'height':int(value('Height')) if value('Height').isdigit() else None,
                           'colorSpace':value('ColorSpace'),'bitsPerComponent':value('BitsPerComponent'),'mask':value('ImageMask')=='true'}
        return info

def preview(path,kind,number,width=1000):
    width=max(200,min(int(width),3200))
    with fitz.open(path) as doc:
        if kind=='page':
            if not 1<=number<=len(doc):raise ValueError('Page not found.')
            p=doc[number-1];pix=p.get_pixmap(matrix=fitz.Matrix(width/p.rect.width,width/p.rect.width),alpha=False)
        elif kind=='image':
            if not 0<number<doc.xref_length() or doc.xref_get_key(number,'Subtype')[1]!='/Image':raise ValueError('Image not found.')
            w=int(doc.xref_get_key(number,'Width')[1]);h=int(doc.xref_get_key(number,'Height')[1])
            if w*h>50_000_000:raise ValueError('This image is too large for an individual preview. View its page instead.')
            pix=fitz.Pixmap(doc,number)
            if pix.colorspace is None:pix=fitz.Pixmap(fitz.csGRAY,pix.width,pix.height,pix.samples,False)
            elif pix.colorspace.n>3:pix=fitz.Pixmap(fitz.csRGB,pix)
            while max(pix.width,pix.height)>1400:pix.shrink(1)
        else:raise ValueError('Unknown preview type.')
        return pix.tobytes('png')
