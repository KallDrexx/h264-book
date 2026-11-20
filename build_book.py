#!/usr/bin/env python3
"""Build EPUB and PDF editions of the H.264 book.

Usage: ./build_book.py REVISION [--out DIR] [--no-pdf] [--no-epub]

Requires: python3 with `markdown-it-py`; chromium (or google-chrome) for PDF.
Chapters are every "Chapter N.M - ..." directory's Readme.md in numeric order,
(Summary.md is not included). Each chapter starts a new page.
"""
import argparse, datetime, html, re, shutil, subprocess, sys, tempfile, uuid, zipfile
from pathlib import Path

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parent
TITLE = "Build an H.264 Decoder from Scratch"
SUBTITLE = "A step-by-step, chapter-by-chapter guide"
AUTHOR = "Matthew Shapiro"

CSS = """
body { font-family: Georgia, 'Times New Roman', serif; line-height: 1.45; font-size: 11pt; }
h1, h2, h3, h4 { font-family: Helvetica, Arial, sans-serif; line-height: 1.2; }
h1 { font-size: 1.9em; border-bottom: 2px solid #444; padding-bottom: .2em; margin-top: 0; }
h2 { font-size: 1.4em; margin-top: 1.6em; } h3 { font-size: 1.15em; margin-top: 1.3em; }
pre { background: #f4f4f4; border: 1px solid #ddd; padding: .6em; font-size: 8.5pt;
      white-space: pre-wrap; word-wrap: break-word; page-break-inside: auto; }
code { font-family: 'DejaVu Sans Mono', Menlo, Consolas, monospace; font-size: .88em; }
:not(pre) > code { background: #f1f1f1; padding: 0 .2em; }
table { border-collapse: collapse; margin: 1em 0; font-size: .9em; }
th, td { border: 1px solid #aaa; padding: .25em .5em; vertical-align: top; }
th { background: #eee; } tr { page-break-inside: avoid; }
blockquote { border-left: 3px solid #bbb; margin-left: 0; padding-left: 1em; color: #444; }
img { max-width: 100%; }
a { color: #1a4f9c; }
.chapter { page-break-before: always; break-before: page; }
.titlepage { text-align: center; padding-top: 28%; }
.titlepage h1 { font-size: 2.6em; border: none; }
.titlepage .sub { font-size: 1.3em; color: #444; margin: .5em 0 2.5em; }
.titlepage .meta { font-size: 1.1em; margin-top: 3em; }
.toc ul { list-style: none; padding-left: 0; } .toc ul ul { padding-left: 1.6em; }
.toc li { margin: .15em 0; } .toc > ul > li { margin-top: .5em; font-weight: bold; }
.toc ul ul li { font-weight: normal; font-size: .92em; }
"""


def chapter_key(p):
    m = re.match(r"Chapter (\d+)\.(\d+)", p.parent.name)
    return (int(m[1]), int(m[2]))


def find_sources():
    chaps = [p for p in ROOT.glob("Chapter *.*/Readme.md") if re.match(r"Chapter \d+\.\d+", p.parent.name)]
    return sorted(chaps, key=chapter_key)


def render_chapter(idx, path):
    """Return (title, body_html, [(h2_title, id)]) with ids/anchors assigned."""
    md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    tokens = md.parse(path.read_text(encoding="utf-8"))
    title, subs, n = path.parent.name, [], 0
    for i, t in enumerate(tokens):
        if t.type != "heading_open":
            continue
        text = tokens[i + 1].content
        if t.tag == "h1" and n == 0 and not subs:
            title = text
            t.attrSet("id", f"ch{idx}")
        elif t.tag == "h2":
            n += 1
            sid = f"ch{idx}-s{n}"
            t.attrSet("id", sid)
            subs.append((text, sid))
    body = md.renderer.render(tokens, md.options, {})
    return title, f'<section class="chapter">\n{body}</section>', subs


def build_toc(chapters, file_for, with_subs=True):
    """file_for(i) -> filename prefix ('' for single file)."""
    out = ["<ul>"]
    for i, (title, _, subs) in enumerate(chapters):
        f = file_for(i)
        out.append(f'<li><a href="{f}#ch{i}">{html.escape(title)}</a>')
        if with_subs and subs:
            out.append("<ul>" + "".join(
                f'<li><a href="{f}#{sid}">{html.escape(t)}</a></li>' for t, sid in subs) + "</ul>")
        out.append("</li>")
    out.append("</ul>")
    return "\n".join(out)


def titlepage(rev):
    date = datetime.date.today().isoformat()
    return f"""<section class="titlepage">
<h1>{html.escape(TITLE)}</h1>
<p class="sub">{html.escape(SUBTITLE)}</p>
<p class="meta">{html.escape(AUTHOR)}</p>
<p class="meta">Revision {html.escape(rev)}<br/>{date}</p>
</section>"""


def xhtml(title, body, extra_head=""):
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">
<head><meta charset="utf-8"/><title>{html.escape(title)}</title>
<link rel="stylesheet" type="text/css" href="style.css"/>{extra_head}</head>
<body>
{body}
</body></html>
"""


def build_epub(rev, chapters, out):
    bid = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, 'h264-book')}"
    files = {}  # name -> text
    files["style.css"] = CSS.replace("page-break-before: always; break-before: page;", "")
    files["title.xhtml"] = xhtml("Title page", titlepage(rev))
    toc_body = ('<nav epub:type="toc" id="toc" class="toc"><h1>Contents</h1>'
                + build_toc(chapters, lambda i: f"ch{i:02d}.xhtml") + "</nav>")
    files["nav.xhtml"] = xhtml("Contents", toc_body)
    for i, (title, body, _) in enumerate(chapters):
        # chapter files start on a new spine item, which EPUB readers paginate
        files[f"ch{i:02d}.xhtml"] = xhtml(title, body.replace('class="chapter"', 'class="c"'))
    items = ['<item id="css" href="style.css" media-type="text/css"/>',
             '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
             '<item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>',
             '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>']
    spine = ['<itemref idref="title"/>', '<itemref idref="nav"/>']
    for i in range(len(chapters)):
        items.append(f'<item id="c{i}" href="ch{i:02d}.xhtml" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="c{i}"/>')
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="bid">{bid}</dc:identifier>
<dc:title>{html.escape(TITLE)}</dc:title>
<dc:creator>{html.escape(AUTHOR)}</dc:creator>
<dc:language>en</dc:language>
<meta property="dcterms:modified">{now}</meta>
<meta name="revision" content="{html.escape(rev)}"/>
</metadata>
<manifest>{"".join(items)}</manifest>
<spine toc="ncx">{"".join(spine)}</spine>
<guide><reference type="toc" title="Contents" href="nav.xhtml"/></guide>
</package>"""
    navpoints = "".join(
        f'<navPoint id="n{i}" playOrder="{i + 3}"><navLabel><text>{html.escape(t)}</text></navLabel>'
        f'<content src="ch{i:02d}.xhtml#ch{i}"/></navPoint>' for i, (t, _, _) in enumerate(chapters))
    ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="{bid}"/></head>
<docTitle><text>{html.escape(TITLE)}</text></docTitle>
<navMap><navPoint id="t" playOrder="1"><navLabel><text>Title page</text></navLabel><content src="title.xhtml"/></navPoint>
<navPoint id="c" playOrder="2"><navLabel><text>Contents</text></navLabel><content src="nav.xhtml"/></navPoint>{navpoints}</navMap></ncx>"""
    container = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>"""
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/toc.ncx", ncx, compress_type=zipfile.ZIP_DEFLATED)
        for name, text in files.items():
            z.writestr(f"OEBPS/{name}", text, compress_type=zipfile.ZIP_DEFLATED)


def build_pdf(rev, chapters, out):
    browser = next((b for b in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
                    if shutil.which(b)), None)
    if not browser:
        sys.exit("PDF needs chromium or google-chrome on PATH (or use --no-pdf)")
    toc = ('<section class="chapter toc"><h1 id="toc">Contents</h1>'
           + build_toc(chapters, lambda i: "") + "</section>")
    page = (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"/><title>{html.escape(TITLE)}</title>'
            f'<style>@page {{ size: Letter; margin: 0.9in; }}\n{CSS}</style></head><body>'
            + titlepage(rev) + toc + "\n".join(b for _, b, _ in chapters) + "</body></html>")
    # snap-packaged chromium can only read/write under $HOME, so stage inside the project
    stage = Path(tempfile.mkdtemp(prefix=".pdfbuild-", dir=ROOT))
    try:
        (stage / "book.html").write_text(page, encoding="utf-8")
        tmp_pdf = stage / "book.pdf"
        subprocess.run([browser, "--headless", "--disable-gpu", "--no-sandbox", "--no-pdf-header-footer",
                        f"--user-data-dir={stage / 'profile'}",
                        f"--print-to-pdf={tmp_pdf}", (stage / "book.html").as_uri()],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.copy(tmp_pdf, out)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("revision", help="revision label shown on the title page, e.g. 3 or 1.2")
    ap.add_argument("--out", default=str(ROOT / "output"), help="output directory (default: ./output)")
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--no-epub", action="store_true")
    a = ap.parse_args()

    chapters = [render_chapter(i, p) for i, p in enumerate(find_sources())]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"h264-book-rev{a.revision}"
    if not a.no_epub:
        build_epub(a.revision, chapters, out / f"{stem}.epub")
        print("wrote", out / f"{stem}.epub")
    if not a.no_pdf:
        build_pdf(a.revision, chapters, out / f"{stem}.pdf")
        print("wrote", out / f"{stem}.pdf")


if __name__ == "__main__":
    main()
