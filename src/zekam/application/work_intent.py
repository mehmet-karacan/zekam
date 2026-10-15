"""Bounded native Work tool selection; operation intent never authorizes an effect."""

from __future__ import annotations

import re

from zekam.application.natural_work_read import explicit_work_references, parse_work_read
from zekam.domain.intake import normalize_text

WORK_OPERATION_COMMANDS = {
    "create": ("work create",),
    "list": ("work list",),
    "show": ("work show",),
    "history": ("work history",),
    "dependencies": ("work dependencies",),
    "relate": ("work relate",),
    "transition": ("work show", "work transition"),
    "checkpoints": ("ask", "work checkpoints"),
    "checkpoint": ("ask", "work checkpoint"),
    "close": ("work show", "work close"),
}


def infer_work_operation(question: str) -> str | None:
    """Recognize explicit record operations, preserving ordinary project/code/Jira questions."""
    text = normalize_text(question)
    references = explicit_work_references(question)
    operations: set[str] = set()
    if re.search(
        r"(?<!\w)(?:work|gorev|is kaydi)\s+(?:olustur|ac)(?!\w)"
        r"(?!\s+(?:metod|metot|fonksiyon|sinif|endpoint|api|buton|ekran))",
        text,
    ):
        operations.add("create")
    if re.search(r"(?<!\w)(?:work|gorevleri|isleri|is kayitlarini)\s+listele(?!\w)", text):
        operations.add("list")
    if references:
        if re.search(r"(?<!\w)(?:kapat|tamamla)(?!\w)", text):
            operations.add("close")
        if re.search(
            r"(?<!\w)(?:blokla|engelle|hazira al|aktif yap|durumunu degistir)(?!\w)", text
        ):
            operations.add("transition")
        if re.search(r"(?<!\w)checkpoint\s+(?:ekle|kaydet|olustur)(?!\w)", text):
            operations.add("checkpoint")
        if re.search(
            r"(?<!\w)(?:bagimlilik ekle|bagimliligi kaldir|bagimli yap|bagla)(?!\w)", text
        ):
            operations.add("relate")
        request = parse_work_read(question)
        if request is not None:
            if request.clarification:
                # Mixed read/mutation and multiple records require clarification.
                # A dependency mutation explicitly names two records by design.
                if not operations or re.search(r"(?<!\w)(?:goster|listele|incele)(?!\w)", text):
                    return "ambiguous"
            else:
                operations.add(request.operation)
        if operations == {"relate"} and len(references) != 2:
            return "ambiguous"
        if len(references) > 1 and operations != {"relate"}:
            return "ambiguous"
    if len(operations) > 1:
        return "ambiguous"
    return next(iter(operations), None)
