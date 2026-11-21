import os
import urllib.parse
from xml.sax.saxutils import escape
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

TALLY_BASE_URL = os.getenv("TALLY_FORM_URL", "https://tally.so/r/BEdLx4")
GBP_FMT = "\u00a3#,##0.00"
FORMULA_MAX_ROW = 500


def truncate_word_boundary(text: str, max_len: int) -> str:
    s = " ".join(str(text or "").split())
    if len(s) <= max_len:
        return s
    cut = s[:max_len].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut if cut else s[:max_len]


def build_tally_link(building: str, door_id: str, defect: str) -> str:
    params = urllib.parse.urlencode({
        "building": truncate_word_boundary(str(building), 60),
        "door": str(door_id).strip()[:40],
        "defect": truncate_word_boundary(str(defect), 80),
    })
    return f"{TALLY_BASE_URL}?{params}"


def safe_excel_text(val, default: str = "") -> str:
    s = str(val if val is not None else default).strip().lstrip("=+").strip()
    return s if s else default


def clean_contractor_name(raw_name: str, client_company: str) -> str:
    s = safe_excel_text(raw_name, "")
    if not s or "gaming" in s.lower():
        return "UK Fire Door Remediation Contractor"
    return s


def get_door_materials(d: dict) -> list[dict]:
    mats = d.get("materials")
    if isinstance(mats, list) and len(mats) > 0:
        out = []
        for m in mats:
            if not isinstance(m, dict):
                continue
            name = safe_excel_text(m.get("part_needed"), "UNMAPPED")
            try:
                qty = int(float(m.get("qty", 0)))
            except (ValueError, TypeError):
                qty = 0
            try:
                unit_cost = float(m.get("unit_cost_gbp", 0.0))
            except (ValueError, TypeError):
                unit_cost = 0.0
            if qty <= 0:
                continue
            out.append({
                "part_needed": name,
                "unit": safe_excel_text(m.get("unit"), "Unit"),
                "qty": qty,
                "unit_cost_gbp": unit_cost,
                "is_unmapped": bool(m.get("is_unmapped", False)) or "UNMAPPED" in name.upper(),
            })
        return out

    # Backwards compatibility for legacy single-part door dict
    part_name = safe_excel_text(d.get("part_needed"), "UNMAPPED")
    try:
        qty = int(float(d.get("qty", 0)))
    except (ValueError, TypeError):
        qty = 0
    try:
        unit_cost = float(d.get("unit_cost_gbp", 0.0))
    except (ValueError, TypeError):
        unit_cost = 0.0
    if qty <= 0 or part_name in ("No Parts Required", "Client-Supplied (Labour Only)"):
        return []
    return [{
        "part_needed": part_name,
        "unit": "Unit",
        "qty": qty,
        "unit_cost_gbp": unit_cost,
        "is_unmapped": bool(d.get("is_unmapped", False)) or "UNMAPPED" in part_name.upper(),
    }]


def generate_excel_pack(building_info: dict, doors: list, output_path: str):
    wb = openpyxl.Workbook()
    wb.calculation.calcMode = "auto"
    wb.calculation.fullCalcOnLoad = True
    wb.calculation.forceFullCalc = True

    navy_fill = PatternFill("solid", fgColor="1B365D")
    blue_fill = PatternFill("solid", fgColor="2C5282")
    light_fill = PatternFill("solid", fgColor="EDF2F7")
    yellow_fill = PatternFill("solid", fgColor="FEF9C3")
    p1_fill = PatternFill("solid", fgColor="FEE2E2")
    p2_fill = PatternFill("solid", fgColor="FEF3C7")
    p3_fill = PatternFill("solid", fgColor="DCFCE7")

    white_bold = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    title_font = Font(name="Calibri", size=15, bold=True, color="1B365D")
    bold_font = Font(name="Calibri", size=11, bold=True)
    warn_font = Font(name="Calibri", size=10, bold=True, color="991B1B")
    ok_font = Font(name="Calibri", size=10, bold=True, color="166534")
    reg_font = Font(name="Calibri", size=10)
    link_font = Font(name="Calibri", size=10, underline="single", color="0563C1")

    thin_side = Side(border_style="thin", color="CBD5E0")
    border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)

    client_company = safe_excel_text(building_info.get("client_company"), "Commercial Client")
    contractor_company = clean_contractor_name(building_info.get("contractor_company"), client_company)
    building_name = safe_excel_text(building_info.get("building_name"), "Commercial Site").lstrip("–-— ").strip()
    standard_version = safe_excel_text(building_info.get("standard_version"), "BS 8214:2016")
    survey_date = safe_excel_text(building_info.get("survey_date"), "")

    # Pre-build Material_Breakdown rows & unique BoM items
    breakdown_rows = []
    unique_parts = {}
    has_unmapped_any = False

    for d in doors:
        door_id = safe_excel_text(d.get("door_id"), "Unknown")
        mats = get_door_materials(d)
        for m in mats:
            breakdown_rows.append((door_id, m["part_needed"], m["unit"], m["qty"], m["unit_cost_gbp"], m["is_unmapped"]))
            if m["is_unmapped"]:
                has_unmapped_any = True
            if m["part_needed"] not in unique_parts:
                unique_parts[m["part_needed"]] = (m["unit_cost_gbp"], m["is_unmapped"])

    # TAB 1: Executive_Dashboard
    ws_dash = wb.active
    ws_dash.title = "Executive_Dashboard"
    ws_dash["B2"] = "UK FIRE DOOR COMPLIANCE & REMEDIATION DASHBOARD"
    ws_dash["B2"].font = title_font
    ws_dash["B3"] = f"Prepared by: {contractor_company} | Client: {client_company} | Site: {building_name}"
    ws_dash["B4"] = f"Standard: {standard_version} | Report Date: {survey_date}"

    # Estimator Review Alert Banner (dynamic formula)
    ws_dash.merge_cells("B6:C6")
    ws_dash["B6"] = (
        f'=IF(COUNTIF(Material_Breakdown!B5:B{FORMULA_MAX_ROW},"*UNMAPPED*")>0,'
        f'"ESTIMATOR REVIEW REQUIRED: "&COUNTIF(Material_Breakdown!B5:B{FORMULA_MAX_ROW},"*UNMAPPED*")'
        f'&" UNMAPPED item(s) at £0.00 — add manual trade quote before issuing to client.",'
        f'"OK: All active remedial materials mapped to UK trade catalogue.")'
    )
    ws_dash["B6"].fill = p1_fill if has_unmapped_any else p3_fill
    ws_dash["B6"].font = warn_font if has_unmapped_any else ok_font
    ws_dash["B6"].alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    ws_dash["B6"].border = border
    ws_dash["C6"].border = border

    ws_dash["B8"] = "AUDIT KPI METRIC"
    ws_dash["C8"] = "VALUE"
    for col in ["B8", "C8"]:
        ws_dash[col].fill = navy_fill
        ws_dash[col].font = white_bold
        ws_dash[col].alignment = Alignment(horizontal="center")

    no_access_ids = building_info.get("no_access_ids") or []
    excluded_doors = building_info.get("excluded_doors") or []
    excluded_summary = "; ".join(f"{e['door_id']} ({e['reason']})" for e in excluded_doors) or "None"

    kpis = [
        ("Total Doors Surveyed", int(building_info.get("total_doors_surveyed", len(doors)))),
        ("Passed (Compliant)", int(building_info.get("passed_count", 0))),
        ("Failed / Active Carpentry Scope", f"=COUNTA(Carpenter_Schedule!A5:A{FORMULA_MAX_ROW})"),
        ("No Access (Re-Book Required)", int(building_info.get("no_access_count", 0))),
        ("Excluded / On Hold (Asbestos / M&E / IPC)", int(building_info.get("excluded_count", len(excluded_doors)))),
        ("Critical Priority (P0 - Immediate)", f'=COUNTIF(Carpenter_Schedule!K5:K{FORMULA_MAX_ROW}, "Critical (P0)")'),
        ("High Priority (P1 - Urgent)", f'=COUNTIF(Carpenter_Schedule!K5:K{FORMULA_MAX_ROW}, "High (P1)")'),
        ("Medium Priority (P2 - Standard)", f'=COUNTIF(Carpenter_Schedule!K5:K{FORMULA_MAX_ROW}, "Medium (P2)")'),
        ("Low Priority (P3 - Advisory)", f'=COUNTIF(Carpenter_Schedule!K5:K{FORMULA_MAX_ROW}, "Low (P3)")'),
        ("Total Estimated Joinery Labour (Hours)", f"=ROUND(SUM(Carpenter_Schedule!J5:J{FORMULA_MAX_ROW})/60, 2)"),
    ]

    for idx, (label, val) in enumerate(kpis, start=9):
        ws_dash[f"B{idx}"] = label
        ws_dash[f"B{idx}"].font = bold_font
        ws_dash[f"B{idx}"].border = border
        ws_dash[f"C{idx}"] = val
        ws_dash[f"C{idx}"].font = bold_font
        ws_dash[f"C{idx}"].alignment = Alignment(horizontal="center")
        ws_dash[f"C{idx}"].border = border

    ws_dash["B20"] = "No Access Door IDs (Re-Booking List)"
    ws_dash["B20"].font = bold_font
    ws_dash["B20"].fill = light_fill
    ws_dash["B20"].border = border
    ws_dash["C20"] = safe_excel_text(", ".join(str(x) for x in no_access_ids), "None")
    ws_dash["C20"].font = reg_font
    ws_dash["C20"].border = border
    ws_dash["C20"].alignment = Alignment(horizontal="center", wrap_text=True)

    ws_dash["B21"] = "Quarantined / Excluded Doors (Asbestos / M&E)"
    ws_dash["B21"].font = bold_font
    ws_dash["B21"].fill = p1_fill
    ws_dash["B21"].border = border
    ws_dash["C21"] = safe_excel_text(excluded_summary, "None")
    ws_dash["C21"].font = reg_font
    ws_dash["C21"].border = border
    ws_dash["C21"].alignment = Alignment(horizontal="center", wrap_text=True)

    ws_dash.column_dimensions["B"].width = 44
    ws_dash.column_dimensions["C"].width = 65

    # TAB 2: Carpenter_Schedule
    ws_carp = wb.create_sheet("Carpenter_Schedule")
    ws_carp["A2"] = f"BS 8214 CARPENTER SCHEDULE OF WORKS - {building_name}"
    ws_carp["A2"].font = title_font

    headers = [
        "Door ID", "Floor / Location", "Rating", "Recorded Defect",
        "BS 8214 Remedial Action", "Proposed Materials (See Material_Breakdown)", "Total Qty",
        "Unit Part (\u00a3)", "Total Parts (\u00a3)", "Labour (Mins)",
        "Priority", "Field Sign-Off Portal"
    ]
    for c_idx, h in enumerate(headers, start=1):
        cell = ws_carp.cell(4, c_idx, h)
        cell.fill = blue_fill
        cell.font = white_bold
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border

    for r_offset, d in enumerate(doors):
        r = 5 + r_offset
        door_id = safe_excel_text(d.get("door_id"), "Unknown")
        floor = safe_excel_text(d.get("floor"), "Unknown")
        rating = safe_excel_text(d.get("rating"), "Unknown")
        defect = safe_excel_text(d.get("defect"), "Unspecified defect")
        remedial_action = safe_excel_text(d.get("remedial_action"), "MANUAL REVIEW REQUIRED")
        priority = safe_excel_text(d.get("priority"), "Medium (P2)")

        mats = get_door_materials(d)
        if len(mats) == 0:
            mat_display = safe_excel_text(d.get("materials_summary") or d.get("part_needed"), "Labour Only / No Additional Parts")
            unit_display = 0.0
        elif len(mats) == 1:
            mat_display = mats[0]["part_needed"]
            unit_display = mats[0]["unit_cost_gbp"]
        else:
            mat_display = "; ".join(f"{m['qty']}x {m['part_needed']}" for m in mats)
            unit_display = "Multi (see Breakdown)"

        try:
            labour_mins = int(float(d.get("labour_mins", 0)))
        except (ValueError, TypeError):
            labour_mins = 0

        tally_url = build_tally_link(building_name, door_id, defect)
        row_vals = [
            door_id,
            floor,
            rating,
            defect,
            remedial_action,
            mat_display,
            f"=SUMIF(Material_Breakdown!$A$5:$A${FORMULA_MAX_ROW}, A{r}, Material_Breakdown!$D$5:$D${FORMULA_MAX_ROW})",
            unit_display,
            f"=SUMIF(Material_Breakdown!$A$5:$A${FORMULA_MAX_ROW}, A{r}, Material_Breakdown!$F$5:$F${FORMULA_MAX_ROW})",
            labour_mins,
            priority,
            "Tap to Upload Photo",
        ]
        for c_idx, val in enumerate(row_vals, start=1):
            cell = ws_carp.cell(r, c_idx, val)
            if c_idx == 12:
                cell.hyperlink = tally_url
                cell.font = link_font
            else:
                cell.font = reg_font
            cell.border = border
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            if c_idx == 8 and isinstance(val, (int, float)):
                cell.number_format = GBP_FMT
            if c_idx == 9:
                cell.number_format = GBP_FMT
            if c_idx == 6 and "UNMAPPED" in str(val).upper():
                cell.fill = p1_fill
                cell.font = warn_font
            if c_idx == 11:
                if "Critical" in str(val) or "High" in str(val):
                    cell.fill = p1_fill
                elif "Medium" in str(val):
                    cell.fill = p2_fill
                else:
                    cell.fill = p3_fill

    col_widths = [20, 16, 10, 34, 38, 42, 10, 16, 14, 13, 15, 22]
    for idx, w in enumerate(col_widths, start=1):
        ws_carp.column_dimensions[openpyxl.utils.get_column_letter(idx)].width = w

    # TAB 3: Material_Order_BoM
    ws_bom = wb.create_sheet("Material_Order_BoM")
    ws_bom["A2"] = "IRONMONGERY & INTUMESCENT MATERIAL ORDER LIST (BoM)"
    ws_bom["A2"].font = title_font

    bom_headers = ["Proposed Material / Ironmongery", "Total Qty Needed", "Unit Cost (\u00a3)", "Total Material Cost (\u00a3)"]
    for c_idx, h in enumerate(bom_headers, start=1):
        cell = ws_bom.cell(4, c_idx, h)
        cell.fill = navy_fill
        cell.font = white_bold
        cell.border = border

    parts_list = list(unique_parts.items()) or [("No Catalogue Materials Required", (0.0, False))]
    for idx, (p_name, (u_cost, is_unm)) in enumerate(parts_list):
        r = 5 + idx
        c_name = ws_bom.cell(r, 1, safe_excel_text(p_name, "No Catalogue Materials Required"))
        c_name.border = border
        c_qty = ws_bom.cell(r, 2, f'=SUMIF(Material_Breakdown!$B$5:$B${FORMULA_MAX_ROW}, A{r}, Material_Breakdown!$D$5:$D${FORMULA_MAX_ROW})')
        c_qty.border = border
        c_unit = ws_bom.cell(r, 3, u_cost)
        c_unit.number_format = GBP_FMT
        c_unit.border = border
        c_tot = ws_bom.cell(r, 4, f"=B{r}*C{r}")
        c_tot.number_format = GBP_FMT
        c_tot.border = border
        if is_unm:
            for c_cell in (c_name, c_qty, c_unit, c_tot):
                c_cell.fill = p1_fill
                c_cell.font = warn_font

    tot_r = 5 + len(parts_list)
    ws_bom.cell(tot_r, 1, "TOTAL MATERIAL COST").font = bold_font
    ws_bom.cell(tot_r, 1).fill = light_fill
    ws_bom.cell(tot_r, 1).border = border
    c_bom_sum = ws_bom.cell(tot_r, 4, f"=SUM(D5:D{tot_r - 1})")
    c_bom_sum.font = bold_font
    c_bom_sum.fill = light_fill
    c_bom_sum.number_format = GBP_FMT
    c_bom_sum.border = border

    for idx, w in enumerate([56, 18, 16, 22], start=1):
        ws_bom.column_dimensions[openpyxl.utils.get_column_letter(idx)].width = w

    # TAB 4: 1Click_Quote_Calculator
    ws_q = wb.create_sheet("1Click_Quote_Calculator")
    ws_q["B2"] = "1-CLICK CLIENT QUOTATION CALCULATOR (EDIT YELLOW CELLS)"
    ws_q["B2"].font = title_font

    ws_q.merge_cells("B3:C3")
    ws_q["B3"] = (
        f'=IF(COUNTIF(Material_Breakdown!B5:B{FORMULA_MAX_ROW},"*UNMAPPED*")>0,'
        f'"WARNING: "&COUNTIF(Material_Breakdown!B5:B{FORMULA_MAX_ROW},"*UNMAPPED*")'
        f'&" UNMAPPED part(s) at £0.00 — update Unit Cost in Material_Order_BoM before sending quote!",'
        f'"All material line items priced from catalogue.")'
    )
    ws_q["B3"].fill = p1_fill if has_unmapped_any else p3_fill
    ws_q["B3"].font = warn_font if has_unmapped_any else ok_font
    ws_q["B3"].alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)

    ws_q["B4"] = "COMMERCIAL PARAMETER / LINE ITEM"
    ws_q["C4"] = "VALUE"
    for col in ["B4", "C4"]:
        ws_q[col].fill = navy_fill
        ws_q[col].font = white_bold
        ws_q[col].border = border

    quote_rows = [
        ("Joiner Hourly Rate (\u00a3/hr)", float(building_info.get("hourly_rate", 55.0)), GBP_FMT, True),
        ("Material Markup (%)", float(building_info.get("material_markup", 0.15)), "0.0%", True),
        ("UK VAT Rate (%)", float(building_info.get("vat_rate", 0.20)), "0.0%", True),
        ("", "", None, False),
        ("Total Estimated Joinery Hours", "=Executive_Dashboard!C18", "0.00", False),
        ("Total Joinery Labour Cost (\u00a3)", "=ROUND(C9*C5, 2)", GBP_FMT, False),
        ("Base Material Cost (\u00a3)", f"=Material_Order_BoM!D{tot_r}", GBP_FMT, False),
        ("Material Total with Markup (\u00a3)", "=ROUND(C11*(1+C6), 2)", GBP_FMT, False),
        ("Site Setup / Certification Fee (GBP)", float(building_info.get("site_fee", 0.0)), GBP_FMT, True),
        ("NET QUOTE SUBTOTAL (Excl. VAT)", "=C10+C12+C13", GBP_FMT, False),
        ("UK VAT Amount (\u00a3)", "=ROUND(C14*C7, 2)", GBP_FMT, False),
        ("GROSS CLIENT QUOTE (Incl. VAT)", "=C14+C15", GBP_FMT, False),
    ]
    for offset, (label, val, num_fmt, is_input) in enumerate(quote_rows):
        r = 5 + offset
        if not label:
            continue
        c_lbl = ws_q.cell(r, 2, label)
        c_val = ws_q.cell(r, 3, val)
        c_lbl.font = bold_font if r >= 14 or is_input else reg_font
        c_val.font = bold_font if r >= 14 or is_input else reg_font
        c_lbl.border = border
        c_val.border = border
        if num_fmt:
            c_val.number_format = num_fmt
        if is_input:
            c_val.fill = yellow_fill
        elif r in [14, 16]:
            c_lbl.fill = light_fill
            c_val.fill = light_fill

    ws_q.column_dimensions["B"].width = 44
    ws_q.column_dimensions["C"].width = 26

    # TAB 5: Material_Breakdown (Door-by-Door Multi-Part Line Items)
    ws_mb = wb.create_sheet("Material_Breakdown")
    ws_mb["A2"] = "DOOR-BY-DOOR MATERIAL LINE-ITEM BREAKDOWN (FEEDS BoM & SCHEDULE)"
    ws_mb["A2"].font = title_font

    mb_headers = ["Door ID", "Material / Ironmongery", "Unit", "Qty", "Unit Cost (\u00a3)", "Line Total (\u00a3)"]
    for c_idx, h in enumerate(mb_headers, start=1):
        cell = ws_mb.cell(4, c_idx, h)
        cell.fill = blue_fill
        cell.font = white_bold
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border

    for idx, (d_id, m_name, m_unit, m_qty, m_cost, is_unm) in enumerate(breakdown_rows):
        r = 5 + idx
        c1 = ws_mb.cell(r, 1, d_id)
        c2 = ws_mb.cell(r, 2, m_name)
        c3 = ws_mb.cell(r, 3, m_unit)
        c4 = ws_mb.cell(r, 4, m_qty)
        c5 = ws_mb.cell(r, 5, m_cost)
        c6 = ws_mb.cell(r, 6, f"=D{r}*E{r}")
        c5.number_format = GBP_FMT
        c6.number_format = GBP_FMT
        for c_cell in (c1, c2, c3, c4, c5, c6):
            c_cell.font = warn_font if is_unm else reg_font
            c_cell.border = border
            if is_unm:
                c_cell.fill = p1_fill

    for idx, w in enumerate([20, 56, 15, 10, 16, 16], start=1):
        ws_mb.column_dimensions[openpyxl.utils.get_column_letter(idx)].width = w

    wb.save(output_path)


def generate_prework_pdf(building_info: dict, doors: list, output_path: str):
    doc = SimpleDocTemplate(output_path, pagesize=landscape(A4), leftMargin=20, rightMargin=20, topMargin=20, bottomMargin=20)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleStyle", parent=styles["Heading1"], fontSize=14.5, textColor=colors.HexColor("#1B365D"), spaceAfter=4)
    sub_style = ParagraphStyle("SubStyle", parent=styles["Normal"], fontSize=9, leading=12, textColor=colors.HexColor("#2C5282"), spaceAfter=8)
    cell_style = ParagraphStyle("CellStyle", parent=styles["Normal"], fontSize=7.8, leading=10)
    head_style = ParagraphStyle("HeadStyle", parent=styles["Normal"], fontSize=8.2, leading=10, textColor=colors.white, fontName="Helvetica-Bold")

    clean_site = str(building_info.get("building_name", "Commercial Site")).lstrip("–-— ").strip()
    client_raw = str(building_info.get("client_company", "Commercial Client"))
    contractor_clean = clean_contractor_name(building_info.get("contractor_company", ""), client_raw)
    contractor_esc = escape(contractor_clean)
    client_esc = escape(client_raw)
    building_esc = escape(clean_site)
    date_esc = escape(str(building_info.get("survey_date", "")))
    std_esc = escape(str(building_info.get("standard_version", "BS 8214:2016")))

    elements = [
        Paragraph("UK FIRE DOOR REMEDIATION SCHEDULE & PRE-WORK COMPLIANCE REPORT", title_style),
        Paragraph(
            f"<b>Prepared by:</b> {contractor_esc} | <b>Client:</b> {client_esc} | <b>Site:</b> {building_esc} | "
            f"<b>Report Date:</b> {date_esc} | <b>Standard:</b> {std_esc}",
            sub_style
        )
    ]

    total_d = int(building_info.get("total_doors_surveyed", 0))
    fail_d = len(doors)
    no_access_d = int(building_info.get("no_access_count", 0))
    excluded_d = int(building_info.get("excluded_count", 0))
    pass_d = int(building_info.get("passed_count", 0))
    p1_count = sum(1 for d in doors if "Critical" in str(d.get("priority", "")) or "High" in str(d.get("priority", "")))

    kpi_table = Table(
        [
            ["Total Surveyed", "Passed", "Active Scope", "No Access", "Excluded (Hold/M&E)", "Critical / High (P0/P1)", "Governing Standard"],
            [str(total_d), str(pass_d), str(fail_d), str(no_access_d), str(excluded_d), str(p1_count), f"BSA 2022, Fire Safety Regs 2022 & {building_info.get('standard_version', 'BS 8214:2016')}"]
        ],
        colWidths=[80, 55, 75, 65, 105, 105, 315]
    )
    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1B365D")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#EDF2F7")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E0")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.extend([kpi_table, Spacer(1, 10)])

    headers = ["Door ID", "Floor", "Rating", "Recorded Defect (Page Ref)", "Required BS 8214 Remedial Works", "Proposed Materials", "Priority", "Site Evidence"]
    table_data = [[Paragraph(h, head_style) for h in headers]]
    for d in doors:
        door_id_raw = str(d.get("door_id", "Unknown"))
        defect_raw = str(d.get("defect", "Unspecified defect")).lstrip("=+").strip()
        door_id_esc = escape(door_id_raw)
        floor_esc = escape(str(d.get("floor", "Unknown")))
        rating_esc = escape(str(d.get("rating", "Unknown")))
        defect_esc = escape(defect_raw)
        remedial_esc = escape(str(d.get("remedial_action", "MANUAL REVIEW REQUIRED")))
        priority_esc = escape(str(d.get("priority", "Medium (P2)")))
        page_tag = escape(f" (p.{d.get('source_page')})") if d.get("source_page") else ""

        mats = get_door_materials(d)
        if mats:
            part_html = "<br/>".join(f"• {escape(str(m['qty']))}x {escape(m['part_needed'])}" for m in mats)
        else:
            fallback_mat = str(d.get("materials_summary") or d.get("part_needed") or "Labour Only")
            part_html = escape(fallback_mat)

        tally_url = escape(build_tally_link(clean_site, door_id_raw, defect_raw))
        evidence_html = f'Pending Photo<br/><a href="{tally_url}" color="#0563C1"><u>Upload Evidence</u></a>'

        table_data.append([
            Paragraph(door_id_esc, cell_style),
            Paragraph(floor_esc, cell_style),
            Paragraph(rating_esc, cell_style),
            Paragraph(f"{defect_esc}{page_tag}", cell_style),
            Paragraph(remedial_esc, cell_style),
            Paragraph(part_html, cell_style),
            Paragraph(priority_esc, cell_style),
            Paragraph(evidence_html, cell_style),
        ])

    main_table = Table(table_data, colWidths=[72, 65, 45, 165, 185, 138, 58, 72], repeatRows=1)
    main_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2C5282")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7FAFC")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    elements.append(main_table)

    no_access_ids = building_info.get("no_access_ids") or []
    if no_access_ids:
        na_text = escape(", ".join(str(x) for x in no_access_ids))
        elements.extend([
            Spacer(1, 8),
            Paragraph(f"<b>No Access Doors (Re-Booking Required):</b> {na_text}", sub_style)
        ])

    excluded_list = building_info.get("excluded_doors") or []
    if excluded_list:
        excl_text = "; ".join(f"{escape(str(e['door_id']))} ({escape(str(e['rating']))}): {escape(str(e['reason']))}" for e in excluded_list)
        elements.extend([
            Spacer(1, 6),
            Paragraph(f"<b>Quarantined / Out-of-Scope Doors (Excluded from Carpentry Quote):</b> {excl_text}", sub_style)
        ])

    elements.extend([
        Spacer(1, 8),
        Paragraph(
            f"<b>Stage 1 Desktop Estimate & Compliance Notice:</b> Generated under {std_esc} from client-supplied survey records. "
            "Valid for 30 days. Excludes asbestos removal/R&amp;D surveys, M&amp;E/fire alarm interface works, re-decoration, and No-Access re-attendance charges. "
            "All remedial specifications and pricing remain subject to physical on-site verification and independent competent-person Golden Thread sign-off.",
            sub_style
        )
    ])
    doc.build(elements)
