#!/usr/bin/env python3
"""
Astons Invoice Generator
Parses BrightManager fee note PDFs and generates branded Astons invoices.
"""

import re
import sys
import os
from collections import defaultdict
import pdfplumber
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm, cm
from reportlab.lib.colors import HexColor, black, white
from reportlab.pdfgen import canvas
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from reportlab.platypus import Paragraph
from reportlab.lib.styles import ParagraphStyle


# === BRAND COLOURS ===
ASTONS_DARK_GREEN = HexColor("#1a5c2e")
ASTONS_MID_GREEN = HexColor("#3a8c4e")
ASTONS_LIGHT_GREEN = HexColor("#6ab04c")
ASTONS_GREY = HexColor("#4a4a4a")
ASTONS_LIGHT_GREY = HexColor("#f5f5f5")
ASTONS_BORDER = HexColor("#e0e0e0")
ASTONS_TEXT = HexColor("#333333")
ASTONS_SUBTLE = HexColor("#777777")


# === PORTFOLIOS ===
# Hardcoded bank details per portfolio. The Streamlit UI asks the team member
# which portfolio an invoice belongs to at submission time, and we inject the
# correct bank details here instead of parsing them from the BrightManager PDF.
#
# NOTE: Account names, numbers and sort codes below are PLACEHOLDERS based on
# what we saw in test PDFs. Ash needs to confirm which account belongs to
# which portfolio before v2 goes live.
PORTFOLIOS = {
    "A": {
        "label": "A-Portfolio (Astons original practice)",
        "bank_name": "Astons Accountants and Business Advisors Limited",
        "bank_account": "19010489",
        "bank_sort": "60-83-71",
    },
    "C": {
        "label": "C-Portfolio (acquired practice)",
        "bank_name": "Astons Accountants and Business Advisors Limited",
        "bank_account": "00273335",
        "bank_sort": "04-13-76",
    },
}


def apply_portfolio(data, portfolio_key):
    """Overwrite bank details on `data` using the hardcoded portfolio entry."""
    if portfolio_key not in PORTFOLIOS:
        raise ValueError(
            f"Unknown portfolio '{portfolio_key}'. Must be one of: {list(PORTFOLIOS.keys())}"
        )
    p = PORTFOLIOS[portfolio_key]
    data['bank_name'] = p['bank_name']
    data['bank_account'] = p['bank_account']
    data['bank_sort'] = p['bank_sort']
    data['portfolio'] = portfolio_key
    return data


def parse_brightmanager_pdf(pdf_path):
    """Extract invoice data from a BrightManager fee note PDF."""
    data = {}
    
    with pdfplumber.open(pdf_path) as pdf:
        text = pdf.pages[0].extract_text()
        
        # Extract page 2 for VAT number
        if len(pdf.pages) > 1:
            text2 = pdf.pages[1].extract_text() or ""
        else:
            text2 = ""
    
    # Invoice number
    m = re.search(r'Invoice No\.\s+(\d+)', text)
    data['invoice_no'] = m.group(1) if m else ''
    
    # Date
    m = re.search(r'Date\s+(\d{1,2}\s+\w+\s+\d{4})', text)
    data['date'] = m.group(1) if m else ''
    
    # Due date
    m = re.search(r'Due By\s+(\d{1,2}\s+\w+\s+\d{4})', text)
    data['due_date'] = m.group(1) if m else ''
    
    # Client name and address - use word positions for reliable two-column separation
    with pdfplumber.open(pdf_path) as pdf2:
        words = pdf2.pages[0].extract_words(keep_blank_chars=True, x_tolerance=3, y_tolerance=3)
    
    # Find the y-position of "Invoiced To:" and "Description" to bracket the address block
    invoiced_y = None
    desc_y = None
    for w in words:
        if 'Invoiced' in w['text']:
            invoiced_y = w['top']
        if w['text'] == 'Description' and w['x0'] < 100:
            desc_y = w['top']
    
    # Collect left-column words (x0 < 200) between Invoiced To and Description
    from collections import defaultdict
    address_lines = defaultdict(list)
    if invoiced_y and desc_y:
        for w in words:
            if w['top'] > invoiced_y and w['top'] < desc_y and w['x0'] < 200:
                y_key = round(w['top'] / 3) * 3
                address_lines[y_key].append(w)
    
    client_lines = []
    for y_key in sorted(address_lines.keys()):
        line_words = sorted(address_lines[y_key], key=lambda w: w['x0'])
        line_text = ' '.join(w['text'].strip() for w in line_words).strip()
        if line_text and line_text not in ['Invoiced To:', 'GBR', '']:
            client_lines.append(line_text)
    
    data['client_name'] = client_lines[0] if client_lines else ''
    data['client_address'] = client_lines[1:] if len(client_lines) > 1 else []
    
    # Description line items - use word positions to reliably extract
    # Description text is at x < 230, Total amount at x > 515
    # Lines with amounts are item starts; lines without are continuations
    with pdfplumber.open(pdf_path) as pdf3:
        words = pdf3.pages[0].extract_words(keep_blank_chars=True, x_tolerance=3, y_tolerance=3)
    
    # Find y boundaries: Description header -> Subtotal
    desc_header_y = None
    subtotal_y = None
    for w in words:
        if w['text'] == 'Description' and w['x0'] < 100:
            desc_header_y = w['top']
        if 'Subtotal' in w['text']:
            subtotal_y = w['top']
    
    # Group words by line within the description area
    desc_word_lines = defaultdict(list)
    if desc_header_y and subtotal_y:
        for w in words:
            if w['top'] > desc_header_y + 10 and w['top'] < subtotal_y:
                y_key = round(w['top'] / 3) * 3
                desc_word_lines[y_key].append(w)
    
    # Parse each line: extract description text (left side) and total amount (right side)
    line_items = []  # list of {'description': str, 'amount': str}
    
    for y_key in sorted(desc_word_lines.keys()):
        line_words = sorted(desc_word_lines[y_key], key=lambda w: w['x0'])
        
        # Get description words (x < 230) and total amount (x > 515)
        desc_words = [w['text'].strip() for w in line_words if w['x0'] < 230]
        total_words = [w['text'].strip() for w in line_words if w['x0'] > 515]
        
        desc_text = ' '.join(desc_words).strip()
        total_amount = total_words[0] if total_words else None
        
        if not desc_text:
            continue
        
        if total_amount:
            # This is a new line item (has an amount)
            # Clean the £ prefix for storage
            amount_clean = total_amount.replace('£', '').replace(',', '')
            line_items.append({
                'description': desc_text,
                'amount': total_amount
            })
        else:
            # Continuation line - append to previous item's description
            if line_items:
                line_items[-1]['description'] += ' ' + desc_text
    
    data['line_items'] = line_items
    # Keep flat description list for backward compatibility
    data['description'] = [item['description'] for item in line_items]
    
    # Financial amounts - use the rightmost £ amount on the Subtotal line
    lines = text.split('\n')
    m = re.search(r'Subtotal.*£([\d,.]+)\s*$', text, re.MULTILINE)
    data['subtotal'] = m.group(1) if m else ''
    
    m = re.search(r'Total VAT\s+£([\d,.]+)', text)
    data['vat'] = m.group(1) if m else ''
    
    m = re.search(r'Total\s+£([\d,.]+)\s*$', text, re.MULTILINE)
    data['total'] = m.group(1) if m else ''
    
    # Bank details are NOT parsed from the BrightManager PDF in v2.
    # BrightManager places BOTH bank accounts on every fee note with no
    # reliable way to tell which is "selected". Instead, the caller passes
    # a portfolio override via process_invoice(...) and we look up the
    # correct bank details from PORTFOLIOS below.
    data['bank_name'] = ''
    data['bank_account'] = ''
    data['bank_sort'] = ''
    
    # VAT registration number
    m = re.search(r'VAT Registration Number:\s*([\d\s]+)', text + '\n' + text2)
    data['vat_reg'] = m.group(1).strip() if m else '361419995'
    
    # Format VAT reg as GB XXX XXXX XX
    vat_digits = re.sub(r'\s', '', data['vat_reg'])
    if len(vat_digits) == 9:
        data['vat_reg_formatted'] = f"GB {vat_digits[:3]} {vat_digits[3:7]} {vat_digits[7:]}"
    else:
        data['vat_reg_formatted'] = f"GB {data['vat_reg']}"
    
    return data


def draw_logo_placeholder(c, x, y, width, height):
    """Draw Astons logo - uses the extracted PNG if available, otherwise a styled text version."""
    logo_path = os.path.join(os.path.dirname(__file__), 'astons_logo.png')
    if os.path.exists(logo_path):
        c.drawImage(logo_path, x, y, width=width, height=height, 
                     preserveAspectRatio=True, mask='auto')
    else:
        # Styled text fallback
        c.saveState()
        # Draw the 'A' triangle shape
        c.setFillColor(ASTONS_DARK_GREEN)
        path = c.beginPath()
        path.moveTo(x + 5*mm, y)
        path.lineTo(x + 15*mm, y + height)
        path.lineTo(x + 25*mm, y)
        path.close()
        c.drawPath(path, fill=1)
        
        c.setFillColor(ASTONS_LIGHT_GREEN)
        path2 = c.beginPath()
        path2.moveTo(x + 10*mm, y)
        path2.lineTo(x + 15*mm, y + height * 0.6)
        path2.lineTo(x + 20*mm, y)
        path2.close()
        c.drawPath(path2, fill=1)
        
        # Company name
        c.setFont("Helvetica", 22)
        c.setFillColor(ASTONS_DARK_GREEN)
        c.drawString(x + 28*mm, y + height - 8*mm, "Astons")
        
        c.setFont("Helvetica", 7)
        c.setFillColor(ASTONS_GREY)
        c.drawString(x + 28*mm, y + height - 13*mm, "A C C O U N T A N T S")
        
        c.restoreState()


def generate_branded_invoice(data, output_path):
    """Generate a professionally branded Astons invoice PDF."""
    
    width, height = A4  # 210mm x 297mm
    c = canvas.Canvas(output_path, pagesize=A4)
    
    # Page margins
    left_margin = 25*mm
    right_margin = width - 25*mm
    content_width = right_margin - left_margin
    
    # Footer zone - nothing should be drawn below this
    footer_zone_top = 45*mm
    
    # === CURSOR - everything flows down from here ===
    y = height - 20*mm
    
    # === HEADER BAR ===
    c.setFillColor(ASTONS_DARK_GREEN)
    c.rect(0, height - 8*mm, width, 8*mm, fill=1, stroke=0)
    
    # === LOGO (left) + DESIGNATION ===
    draw_logo_placeholder(c, left_margin, y - 30*mm, 70*mm, 22*mm)
    
    # Chartered Certified Accountants tagline under logo
    c.setFont("Helvetica", 7)
    c.setFillColor(ASTONS_SUBTLE)
    c.drawString(left_margin, y - 34*mm, "Chartered Certified Accountants")
    
    # === INVOICE TITLE & META (right) ===
    meta_x = right_margin - 65*mm
    meta_y = y - 12*mm
    
    c.setFont("Helvetica-Bold", 20)
    c.setFillColor(ASTONS_DARK_GREEN)
    c.drawRightString(right_margin, meta_y, "INVOICE")
    meta_y -= 12*mm
    
    for label, value in [
        ("Invoice No.", data['invoice_no']),
        ("Date", data['date']),
        ("Due Date", data['due_date']),
    ]:
        c.setFont("Helvetica", 8)
        c.setFillColor(ASTONS_SUBTLE)
        c.drawString(meta_x, meta_y, label)
        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(ASTONS_TEXT)
        c.drawRightString(right_margin, meta_y, value)
        meta_y -= 6*mm
    
    # === CLIENT ADDRESS ===
    y -= 48*mm
    
    c.setFont("Helvetica", 7)
    c.setFillColor(ASTONS_SUBTLE)
    c.drawString(left_margin, y, "INVOICED TO")
    y -= 7*mm
    
    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(ASTONS_TEXT)
    c.drawString(left_margin, y, data['client_name'])
    y -= 5.5*mm
    
    c.setFont("Helvetica", 9)
    c.setFillColor(ASTONS_GREY)
    for line in data['client_address']:
        c.drawString(left_margin, y, line)
        y -= 4.5*mm
    
    # === GREEN DIVIDER ===
    y -= 3*mm
    c.setStrokeColor(ASTONS_DARK_GREEN)
    c.setLineWidth(1.5)
    c.line(left_margin, y, right_margin, y)
    
    # === DESCRIPTION SECTION ===
    y -= 8*mm
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(ASTONS_DARK_GREEN)
    c.drawString(left_margin, y, "DESCRIPTION OF SERVICES")
    
    y -= 8*mm
    c.setFont("Helvetica", 9)
    c.setFillColor(ASTONS_TEXT)
    c.drawString(left_margin, y, "For professional services rendered in connection with:-")
    y -= 8*mm
    
    # Description style
    desc_style = ParagraphStyle(
        'desc', fontName='Helvetica', fontSize=9, leading=13,
        textColor=ASTONS_TEXT,
    )
    
    # Service lines with amounts
    has_multiple_items = len(data.get('line_items', [])) > 1
    
    for i, item in enumerate(data.get('line_items', [])):
        c.setFont("Helvetica", 9)
        c.setFillColor(ASTONS_GREY)
        c.drawString(left_margin + 5*mm, y, f"{i+1}.")
        
        text_width = content_width - 45*mm if has_multiple_items else content_width - 15*mm
        p = Paragraph(item['description'], desc_style)
        pw, ph = p.wrap(text_width, 200)
        p.drawOn(c, left_margin + 15*mm, y - ph + 9)
        
        if has_multiple_items:
            c.setFont("Helvetica", 9)
            c.setFillColor(ASTONS_TEXT)
            c.drawRightString(right_margin, y, item['amount'])
        
        y -= max(ph + 3*mm, 7*mm)
    
    # Closing narrative
    y -= 2*mm
    c.setFont("Helvetica", 9)
    c.setFillColor(ASTONS_GREY)
    c.drawString(left_margin + 15*mm, y, "all including —")
    y -= 5.5*mm
    c.drawString(left_margin + 15*mm, y, "Correspondence and advice, generally, to date.")
    
    # === TOTALS SECTION ===
    y -= 18*mm
    
    totals_x_label = right_margin - 75*mm
    totals_x_value = right_margin - 5*mm
    
    # Calculate totals box dimensions first
    totals_box_top = y + 5*mm
    totals_box_bottom = y - 26*mm
    
    # Draw grey background
    c.setFillColor(ASTONS_LIGHT_GREY)
    c.roundRect(totals_x_label - 5*mm, totals_box_bottom,
                right_margin - totals_x_label + 10*mm,
                totals_box_top - totals_box_bottom,
                3, fill=1, stroke=0)
    
    # Net Fee
    c.setFont("Helvetica", 9)
    c.setFillColor(ASTONS_GREY)
    c.drawString(totals_x_label, y, "Total Net Fee")
    c.setFont("Helvetica", 10)
    c.setFillColor(ASTONS_TEXT)
    c.drawRightString(totals_x_value, y, f"£{data['subtotal']}")
    y -= 7*mm
    
    # VAT
    c.setFont("Helvetica", 9)
    c.setFillColor(ASTONS_GREY)
    c.drawString(totals_x_label, y, "VAT @ 20%")
    c.setFont("Helvetica", 10)
    c.setFillColor(ASTONS_TEXT)
    c.drawRightString(totals_x_value, y, f"£{data['vat']}")
    y -= 3*mm
    
    # Divider
    c.setStrokeColor(ASTONS_BORDER)
    c.setLineWidth(0.5)
    c.line(totals_x_label, y, totals_x_value, y)
    y -= 7*mm
    
    # Total Due
    c.setFont("Helvetica-Bold", 12)
    c.setFillColor(ASTONS_DARK_GREEN)
    c.drawString(totals_x_label, y, "Total Due")
    c.drawRightString(totals_x_value, y, f"£{data['total']}")
    
    # === PAYMENT SECTION ===
    y -= 18*mm
    
    # Green bar header
    bar_height = 8*mm
    c.setFillColor(ASTONS_DARK_GREEN)
    c.roundRect(left_margin, y - 2*mm, content_width, bar_height, 2, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(white)
    # Left-aligned, vertically centred in bar
    c.drawString(left_margin + 5*mm, y + 0.5*mm, "PAYMENT DETAILS")
    
    y -= 12*mm
    c.setFont("Helvetica", 8)
    c.setFillColor(ASTONS_SUBTLE)
    c.drawString(left_margin, y, "Payment Terms: Upon presentation")
    y -= 6*mm
    
    # Bank details - compact layout
    for label, value in [
        ("Account Name", data['bank_name']),
        ("Sort Code", data['bank_sort']),
        ("Account Number", data['bank_account']),
        ("Reference", f"Invoice {data['invoice_no']}"),
    ]:
        c.setFont("Helvetica", 8)
        c.setFillColor(ASTONS_SUBTLE)
        c.drawString(left_margin, y, label)
        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(ASTONS_TEXT)
        c.drawString(left_margin + 35*mm, y, value)
        y -= 5*mm
    
    # Stripe payment link - same spacing as bank detail lines
    c.setFont("Helvetica", 8)
    c.setFillColor(ASTONS_SUBTLE)
    c.drawString(left_margin, y, "Pay online:")
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(ASTONS_MID_GREEN)
    c.drawString(left_margin + 35*mm, y, "Click here to pay by card")
    link_width = c.stringWidth("Click here to pay by card", "Helvetica-Bold", 8)
    c.linkURL("https://buy.stripe.com/28EfZi01TgGg6fh26H8Zq04",
              (left_margin + 35*mm, y - 2, left_margin + 35*mm + link_width, y + 8),
              relative=0)
    
    # === FOOTER (anchored to bottom) ===
    footer_y = 18*mm
    c.setStrokeColor(ASTONS_BORDER)
    c.setLineWidth(0.5)
    c.line(left_margin, footer_y, right_margin, footer_y)
    
    footer_y -= 5*mm
    c.setFont("Helvetica", 7)
    c.setFillColor(ASTONS_SUBTLE)
    c.drawString(left_margin, footer_y,
                 "Astons Accountants & Business Advisors Limited  |  Chartered Certified Accountants")
    c.drawRightString(right_margin, footer_y,
                      f"VAT Reg: {data['vat_reg_formatted']}")
    
    footer_y -= 4*mm
    c.drawString(left_margin, footer_y,
                 "6b Parkway, Porters Wood, St Albans, AL3 6PA")
    c.drawRightString(right_margin, footer_y,
                      "Registered in England & Wales")
    
    # Bottom green bar
    c.setFillColor(ASTONS_DARK_GREEN)
    c.rect(0, 0, width, 5*mm, fill=1, stroke=0)
    
    c.save()
    return output_path


def process_invoice(input_pdf, portfolio, output_dir=None):
    """Process a single BrightManager PDF into a branded Astons invoice.

    Args:
        input_pdf: path to the BrightManager fee note PDF.
        portfolio: 'A' or 'C' — which portfolio's bank details to use.
        output_dir: where to write the branded PDF. Defaults to input dir.
    """
    data = parse_brightmanager_pdf(input_pdf)
    apply_portfolio(data, portfolio)

    if output_dir is None:
        output_dir = os.path.dirname(input_pdf) or '.'

    os.makedirs(output_dir, exist_ok=True)

    # Clean client name for filename
    clean_name = re.sub(r'[^\w\s-]', '', data['client_name']).strip().replace(' ', '_')
    filename = f"Astons_Invoice_{data['invoice_no']}_{clean_name}.pdf"
    output_path = os.path.join(output_dir, filename)

    generate_branded_invoice(data, output_path)
    print(f"Generated: {output_path}")
    return output_path


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("Usage: python generate_invoice.py <brightmanager_pdf> <A|C> [output_dir]")
        sys.exit(1)

    input_pdf = sys.argv[1]
    portfolio = sys.argv[2].upper()
    output_dir = sys.argv[3] if len(sys.argv) > 3 else None
    process_invoice(input_pdf, portfolio, output_dir)
