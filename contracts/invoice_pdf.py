
"""
contracts/invoice_pdf.py

Renders an Invoice to PDF — same HTML-straight-to-PDF approach as
agreements/pdf.py and property_intel/pdf.py (WeasyPrint over a Jinja2
template), reusing agreements/pdf.py's own Jinja2 Environment/TEMPLATE_DIR
convention but pointed at this app's own templates directory.

Company name/address and the payment-methods list come from
backend/settings.py's INVOICE_* values — edit those, not this file, when
real business details replace the current placeholders.
"""
import logging
import os

from django.conf import settings
from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML

logger = logging.getLogger("contracts")

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
_jinja_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "j2"]),
)

COMPANY_NAME = getattr(settings, "INVOICE_COMPANY_NAME", "Scape Data Solutions")
COMPANY_ADDRESS = getattr(settings, "INVOICE_COMPANY_ADDRESS", "")
COMPANY_EMAIL = getattr(settings, "INVOICE_COMPANY_EMAIL", "")
COMPANY_PHONE = getattr(settings, "INVOICE_COMPANY_PHONE", "")
PAYMENT_METHODS = getattr(settings, "INVOICE_PAYMENT_METHODS", [])


class InvoiceRenderError(Exception):
    """Raised on unrecoverable PDF rendering failure — caller (services.py)
    treats this as a failed render; nothing is uploaded or linked from
    Invoice.pdf_url unless this function returns successfully."""


def _fmt_dt(dt):
    return dt.strftime("%d %B %Y") if dt else ""


def render_invoice_pdf(invoice):
    """Takes an already-created, already-numbered Invoice and lays it
    out as PDF bytes. Milestones are read fresh from invoice.milestones —
    fine to call more than once (e.g. to re-download), since nothing
    about an Invoice's line items changes after creation (total_amount
    is frozen; milestones attached to it don't get removed except by
    void_invoice, which is a whole-invoice action, not a line-item edit)."""
    try:
        milestone_rows = [
            {
                "title": m.title,
                "description": m.description,
                "amount": m.amount,
                "order": m.order,
            }
            for m in invoice.milestones.order_by("order").all()
        ]

        context = {
            "invoice": invoice,
            "contract": invoice.contract,
            "milestones": milestone_rows,
            "issued_at": _fmt_dt(invoice.issued_at),
            "due_at": _fmt_dt(invoice.due_at),
            "company_name": COMPANY_NAME,
            "company_address": COMPANY_ADDRESS,
            "company_email": COMPANY_EMAIL,
            "company_phone": COMPANY_PHONE,
            "payment_methods": PAYMENT_METHODS,
        }

        template = _jinja_env.get_template("invoice_document.html.j2")
        html_string = template.render(**context)

        return HTML(string=html_string, base_url=TEMPLATE_DIR).write_pdf()
    except Exception as exc:
        logger.error("Invoice PDF render failed for invoice %s: %s", invoice.id, exc)
        raise InvoiceRenderError(str(exc)) from exc
