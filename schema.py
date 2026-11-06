"""Output contract of the Document Intake Agent (Agent 1) -> Claims Processing Agent (Agent 2).

Rules of the contract:
  * Agent 1 is purely EXTRACTIVE: it reports what the documents say, never what is payable or eligible.
  * Dates are ISO-8601 strings (YYYY-MM-DD). Amounts are decimal strings ("12500.50"), never floats.
  * Every extracted field carries a confidence (0-1), the page it came from, and the text as printed (evidence).
  * Flags have a severity: 'error' (needs review), 'warning' (review only for some codes), 'info' (context for Agent 2).
  * Bump SCHEMA_VERSION on any breaking change; Agent 2 should check it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.1"


class DocType(str, Enum):
    CLAIM_FORM = "claim_form"
    HOSPITAL_BILL = "hospital_bill"
    DISCHARGE_SUMMARY = "discharge_summary"
    PRESCRIPTION = "prescription"
    ID_CARD = "id_card"
    UNKNOWN = "unknown"


# Field names the extractor must look for, per document type.
REQUIRED_FIELDS: dict[DocType, list[str]] = {
    DocType.CLAIM_FORM: ["policy_number", "patient_name", "claim_amount", "date_of_claim"],
    DocType.HOSPITAL_BILL: ["hospital_name", "patient_name", "bill_number", "bill_date", "total_amount"],
    DocType.DISCHARGE_SUMMARY: ["patient_name", "hospital_name", "admission_date", "discharge_date", "diagnosis"],
    DocType.PRESCRIPTION: ["patient_name", "doctor_name", "prescription_date"],
    DocType.ID_CARD: ["name", "id_number"],
    DocType.UNKNOWN: [],
}
OPTIONAL_FIELDS: dict[DocType, list[str]] = {
    DocType.CLAIM_FORM: ["policy_holder_name", "hospital_name", "admission_date", "discharge_date", "diagnosis"],
    DocType.HOSPITAL_BILL: ["policy_number", "admission_date", "discharge_date", "line_items_total", "discount_amount", "tax_amount", "gstin"],
    DocType.DISCHARGE_SUMMARY: ["doctor_name", "procedures", "policy_number"],
    DocType.PRESCRIPTION: ["medicines", "diagnosis"],
    DocType.ID_CARD: ["date_of_birth", "id_type"],
    DocType.UNKNOWN: [],
}

DATE_FIELDS = {
    "date_of_claim", "bill_date", "admission_date", "discharge_date",
    "prescription_date", "date_of_birth",
}
AMOUNT_FIELDS = {"claim_amount", "total_amount", "line_items_total"}   # must be > 0
NONNEG_AMOUNT_FIELDS = {"discount_amount", "tax_amount"}               # may be 0

# Plain-language definitions sent to the model alongside each field name (avoids guessing what a name means).
FIELD_HINTS: dict[str, str] = {
    "line_items_total": "The printed 'Sub Total' / sum of the line-item amounts, BEFORE discount, tax or rounding. Not the final payable total.",
    "total_amount": "The final total of the bill ('Total Amount', 'Net Payable', 'Grand Total'). If an advance/deposit is deducted afterwards, give the total BEFORE that deduction.",
    "discount_amount": "Total discount/concession shown on the bill as a plain decimal. Use 0 if the bill shows none.",
    "tax_amount": "Total GST/tax added on the bill as a plain decimal. Use 0 if none is shown.",
    "claim_amount": "The amount being claimed on the claim form.",
    "bill_number": "The bill/invoice number only, without the date or any other text on the same line.",
    "hospital_name": "Hospital name as printed in the letterhead or header, exactly as written.",
    "patient_name": "Patient's full name, without titles such as Mr/Mrs/Ms/Dr.",
    "doctor_name": "Doctor's full name, without the 'Dr.' prefix and without degrees (MBBS, MS, MD...).",
    "name": "Cardholder's full name, without titles.",
    "policy_number": "The insurance policy number (not a member ID, claim number or bill number).",
    "diagnosis": "The diagnosis as written by the doctor/hospital.",
}


class ExtractedField(BaseModel):
    name: str
    value: Optional[str] = None
    evidence: Optional[str] = Field(default=None, description="Text exactly as it appears on the page, for human audit")
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    page: Optional[int] = Field(default=None, description="1-based page in the source file")


class LineItem(BaseModel):
    """One row of a bill's charges table, transcribed as printed (never recalculated by the model)."""

    description: str
    quantity: Optional[str] = None
    rate: Optional[str] = None
    amount: Optional[str] = None
    page: Optional[int] = None


class ExtractionResult(BaseModel):
    fields: list[ExtractedField] = Field(default_factory=list)
    line_items: list[LineItem] = Field(default_factory=list)


class Classification(BaseModel):
    doc_type: DocType
    multiple_documents: bool = False
    other_doc_types: list[DocType] = Field(default_factory=list)


class ValidationFlag(BaseModel):
    field: Optional[str] = None
    code: str
    severity: Literal["error", "warning", "info"]
    message: str
    source_file_ids: list[str] = Field(default_factory=list, description="Files involved (for cross-document flags)")


class ExtractedDocument(BaseModel):
    doc_type: DocType
    source_file_id: str
    page_count: int
    extracted_by: str = Field(default="", description="Model/extractor id, for audit")
    fields: list[ExtractedField] = Field(default_factory=list)
    line_items: list[LineItem] = Field(default_factory=list)
    computed_line_items_total: Optional[str] = Field(default=None, description="Sum of line-item amounts, computed in code")
    missing_required: list[str] = Field(default_factory=list)
    validation_flags: list[ValidationFlag] = Field(default_factory=list)


class SourceFile(BaseModel):
    file_id: str = Field(description="Reference into the shared Server/store; pass references, not bytes, over A2A")
    original_name: str
    sha256: str
    mime_type: str


class ClaimIntake(BaseModel):
    """The single message Agent 1 hands to Agent 2."""

    schema_version: str = SCHEMA_VERSION
    claim_id: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    user_text: str = ""
    source_files: list[SourceFile] = Field(default_factory=list)
    documents: list[ExtractedDocument] = Field(default_factory=list)
    cross_document_flags: list[ValidationFlag] = Field(default_factory=list)
    needs_human_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)
