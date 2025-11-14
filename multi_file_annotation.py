import typing as typ
import argparse, os
from pdfminer.layout import (LAParams, LTAnno, LTChar, LTComponent, LTContainer, LTFigure, LTItem,
                             LTPage, LTTextBox, LTTextLine)
from pdfannots.types import Page, Outline, AnnotationType, Annotation, Document, RGB
from pdfminer.pdfinterp import PDFResourceManager, PDFPageInterpreter
from pdfminer.pdfparser import PDFParser
from pdfminer.pdfdocument import PDFDocument, PDFNoOutlines
from pdfannots import _get_outlines, _mkannotation
import collections
from pdfminer import pdftypes
from pdfminer.pdfpage import PDFPage
import logging
import pdfminer
from pdfminer.psparser import PSLiteralTable, PSLiteral
from CustomAnnotation import CustomAnnotation

from pdfannots import _PDFProcessor
from pdfannots.utils import cleanup_text, decode_datetime

pdfminer.settings.STRICT = False
from pdfannots import _get_outlines

logger = logging.getLogger('pdfannots')


ANNOT_SUBTYPES: typ.Dict[PSLiteral, AnnotationType] = {
    PSLiteralTable.intern(e.name): e for e in AnnotationType}
"""Mapping from PSliteral to our own enumerant, for supported annotation types."""

IGNORED_ANNOT_SUBTYPES = \
    frozenset(PSLiteralTable.intern(n) for n in (
        'Link',   # Links are used for internal document links (e.g. to other pages).
        'Popup',  # Controls the on-screen appearance of other annotations. TODO: we may want to
                  # check for an optional 'Contents' field for alternative human-readable contents.
    ))
"""Annotation types that we ignore without issuing a warning."""

class ExtendedPDFProcessor(_PDFProcessor):
    """
    Extended PDF processor that customizes the receive_layout method and include full page text as doc attribute.
    """
    def receive_layout(self, ltpage: LTPage) -> None:
        """Callback from PDFLayoutAnalyzer superclass. Called once with each laid-out page."""
        assert self.page is not None

        # Re-initialise our per-page state
        self.clear()

        # Render all the items on the page
        self.render(ltpage)

        # If we still have annotations needing context, give them whatever we have
        for (charseq, annot) in self.context_subscribers:
            available = self.charseq - charseq
            annot.post_context = ''.join(self.recent_text[n] for n in range(-available, 0))
        
        texts = []
        for element in ltpage:
            if isinstance(element, (LTTextBox, LTTextLine)):
                texts.append(element.get_text())
        if self.page is not None:
            self.page.full_text = cleanup_text(" ".join(t.strip() for t in texts if t.strip()))
        
        self.page = None

def mkannotationcustom(
    pa: typ.Dict[str, typ.Any],
    page: Page
) -> typ.Optional[CustomAnnotation]:
    """
    Given a PDF annotation, capture relevant fields and construct an Annotation object.

    Refer to Section 8.4 of the PDF reference (version 1.7).
    """

    subtype = pa.get('Subtype')
    annot_type = None
    assert isinstance(subtype, PSLiteral)
    try:
        annot_type = ANNOT_SUBTYPES[subtype]
    except KeyError:
        pass

    if annot_type is None:
        if subtype not in IGNORED_ANNOT_SUBTYPES:
            logger.warning("Unsupported %s annotation ignored on %s", subtype.name, page)
        return None

    contents = pa.get('Contents')
    if contents is not None:
        # decode as string, normalise line endings, replace special characters
        contents = cleanup_text(pdfminer.utils.decode_text(contents))

    rgb: typ.Optional[RGB] = None
    color = pdftypes.resolve1(pa.get('C'))
    if color:
        if (isinstance(color, list)
                and len(color) == 3
                and all(isinstance(e, (int, float)) and 0 <= e <= 1 for e in color)):
            rgb = RGB(*color)
        else:
            logger.warning("Invalid color %s in annotation on %s", color, page)

    # Rect defines the location of the annotation on the page
    rect = pdftypes.resolve1(pa.get('Rect'))

    # QuadPoints are defined only for "markup" annotations (Highlight, Underline, StrikeOut,
    # Squiggly, Caret), where they specify the quadrilaterals (boxes) covered by the annotation.
    quadpoints = pdftypes.resolve1(pa.get('QuadPoints'))

    author = pdftypes.resolve1(pa.get('T'))
    if author is not None:
        author = pdfminer.utils.decode_text(author)

    name = pdftypes.resolve1(pa.get('NM'))
    if name is not None:
        name = pdfminer.utils.decode_text(name)

    created = None
    dobj = pa.get('CreationDate')
    # some pdf apps set modification date, but not creation date
    dobj = dobj or pa.get('ModDate')
    # poppler-based apps (e.g. Okular) use 'M' for some reason
    dobj = dobj or pa.get('M')
    createds = pdftypes.resolve1(dobj)
    if createds is not None:
        createds = pdfminer.utils.decode_text(createds)
        created = decode_datetime(createds)

    in_reply_to = pa.get('IRT')
    is_group = False
    if in_reply_to is not None:
        reply_type = pa.get('RT')
        if reply_type is PSLiteralTable.intern('Group'):
            is_group = True
        elif not (reply_type is None or reply_type is PSLiteralTable.intern('R')):
            logger.warning("Unexpected RT=%s, treated as R", reply_type)

    return CustomAnnotation(page, annot_type, quadpoints=quadpoints, rect=rect, name=name,
                      contents=contents, author=author, created=created, color=rgb,
                      in_reply_to_ref=in_reply_to, is_group_child=is_group)

def custom_process_file(file: typ.BinaryIO,
    *,  # Subsequent arguments are keyword-only
    columns_per_page: typ.Optional[int] = None,
    emit_progress_to: typ.Optional[typ.TextIO] = None,
    laparams: LAParams = LAParams()
) -> Document:
    """
    Process a PDF file, extracting its annotations and outlines. Extending original version to extract sentence which the annotation is part of.

    Arguments:
        file                Handle to PDF file
        columns_per_page    If set, overrides PDF Miner's layout detect with a fixed page layout
        emit_progress_to    If set, file handle (e.g. sys.stderr) to which progress is reported
        laparams            PDF Miner layout parameters
    """

# Initialise PDFMiner state
    rsrcmgr = PDFResourceManager()
    device =  ExtendedPDFProcessor(rsrcmgr, laparams)
    interpreter = PDFPageInterpreter(rsrcmgr, device)
    parser = PDFParser(file)
    doc = PDFDocument(parser)

    def emit_progress(msg: str) -> None:
        if emit_progress_to is not None:
            emit_progress_to.write(msg)
            emit_progress_to.flush()

    emit_progress(file.name)

    # Retrieve outlines if present. Each outline refers to a page, using
    # *either* a PDF object ID or an integer page number. These references will
    # be resolved below while rendering pages -- for now we insert them into one
    # of two dicts for later.
    outlines_by_pageno: typ.Dict[object, typ.List[Outline]] = collections.defaultdict(list)
    outlines_by_objid: typ.Dict[object, typ.List[Outline]] = collections.defaultdict(list)

    try:
        for o in _get_outlines(doc):
            if isinstance(o.pageref, pdftypes.PDFObjRef):
                outlines_by_objid[o.pageref.objid].append(o)
            else:
                outlines_by_pageno[o.pageref].append(o)
    except PDFNoOutlines:
        logger.info("Document doesn't include outlines (\"bookmarks\")")
    except Exception as ex:
        logger.warning("Failed to retrieve outlines: %s", ex)

    # Iterate over all the pages, constructing page objects.
    result = Document()
    for (pageno, pdfpage) in enumerate(PDFPage.create_pages(doc)):
        emit_progress(" %d" % (pageno + 1))

        page = Page(pageno, pdfpage.pageid, pdfpage.label, pdfpage.mediabox, columns_per_page)
        result.pages.append(page)

        # Resolve any outlines referring to this page, and link them to the page.
        # Note that outlines may refer to the page number or ID.
        for o in (outlines_by_objid.pop(page.objid, [])
                  + outlines_by_pageno.pop(pageno, [])):
            o.resolve(page)
            page.outlines.append(o)

        # Dict from object ID (in the ObjRef) to Annotation object
        # This is used while post-processing to resolve inter-annotation references
        annots_by_objid: typ.Dict[int, Annotation] = {}

        # Construct Annotation objects, and append them to the page.
        for pa in pdftypes.resolve1(pdfpage.annots) if pdfpage.annots else []:
            if isinstance(pa, pdftypes.PDFObjRef):
                annot_dict = pdftypes.dict_value(pa)
                if annot_dict:  # Would be empty if pa is a broken ref
                    annot = mkannotationcustom(annot_dict, page)
                    if annot is not None:
                        page.annots.append(annot)
                        assert pa.objid not in annots_by_objid
                        annots_by_objid[pa.objid] = annot
            else:
                logger.warning("Unknown annotation: %s", pa)

        # If the page has neither outlines nor annotations, skip further processing.
        if not (page.annots or page.outlines):
            continue

        # Render the page. This captures the selected text for any annotations
        # on the page, and updates annotations and outlines with a logical
        # sequence number based on the order of text lines on the page.
        device.set_page(page)
        interpreter.process_page(pdfpage)

        # Now we have their logical order, sort the annotations and outlines.
        page.annots.sort()
        page.outlines.sort()

        # Give the annotations a chance to update their internals
        for a in page.annots:
            a.postprocess(annots_by_objid)
            a.set_context_sentence(page.full_text)


    emit_progress("\n")

    device.close()

    # all outlines should be resolved by now
    assert {} == outlines_by_pageno
    assert {} == outlines_by_objid

    return result


def write_list_to_markdown(output_path: str, file_name: str, entries: list[tuple[str, str, str]], mode="a") -> None:
    """
    Writes a list of strings to a Markdown file.
    - Uses the file name (without extension) as the title.
    - Each string is written as a bulleted item.
    """
    title = file_name.replace(".pdf", "").split("\\")[-1]
    with open(os.path.join(output_path, "consolidated_notes.md"), mode, encoding="utf-8") as f:
        f.write(f"# {title}\n\n")  # Markdown title
        for entry in entries:
            if entry[1] and not entry[0]: # standalone comment present
                f.write(f"\n\n- > *standalone comment:* {entry[0]}\n\n")
            else: # context sentence is present
                if not entry[1]:
                    f.write(f"- {entry[0]}\n")
                else: # related comment is also present
                    f.write(f"- {entry[0]}\n\n\t> *comment:* {entry[1]}\n\t>\n\t> *highlight_text:* {entry[2]}\n\n")
        f.write("---\n\n")

def main():
    first = True  # for file mode
    for root, dirs, files in os.walk(args.input_path):
        for file_name in files:
            # Construct the absolute path of the file
            file_path = os.path.join(root, file_name)
            print("Currently Processing: ", file_path)
            if ".pdf" in file_path:
                entries = []
                document = custom_process_file(open(file_path, "rb"))

                for page_idx in range(len(document.pages)):
                    annots = document.pages[page_idx].annots
                    for annot in annots:
                        contents = annot.contents
                        context = annot.context_sentence
                        text = annot.gettext()

                        entries.append((context,contents,text)) # context_sentence, comment, highlighted text
                
                if not any(t[0] is not None for t in entries):
                    print("No context sentences found.")
                else:
                    output_path = args.input_path if args.output_path is None else args.output_path

                    if not os.path.exists(output_path):
                        os.makedirs(output_path)
                    
                    mode = "w" if first else "a"
                    write_list_to_markdown(output_path, file_path, entries, mode)
                    first = False

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_path", type =str, help = "path to dir")
    parser.add_argument("--output_path", type =str, help = "directory path where to store consolidated md file containing context sentences. Default is same as input path", default=None)

    args = parser.parse_args()

    print("args ..", args)
    main()