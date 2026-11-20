import io
import re
from pathlib import Path
from xml.sax.saxutils import escape
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

FONT_DIR = Path(__file__).with_name("fonts")

def report_pdf(title, data, note=""):
    if "DeskLatin" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("DeskLatin", str(FONT_DIR / "DejaVuSans.ttf"), shapable=True))
        pdfmetrics.registerFont(TTFont("DeskBangla", str(FONT_DIR / "NotoSansBengali-Regular.ttf"), shapable=True))
    body = ParagraphStyle("Body", fontName="DeskLatin", fontSize=10, leading=15, shaping=True)
    heading = ParagraphStyle("Title", parent=body, fontSize=21, leading=27, textColor=colors.HexColor("#4939be"))
    key = ParagraphStyle("Key", parent=body, textColor=colors.HexColor("#4c526f"), fontSize=9)
    def markup(value):
        safe = escape(str(value)).replace("\n", "<br/>")
        return re.sub(r"[\u0980-\u09ff\u200c\u200d]+", lambda m: f'<font name="DeskBangla">{m.group()}</font>', safe)
    story = [Paragraph(markup(title), heading), Spacer(1, 19)]
    for label, value in data.items():
        if isinstance(value, list):
            value = "\n".join(" · ".join(map(str, item)) if isinstance(item, (tuple, list)) else str(item) for item in value) or "—"
        story.extend([Paragraph(markup(label), key), Paragraph(markup(value), body), Spacer(1, 10)])
    if note:
        story.extend([Paragraph("Handover", key), Paragraph(markup(note), body)])
    output = io.BytesIO()
    SimpleDocTemplate(output, pagesize=A4, title=title, author="KOL Desk").build(story)
    return output.getvalue()
