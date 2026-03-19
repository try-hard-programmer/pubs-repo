"""
Credits & Billing API Endpoints
Provides routes for fetching subscription status, transaction history, and usage stats.
"""
import csv
import io
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
import logging
from typing import List, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.enums import TA_RIGHT
from reportlab.platypus import SimpleDocTemplate, Spacer, Table, TableStyle, Paragraph, HRFlowable

# Auth & User Models
from app.auth.dependencies import get_current_user
from app.models.user import User

# Models & Service
from app.models.credit import CreditUsage
from app.models.subscription import Subscription
from app.services.credit_service import get_credit_service
from app.services.subscription_service import get_subscription_service
from app.services.organization_service import OrganizationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing-and-credits"])

# 1. Define the Request Body schema locally
class BillingRequest(BaseModel):
    organization_id: Optional[str] = Field(None, description="Organization ID passed from frontend body")

def get_org_id(user: User, body_org_id: Optional[str] = None) -> str:
    """Helper to extract organization_id from token metadata or fallback to Request Body."""
    # 1. Try to get it from the JWT token
    org_id = user.user_metadata.get("organization_id") or user.app_metadata.get("organization_id")
    
    # 2. Fallback to explicit Body if token doesn't have it
    if not org_id and body_org_id:
        org_id = body_org_id
        
    # 3. Hard fail if neither exists
    if not org_id:
        logger.error(f"User {user.email} attempted billing access without an organization_id.")
        raise HTTPException(
            status_code=400, 
            detail="Organization ID missing. Frontend must pass 'organization_id' in the JSON body."
        )
    return org_id


# 2. Changed from GET to POST to accept the JSON body
@router.post("/subscription", response_model=Subscription)
async def get_active_subscription(
    request: BillingRequest,
    current_user: User = Depends(get_current_user)
):
    try:
        org_id = get_org_id(current_user, request.organization_id)
        
        # USE THE SUBSCRIPTION SERVICE HERE
        sub_service = get_subscription_service()
        subscription_data = await sub_service.get_subscription(org_id)
        
        if not subscription_data:
            raise HTTPException(status_code=500, detail="Failed to retrieve or provision subscription.")
            
        return subscription_data

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching subscription for user {current_user.user_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 3. Changed from GET to POST to accept the JSON body
@router.post("/transactions", response_model=List[CreditUsage])
async def list_transactions(
    request: BillingRequest,
    limit: int = Query(50, ge=1, le=100, description="Items per page"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    current_user: User = Depends(get_current_user)
):
    try:
        org_id = get_org_id(current_user, request.organization_id)
        service = get_credit_service()
        
        # Change 'get_transactions' to 'get_usage_history'
        transactions = await service.get_usage_history(
            organization_id=org_id,
            limit=limit,
            offset=offset
        )
        return transactions

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching transactions for user {current_user.user_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch transaction history.")


# 4. Changed from GET to POST to accept the JSON body
@router.post("/stats")
async def get_billing_stats(
    request: BillingRequest,
    current_user: User = Depends(get_current_user)
):
    try:
        org_id = get_org_id(current_user, request.organization_id)
        service = get_credit_service()
        
        stats = await service.get_usage_stats(org_id)
        return stats

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching billing stats for user {current_user.user_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch billing statistics.")


# ──────────────────────────────────────────────────────────────
# EXPORT HELPERS
# ──────────────────────────────────────────────────────────────

_BRAND_COLOR = colors.HexColor("#1a1a2e")
_ACCENT_COLOR = colors.HexColor("#4f46e5")
_ROW_ALT = colors.HexColor("#f8f8ff")


def _build_invoice_pdf(org_name: str, tx: dict) -> bytes:
    """Build a simple A4 invoice PDF for a single credit_usage row."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=2 * cm, leftMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
    )
    styles = getSampleStyleSheet()

    invoice_num = f"INV-{str(tx['id'])[:8].upper()}"
    date_str = str(tx.get("created_at", ""))[:10]
    cost_idr = float(tx.get("cost", 0) or 0)

    elements = [
        Paragraph(org_name, styles["Title"]),
        Paragraph("INVOICE", styles["Heading2"]),
        Spacer(1, 0.4 * cm),
    ]

    meta_tbl = Table(
        [
            ["Invoice #", invoice_num],
            ["Date", date_str],
            ["Status", str(tx.get("status", "")).title()],
        ],
        colWidths=[4 * cm, 12 * cm],
    )
    meta_tbl.setStyle(TableStyle([
        ("FONTNAME",      (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
    ]))
    elements.append(meta_tbl)
    elements.append(Spacer(1, 0.5 * cm))

    detail_tbl = Table(
        [
            ["Field",        "Detail"],
            ["Type",         str(tx.get("query_type", "")).replace("_", " ").title()],
            ["Description",  str(tx.get("query_text", ""))],
            ["Credits Used", str(tx.get("credits_used", 0))],
            ["Input Tokens", str(tx.get("input_tokens", 0))],
            ["Output Tokens",str(tx.get("output_tokens", 0))],
            ["Cost (IDR)",   f"Rp {cost_idr:,.2f}"],
        ],
        colWidths=[5 * cm, 11 * cm],
    )
    detail_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0),  _BRAND_COLOR),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
        ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTNAME",      (0, 1), (0, -1),  "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 10),
        ("GRID",          (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, _ROW_ALT]),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
    ]))
    elements.append(detail_tbl)

    doc.build(elements)
    return buffer.getvalue()


def _build_statement_pdf(org_name: str, rows: list, year=None, month=None) -> bytes:
    """Build a single A4 billing-statement PDF containing all transactions."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=2 * cm, leftMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
    )
    styles = getSampleStyleSheet()

    right_style = ParagraphStyle("right", parent=styles["Normal"], alignment=TA_RIGHT)
    small_style = ParagraphStyle("small", parent=styles["Normal"], fontSize=8, textColor=colors.grey)
    title_style = ParagraphStyle(
        "stitle", parent=styles["Title"],
        textColor=_BRAND_COLOR, fontSize=20, spaceAfter=0,
    )


    elements = []

    # ── Header: brand name left, title right ────────────────────
    logo_style = ParagraphStyle(
        "logo", parent=styles["Normal"],
        fontSize=16, textColor=_ACCENT_COLOR,
        fontName="Helvetica-Bold",
    )
    logo_cell = Paragraph("PALAPA AI", logo_style)

    period_label = "All Time"
    if year and month:
        period_label = f"{year} / {month:02d}"
    elif year:
        period_label = str(year)

    header_tbl = Table(
        [[logo_cell, Paragraph("BILLING STATEMENT", title_style)]],
        colWidths=[8 * cm, 8 * cm],
    )
    header_tbl.setStyle(TableStyle([
        ("VALIGN",  (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN",   (1, 0), (1, 0),   "RIGHT"),
    ]))
    elements.append(header_tbl)
    elements.append(HRFlowable(width="100%", thickness=1.5, color=_ACCENT_COLOR, spaceAfter=6))

    # ── Meta info ────────────────────────────────────────────────
    from datetime import timezone
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    meta_tbl = Table(
        [
            [Paragraph(f"<b>Organization:</b> {org_name}", styles["Normal"]),
             Paragraph(f"Generated: {generated}", right_style)],
            [Paragraph(f"<b>Period:</b> {period_label}", styles["Normal"]),
             Paragraph(f"Total Transactions: {len(rows)}", right_style)],
        ],
        colWidths=[9 * cm, 7 * cm],
    )
    meta_tbl.setStyle(TableStyle([
        ("FONTSIZE",      (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    elements.append(meta_tbl)
    elements.append(Spacer(1, 0.5 * cm))

    # ── Transaction table ────────────────────────────────────────
    wrap = ParagraphStyle("wrap", parent=styles["Normal"], fontSize=8, leading=10)
    header_row = [
        Paragraph("<b>Date</b>", wrap),
        Paragraph("<b>Type</b>", wrap),
        Paragraph("<b>Description</b>", wrap),
        Paragraph("<b>Credits</b>", wrap),
        Paragraph("<b>Cost (IDR)</b>", wrap),
        Paragraph("<b>Status</b>", wrap),
    ]
    table_data = [header_row]
    total_credits = 0
    total_cost = 0.0

    for tx in rows:
        cost = float(tx.get("cost", 0) or 0)
        credits = int(tx.get("credits_used", 0) or 0)
        total_credits += credits
        total_cost += cost
        desc = str(tx.get("query_text", ""))
        if len(desc) > 55:
            desc = desc[:52] + "..."
        table_data.append([
            Paragraph(str(tx.get("created_at", ""))[:10], wrap),
            Paragraph(str(tx.get("query_type", "")).replace("_", " ").title(), wrap),
            Paragraph(desc, wrap),
            Paragraph(str(credits), wrap),
            Paragraph(f"Rp {cost:,.0f}", wrap),
            Paragraph(str(tx.get("status", "")).title(), wrap),
        ])

    # Summary footer row
    table_data.append([
        Paragraph("<b>TOTAL</b>", wrap), "", "",
        Paragraph(f"<b>{total_credits:,}</b>", wrap),
        Paragraph(f"<b>Rp {total_cost:,.0f}</b>", wrap),
        "",
    ])

    tx_tbl = Table(
        table_data,
        colWidths=[2.2*cm, 2.5*cm, 6.0*cm, 1.8*cm, 2.8*cm, 1.7*cm],
        repeatRows=1,
    )
    tx_tbl.setStyle(TableStyle([
        # Header
        ("BACKGROUND",    (0, 0), (-1, 0),  _BRAND_COLOR),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
        # Alternating rows
        ("ROWBACKGROUNDS",(0, 1), (-1, -2), [colors.white, _ROW_ALT]),
        # Footer total
        ("BACKGROUND",    (0, -1), (-1, -1), colors.HexColor("#e8e8f0")),
        ("SPAN",          (1, -1), (2, -1)),
        ("SPAN",          (5, -1), (5, -1)),
        # Grid
        ("GRID",          (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ("LINEABOVE",     (0, -1), (-1, -1), 1.0, _ACCENT_COLOR),
        # Padding
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 5),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elements.append(tx_tbl)

    # ── Footer note ──────────────────────────────────────────────
    elements.append(Spacer(1, 0.5 * cm))
    elements.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    elements.append(Spacer(1, 0.2 * cm))
    elements.append(Paragraph(
        "This document is a system-generated billing statement from Palapa AI. "
        "Costs are shown in Indonesian Rupiah (IDR).",
        small_style,
    ))

    doc.build(elements)
    return buffer.getvalue()


async def _get_org_name(org_id: str) -> str:
    """Resolve org name; falls back to org_id if lookup fails."""
    try:
        org = await OrganizationService().get_organization_by_id(org_id)
        return org.name if org else org_id
    except Exception:
        return org_id


# ──────────────────────────────────────────────────────────────
# EXPORT ENDPOINTS
# ──────────────────────────────────────────────────────────────

class UsageExportRequest(BaseModel):
    organization_id: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None


class InvoicePDFRequest(BaseModel):
    organization_id: Optional[str] = None
    transaction_id: str


class InvoicesExportAllRequest(BaseModel):
    organization_id: Optional[str] = None
    year: Optional[int] = None
    month: Optional[int] = None


@router.post("/usage/export")
async def export_usage_csv(
    request: UsageExportRequest,
    current_user: User = Depends(get_current_user),
):
    """Export usage history as a CSV file."""
    org_id = get_org_id(current_user, request.organization_id)
    service = get_credit_service()

    rows = await service.get_usage_history_filtered(
        organization_id=org_id,
        start_date=request.start_date,
        end_date=request.end_date,
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Query Type", "Description", "Credits Used", "Status"])
    for row in rows:
        writer.writerow([
            str(row.get("created_at", ""))[:19],
            str(row.get("query_type", "")).replace("_", " ").title(),
            row.get("query_text", ""),
            row.get("credits_used", 0),
            str(row.get("status", "")).title(),
        ])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="usage_report.csv"'},
    )


@router.post("/invoice/pdf")
async def download_invoice_pdf(
    request: InvoicePDFRequest,
    current_user: User = Depends(get_current_user),
):
    """Download a single invoice as a PDF."""
    org_id = get_org_id(current_user, request.organization_id)
    service = get_credit_service()

    tx = await service.get_transaction_by_id(org_id, request.transaction_id)
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found.")

    org_name = await _get_org_name(org_id)
    pdf_bytes = _build_invoice_pdf(org_name, tx)
    invoice_num = f"INV-{str(tx['id'])[:8].upper()}"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="invoice_{invoice_num}.pdf"'},
    )


@router.post("/invoices/export-all")
async def export_all_invoices_pdf(
    request: InvoicesExportAllRequest,
    current_user: User = Depends(get_current_user),
):
    """Export all invoices as a single billing-statement PDF."""
    org_id = get_org_id(current_user, request.organization_id)
    service = get_credit_service()

    rows = await service.get_usage_history_filtered(
        organization_id=org_id,
        year=request.year,
        month=request.month,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="No transactions found for the given filters.")

    org_name = await _get_org_name(org_id)
    pdf_bytes = _build_statement_pdf(org_name, rows, year=request.year, month=request.month)

    if request.year and request.month:
        fname = f"billing_statement_{request.year}_{request.month:02d}.pdf"
    elif request.year:
        fname = f"billing_statement_{request.year}.pdf"
    else:
        fname = "billing_statement_all.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )