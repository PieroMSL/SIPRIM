"""Generador de informe preliminar de priorizacion en PDF (HU4.3).

Genera el PDF completamente en memoria (BytesIO) sin archivos temporales en disco
ni ejecutables externos, compatible con despliegue serverless en Vercel.
"""
from __future__ import annotations

from datetime import datetime
from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

PROTOTYPE_DISCLAIMER = (
    "Reporte preliminar del prototipo académico SIPRIM. "
    "No constituye resolución municipal oficial ni reemplaza la revisión o firma de la autoridad competente."
)


def _p(text: Any, style: ParagraphStyle) -> Paragraph:
    safe = escape(str(text if text is not None else ""))
    return Paragraph(safe.replace("\n", "<br/>"), style)


def _p_bold(text: Any, style: ParagraphStyle) -> Paragraph:
    safe = escape(str(text if text is not None else "")).replace("\n", "<br/>")
    return Paragraph(f"<b>{safe}</b>", style)


def _p_segments(segments: list[tuple[Any, bool]], style: ParagraphStyle) -> Paragraph:
    chunks: list[str] = []
    for text, bold in segments:
        safe = escape(str(text if text is not None else "")).replace("\n", "<br/>")
        chunks.append(f"<b>{safe}</b>" if bold else safe)
    return Paragraph("".join(chunks), style)


def _fmt_dt(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(value or "No registrada")


def _draw_footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(colors.HexColor("#475569"))
    width, _ = A4
    canvas.line(doc.leftMargin, 16 * mm, width - doc.rightMargin, 16 * mm)
    canvas.drawString(doc.leftMargin, 11.5 * mm, PROTOTYPE_DISCLAIMER[:106])
    canvas.drawString(doc.leftMargin, 8.2 * mm, PROTOTYPE_DISCLAIMER[106:].strip())
    canvas.drawRightString(width - doc.rightMargin, 8.2 * mm, f"Página {doc.page}")
    canvas.restoreState()


def build_prioritization_pdf(
    ranking: dict[str, Any],
    evidences_by_version: dict[str, list[dict[str, str]]],
    human_review_by_version: dict[str, dict[str, Any]],
) -> bytes:
    """Construye el PDF de priorizacion usando exactamente los datos del ranking vigente."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=16 * mm,
        bottomMargin=22 * mm,
        title="SIPRIM - Informe Preliminar de Priorización",
        author="SIPRIM",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "SiprimTitle",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=15,
        leading=19,
        textColor=colors.HexColor("#0F3D3E"),
        spaceAfter=4,
    )
    h2_style = ParagraphStyle(
        "SiprimH2",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=11.5,
        leading=15,
        textColor=colors.HexColor("#0F3D3E"),
        spaceBefore=10,
        spaceAfter=4,
    )
    body_style = ParagraphStyle(
        "SiprimBody",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.7,
        leading=11.5,
        textColor=colors.HexColor("#1E293B"),
    )
    small_style = ParagraphStyle(
        "SiprimSmall",
        parent=body_style,
        fontSize=8.0,
        leading=10.5,
        textColor=colors.HexColor("#334155"),
    )
    warn_style = ParagraphStyle(
        "SiprimWarn",
        parent=body_style,
        fontName="Helvetica-Bold",
        fontSize=8.8,
        leading=12,
        textColor=colors.HexColor("#78350F"),
    )

    story: list[Any] = []
    story.append(_p("SIPRIM · Informe Preliminar de Priorización de Inversiones (HU4.3)", title_style))

    # Advertencia obligatoria en primera página
    warn_box = Table([[_p(PROTOTYPE_DISCLAIMER, warn_style)]], colWidths=[doc.width])
    warn_box.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FEF3C7")),
            ("BOX", (0, 0), (-1, -1), 1.0, colors.HexColor("#D97706")),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ])
    )
    story.append(warn_box)
    story.append(Spacer(1, 6))

    crit_num = ranking.get("criteria_version_number")
    crit_id = ranking.get("criteria_version_id")
    crit_label = f"v{crit_num} ({crit_id})" if crit_num is not None else "No configurada"
    meta_rows = [
        [_p_bold("Cartera evaluada:", body_style), _p("Municipalidad Distrital de El Tambo · Cartera vigente PMV1/PMV3", body_style)],
        [_p_bold("Fecha y hora de generación:", body_style), _p(_fmt_dt(ranking.get("generated_at")), body_style)],
        [_p_bold("Versión de criterios aplicada:", body_style), _p(crit_label, body_style)],
        [
            _p_bold("Resumen de cartera:", body_style),
            _p(
                f"Total expedientes: {ranking.get('total_projects', 0)} · "
                f"Comparables en ranking: {ranking.get('comparable_count', 0)} · "
                f"Con coordenadas: {ranking.get('mapped_count', 0)} · "
                f"Sin coordenadas: {ranking.get('unmapped_count', 0)} · "
                f"Excluidos (no comparables): {ranking.get('excluded_count', 0)}",
                body_style,
            ),
        ],
        [
            _p_bold("Regla de comparabilidad:", body_style),
            _p(
                "Solo ingresan al ranking numérico expedientes cuya versión vigente completó las 5 dimensiones "
                "(económica, social, ambiental, técnica y jurídica) sin bloqueos activos ni desfase de versión de criterios. "
                "Los proyectos incompletos, bloqueados o no comparables se listan en la sección de excluidos sin puntaje ficticio.",
                small_style,
            ),
        ],
    ]
    meta_table = Table(meta_rows, colWidths=[48 * mm, doc.width - 48 * mm])
    meta_table.setStyle(
        TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#CBD5E1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E2E8F0")),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F8FAFC")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ])
    )
    story.append(meta_table)
    story.append(Spacer(1, 8))

    # Sección 1: Orden propuesto de proyectos comparables
    story.append(_p("1. Orden propuesto de proyectos comparables (Recomendación)", h2_style))
    ranked = ranking.get("ranked_projects", [])
    if not ranked:
        story.append(
            _p(
                "No existen expedientes comparables con evaluación multidimensional completa bajo la versión vigente de criterios.",
                body_style,
            )
        )
    else:
        for item in ranked:
            vid = str(item["project_version_id"])
            weights_txt = ", ".join(f"{k}: {v:.1f}%" for k, v in (item.get("weights") or {}).items()) or "Sin ponderación"
            scores_txt = ", ".join(f"{k}: {v:.1f}" for k, v in (item.get("input_scores") or {}).items()) or "Sin puntajes"
            geo_txt = (
                f"Coordenadas verificadas: {item['latitude']:.6f}, {item['longitude']:.6f} "
                f"(Origen: {item.get('coordinates_origin') or 'declared'}; Fuente: {item.get('coordinates_source') or 'N/D'})"
                if item.get("has_coordinates")
                else "Sin coordenadas verificables (lista complementaria; no se asigna punto ficticio)."
            )

            ev_list = evidences_by_version.get(vid, [])
            verified_ev = [ev for ev in ev_list if ev.get("verification_status", "verified") == "verified"]
            pending_ev = [ev for ev in ev_list if ev.get("verification_status", "verified") != "verified"]

            if verified_ev:
                ev_lines = "; ".join(
                    f"[{ev['document']} · {ev['version']} · {ev['locator']}] {ev['excerpt']}"
                    for ev in verified_ev
                )
            else:
                ev_lines = "Sin evidencia normativa verificable registrada para esta versión; requiere cotejo manual."

            pending_lines = ""
            if pending_ev:
                pending_parts: list[str] = []
                for ev in pending_ev:
                    tag = (
                        "Fuente simulada/demo — pendiente de cotejo"
                        if ev.get("verification_status") == "simulated"
                        else "Sin documento fuente vinculado — pendiente de cotejo"
                    )
                    note = f" ({ev['verification_note']})" if ev.get("verification_note") else ""
                    pending_parts.append(
                        f"[{tag}: {ev['document']} · {ev['version']} · {ev['locator']}] {ev['excerpt']}{note}"
                    )
                pending_lines = "; ".join(pending_parts)

            hr = human_review_by_version.get(vid, {})
            hr_txt = hr.get("summary") or "Sin dictamen humano formal emitido; la recomendación no constituye aprobación municipal."

            rows = [
                [
                    _p_bold(
                        f"Puesto #{item['rank_position']} · {item['code']} (v{item['version_number']}) — "
                        f"Puntaje global: {item['global_score']:.2f} / 100",
                        body_style,
                    ),
                ],
                [
                    _p_segments(
                        [
                            ("Proyecto: ", True),
                            (item["title"], False),
                            (" · ", False),
                            ("Presupuesto: ", True),
                            (f"S/ {float(item['estimated_budget_pen']):,.2f}", False),
                            (" · ", False),
                            ("Beneficiarios est. (ficha): ", True),
                            (f"{item['beneficiaries_count']:,}", False),
                        ],
                        small_style,
                    )
                ],
                [
                    _p_segments(
                        [
                            (f"Criterios y pesos (v{item['criteria_version_number']}): ", True),
                            (weights_txt, False),
                            (" | ", False),
                            ("Entradas: ", True),
                            (scores_txt, False),
                        ],
                        small_style,
                    )
                ],
                [
                    _p_segments(
                        [
                            ("Fórmula y explicación: ", True),
                            (f"{item['formula']} — {item['explanation']}", False),
                        ],
                        small_style,
                    )
                ],
            ]
            if item.get("tie_break_reason"):
                rows.append([_p_segments([("Desempate determinista: ", True), (item["tie_break_reason"], False)], small_style)])
            rows.extend([
                [_p_segments([("Ubicación territorial: ", True), (geo_txt, False)], small_style)],
                [_p_segments([("Evidencia normativa verificable: ", True), (ev_lines, False)], small_style)],
            ])
            if pending_lines:
                rows.append([
                    _p_segments(
                        [("Referencias registradas pendientes de cotejo (no verificables): ", True), (pending_lines, False)],
                        small_style,
                    )
                ])
            rows.append([_p_segments([("Estado de evaluación y revisión humana: ", True), (hr_txt, False)], small_style)])

            card = Table(rows, colWidths=[doc.width])
            card.setStyle(
                TableStyle([
                    ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#94A3B8")),
                    ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#F0FDFA")),
                    ("LINEBELOW", (0, 0), (0, 0), 0.6, colors.HexColor("#5EEAD4")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 3.5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
                ])
            )
            story.append(card)
            story.append(Spacer(1, 6))

    # Sección 2: Expedientes excluidos o no comparables
    excluded = ranking.get("excluded_projects", [])
    story.append(_p(f"2. Expedientes excluidos del ranking por no ser comparables ({len(excluded)})", h2_style))
    if not excluded:
        story.append(_p("No hay expedientes excluidos en esta cartera.", small_style))
    else:
        excl_rows = [
            [
                _p_bold("Expediente / Versión", small_style),
                _p_bold("Motivo de exclusión", small_style),
                _p_bold("Revisión humana / Evidencia normativa", small_style),
            ]
        ]
        for exc in excluded:
            vid = str(exc["project_version_id"])
            hr = human_review_by_version.get(vid, {})
            ev_list = evidences_by_version.get(vid, [])
            verified_ev = [ev for ev in ev_list if ev.get("verification_status", "verified") == "verified"]
            pending_ev = [ev for ev in ev_list if ev.get("verification_status", "verified") != "verified"]
            if verified_ev:
                ev_brief = f"{len(verified_ev)} cita(s) verificable(s): {verified_ev[0]['document']} ({verified_ev[0]['locator']})"
            elif pending_ev:
                ev_brief = f"Sin evidencia normativa verificable ({len(pending_ev)} referencia(s) pendiente(s) de cotejo)"
            else:
                ev_brief = "Sin evidencia normativa verificable registrada"
            hr_brief = hr.get("summary") or "Pendiente de evaluación completa"
            excl_rows.append([
                _p(f"{exc['code']} (v{exc['version_number']})\n{exc['title']}", small_style),
                _p(f"[{exc['exclusion_code']}] {exc['exclusion_reason']}", small_style),
                _p(f"{hr_brief}\n{ev_brief}", small_style),
            ])
        excl_table = Table(excl_rows, colWidths=[52 * mm, 64 * mm, doc.width - 116 * mm], repeatRows=1)
        excl_table.setStyle(
            TableStyle([
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#CBD5E1")),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E2E8F0")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#FEF3C7")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ])
        )
        story.append(excl_table)

    doc.build(story, onFirstPage=_draw_footer, onLaterPages=_draw_footer)
    return buffer.getvalue()
