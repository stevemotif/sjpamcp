"""
receipt_generator.py
Generates a PDF receipt that matches the SJ Piano Academy receipt style.
"""

import os
import io
from datetime import datetime
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT, TA_LEFT, TA_CENTER


def generate_receipt(
    receipt_number: str,
    paid_on: datetime,
    student_name: str,
    student_email: str,
    amount: float,
    output_path: str = None,
) -> bytes:
    """
    Generate a PDF receipt matching the SJ Piano Academy style.

    Args:
        receipt_number:  e.g. "1764355491540"
        paid_on:         datetime the payment was received
        student_name:    e.g. "Yanish"
        student_email:   e.g. "stevemotif@gmail.com"
        amount:          e.g. 200.0
        output_path:     if provided, also writes the PDF to disk

    Returns:
        PDF bytes
    """

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )

    styles = getSampleStyleSheet()

    # ── Custom styles ──────────────────────────────────────────────
    header_right = ParagraphStyle(
        "header_right",
        parent=styles["Normal"],
        alignment=TA_RIGHT,
        fontSize=10,
        leading=16,
    )
    normal_left = ParagraphStyle(
        "normal_left",
        parent=styles["Normal"],
        alignment=TA_LEFT,
        fontSize=10,
        leading=16,
    )
    bold_left = ParagraphStyle(
        "bold_left",
        parent=styles["Normal"],
        alignment=TA_LEFT,
        fontSize=10,
        leading=16,
        fontName="Helvetica-Bold",
    )
    bold_right = ParagraphStyle(
        "bold_right",
        parent=styles["Normal"],
        alignment=TA_RIGHT,
        fontSize=10,
        leading=16,
        fontName="Helvetica-Bold",
    )

    academy_name = os.getenv("ACADEMY_NAME", "SJ Piano Academy.")
    academy_addr = os.getenv("ACADEMY_ADDRESS", "2869 Battleford Rd")
    academy_city = os.getenv("ACADEMY_CITY", "Mississauga,ON L5N 2S6")
    paid_on_str = paid_on.strftime("%b %d, %Y")

    story = []

    # ── Top: Logo placeholder (SJ PA box) + Receipt info ──────────
    logo_text = Paragraph(
        "<b>SJ<br/>PA</b>",
        ParagraphStyle(
            "logo",
            parent=styles["Normal"],
            fontSize=16,
            leading=20,
            fontName="Helvetica-Bold",
            alignment=TA_CENTER,
            borderPadding=6,
        ),
    )

    receipt_info = Paragraph(
        f"Receipt #: {receipt_number}<br/>Paid on : {paid_on_str}",
        header_right,
    )

    top_table = Table(
        [[logo_text, receipt_info]],
        colWidths=[1.2 * inch, None],
    )
    top_table.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ("BOX", (0, 0), (0, 0), 1, colors.black),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ])
    )
    story.append(top_table)
    story.append(Spacer(1, 0.4 * inch))

    # ── Middle: Academy address (left) + Student info (right) ─────
    addr_block = Paragraph(
        f"{academy_name}<br/>{academy_addr}<br/>{academy_city}",
        normal_left,
    )
    student_block = Paragraph(
        f"{student_name}<br/>{student_email}",
        ParagraphStyle(
            "student",
            parent=styles["Normal"],
            alignment=TA_RIGHT,
            fontSize=10,
            leading=16,
        ),
    )

    info_table = Table(
        [[addr_block, student_block]],
        colWidths=[3.5 * inch, None],
    )
    info_table.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ])
    )
    story.append(info_table)
    story.append(Spacer(1, 0.4 * inch))

    # ── Payment Method table ───────────────────────────────────────
    payment_data = [
        [
            Paragraph("Payment Method", bold_left),
            Paragraph("Check #", bold_right),
        ],
        [
            Paragraph("E Transfer", normal_left),
            Paragraph("NA", ParagraphStyle("na", parent=styles["Normal"], alignment=TA_RIGHT, fontSize=10)),
        ],
    ]
    payment_table = Table(payment_data, colWidths=[3.5 * inch, None])
    payment_table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.93, 0.93, 0.93)),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.grey),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ])
    )
    story.append(payment_table)
    story.append(Spacer(1, 0.2 * inch))

    # ── Items table ────────────────────────────────────────────────
    items_data = [
        [
            Paragraph("Item", bold_left),
            Paragraph("Price", bold_right),
        ],
        [
            Paragraph("Piano Class", normal_left),
            Paragraph(f"${amount:,.0f}", ParagraphStyle("price", parent=styles["Normal"], alignment=TA_RIGHT, fontSize=10)),
        ],
        [
            Paragraph("", normal_left),
            Paragraph(f"<b>Total: ${amount:,.0f}</b>", bold_right),
        ],
    ]
    items_table = Table(items_data, colWidths=[3.5 * inch, None])
    items_table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.93, 0.93, 0.93)),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.grey),
            ("LINEABOVE", (0, 2), (-1, 2), 0.5, colors.grey),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ])
    )
    story.append(items_table)

    doc.build(story)
    pdf_bytes = buffer.getvalue()
    buffer.close()

    if output_path:
        os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(pdf_bytes)

    return pdf_bytes


if __name__ == "__main__":
    # Quick test
    from dotenv import load_dotenv
    load_dotenv()
    pdf = generate_receipt(
        receipt_number="1764355491540",
        paid_on=datetime.now(),
        student_name="Yanish",
        student_email="stevemotif@gmail.com",
        amount=200.0,
        output_path="test_receipt.pdf",
    )
    print(f"Receipt generated: {len(pdf)} bytes → test_receipt.pdf")
