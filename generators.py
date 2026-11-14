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


def build_tally_link(building: str, door_id: str, defect: str) -> str:
    params = urllib.parse.urlencode({
        "building": str(building)[:60],
        "door": str(door_id)[:40],
        "defect": str(defect)[:80]
    })
    return f"{TALLY_BASE_URL}?{params}"


def safe_excel_text(val, default: str = "") -> str:
    s = str(val if val is not None else default).strip()
    if not s:
        s = default
    if s.startswith("="):
        return " " + s
    return s


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
    reg_font = Font(name="Calibri", size=10)
    link_font = Font(name="Calibri", size=10, underline="single", color="0563C1")

    thin_side = Side(border_style="thin", color="CBD5E0")
    border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)

    contractor_company = safe_excel_text(building_info.get("contractor_company"), "UK Fire Contractor")
    client_company = safe_excel_text(building_info.get("client_company"), "Commercial Client")
    building_name = safe_excel_text(building_info.get("building_name"), "Commercial Site").lstrip("–-— ").strip()
    standard_version = safe_excel_text(building_info.get("standard_version"), "BS 8214:2016")
    survey_date = safe_excel_text(building_info.get("survey_date"), "")
    last_row = max(5, 4 + len(doors))

    # TAB 1: Executive_Dashboard
    ws_dash = wb.active
    ws_dash.title = "Executive_Dashboard"
    ws_dash["B2"] = "UK FIRE DOOR COMPLIANCE & REMEDIATION DASHBOARD"
    ws_dash["B2"].font = title_font
    ws_dash["B3"] = f"Prepared for: {contractor_company} | Client: {client_company} | Site: {building_name}"
    ws_dash["B4"] = f"Standard: {standard_version} | Report Date: {survey_date}"

    ws_dash["B8"] = "AUDIT KPI METRIC"
    ws_dash["C8"] = "VALUE"
    for col in ["B8", "C8"]:
        ws_dash[col].fill = navy_fill
        ws_dash[col].font = white_bold
        ws_dash[col].alignment = Alignment(horizontal="center")

    no_access_ids = building_info.get("no_access_ids") or []
    kpis = [
        ("Total Doors Surveyed", int(building_info.get("total_doors_surveyed", len(doors)))),
        ("Passed (Compliant)", int(building_info.get("passed_count", 0))),
        ("Failed / Action Required", f"=COUNTA(Carpenter_Schedule!A5:A{last_row})"),
        ("No Access (Re-Book Required)", int(building_info.get("no_access_count", 0))),
        ("High Priority (P1 - Urgent)", f'=COUNTIF(Carpenter_Schedule!K5:K{last_row}, "High (P1)")'),
        ("Medium Priority (P2 - Standard)", f'=COUNTIF(Carpenter_Schedule!K5:K{last_row}, "Medium (P2)")'),
        ("Low Priority (P3 - Advisory)", f'=COUNTIF(Carpenter_Schedule!K5:K{last_row}, "Low (P3)")'),
        ("Total Estimated Joinery Labour (Hours)", f"=ROUND(SUM(Carpenter_Schedule!J5:J{last_row})/60, 2)"),
    ]
    for idx, (label, val) in enumerate(kpis, start=9):
        ws_dash[f"B{idx}"] = label
        ws_dash[f"B{idx}"].font = bold_font
        ws_dash[f"B{idx}"].border = border
        ws_dash[f"C{idx}"] = val
        ws_dash[f"C{idx}"].font = bold_font
        ws_dash[f"C{idx}"].alignment = Alignment(horizontal="center")
        ws_dash[f"C{idx}"].border = border

    ws_dash["B18"] = "No Access Door IDs (Re-Booking List)"
    ws_dash["B18"].font = bold_font
    ws_dash["B18"].fill = light_fill
    ws_dash["B18"].border = border
    ws_dash["C18"] = safe_excel_text(", ".join(str(x) for x in no_access_ids), "None")
    ws_dash["C18"].font = reg_font
    ws_dash["C18"].border = border
    ws_dash["C18"].alignment = Alignment(horizontal="center", wrap_text=True)

    ws_dash.column_dimensions["B"].width = 42
    ws_dash.column_dimensions["C"].width = 28

    # TAB 2: Carpenter_Schedule
    ws_carp = wb.create_sheet("Carpenter_Schedule")
    ws_carp["A2"] = f"BS 8214 CARPENTER SCHEDULE OF WORKS - {building_name}"
    ws_carp["A2"].font = title_font

    headers = [
        "Door ID", "Floor / Location", "Rating", "Recorded Defect",
        "BS 8214 Remedial Action", "Proposed Material", "Qty",
        "Unit Part (\u00a3)", "Total Parts (\u00a3)", "Labour (Mins)",
        "Priority", "Field Sign-Off Portal"
    ]
    for c_idx, h in enumerate(headers, start=1):
        cell = ws_carp.cell(4, c_idx, h)
        cell.fill = blue_fill
        cell.font = white_bold
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border

    unique_parts = {}
    for r_offset, d in enumerate(doors):
        r = 5 + r_offset
        door_id = safe_excel_text(d.get("door_id"), "Unknown")
        floor = safe_excel_text(d.get("floor"), "Unknown")
        rating = safe_excel_text(d.get("rating"), "Unknown")
        defect = safe_excel_text(d.get("defect"), "Unspecified defect")
        remedial_action = safe_excel_text(d.get("remedial_action"), "MANUAL REVIEW REQUIRED")
        part_name = safe_excel_text(d.get("part_needed"), "UNMAPPED")
        priority = safe_excel_text(d.get("priority"), "Medium (P2)")

        try:
            qty = int(float(d.get("qty", 0)))
        except (ValueError, TypeError):
            qty = 0
        try:
            unit_cost = float(d.get("unit_cost_gbp", 0.0))
        except (ValueError, TypeError):
            unit_cost = 0.0
        try:
            labour_mins = int(float(d.get("labour_mins", 0)))
        except (ValueError, TypeError):
            labour_mins = 0

        if part_name not in unique_parts and part_name not in ("No Parts Required", "UNMAPPED", "Client-Supplied (Labour Only)"):
            unique_parts[part_name] = unit_cost

        tally_url = build_tally_link(building_name, door_id, defect)
        row_vals = [
            door_id, floor, rating, defect,
            remedial_action, part_name, qty,
            unit_cost, f"=G{r}*H{r}", labour_mins,
            priority, "Tap to Upload Photo"
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
            if c_idx in [8, 9]:
                cell.number_format = GBP_FMT
            if c_idx == 11:
                if "High" in str(val):
                    cell.fill = p1_fill
                elif "Medium" in str(val):
                    cell.fill = p2_fill
                else:
                    cell.fill = p3_fill

    col_widths = [24, 16, 10, 34, 38, 36, 8, 13, 14, 13, 15, 22]
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

    parts_list = list(unique_parts.items()) or [("No Catalogue Materials Required", 0.0)]
    for idx, (p_name, u_cost) in enumerate(parts_list):
        r = 5 + idx
        ws_bom.cell(r, 1, safe_excel_text(p_name, "No Catalogue Materials Required")).border = border
        c_qty = ws_bom.cell(r, 2, f'=SUMIF(Carpenter_Schedule!$F$5:$F${last_row}, A{r}, Carpenter_Schedule!$G$5:$G${last_row})')
        c_qty.border = border
        c_unit = ws_bom.cell(r, 3, u_cost)
        c_unit.number_format = GBP_FMT
        c_unit.border = border
        c_tot = ws_bom.cell(r, 4, f"=B{r}*C{r}")
        c_tot.number_format = GBP_FMT
        c_tot.border = border

    tot_r = 5 + len(parts_list)
    ws_bom.cell(tot_r, 1, "TOTAL MATERIAL COST").font = bold_font
    ws_bom.cell(tot_r, 1).fill = light_fill
    ws_bom.cell(tot_r, 1).border = border
    c_bom_sum = ws_bom.cell(tot_r, 4, f"=SUM(D5:D{tot_r - 1})")
    c_bom_sum.font = bold_font
    c_bom_sum.fill = light_fill
    c_bom_sum.number_format = GBP_FMT
    c_bom_sum.border = border

    for idx, w in enumerate([52, 18, 16, 22], start=1):
        ws_bom.column_dimensions[openpyxl.utils.get_column_letter(idx)].width = w

    # TAB 4: 1Click_Quote_Calculator
    ws_q = wb.create_sheet("1Click_Quote_Calculator")
    ws_q["B2"] = "1-CLICK CLIENT QUOTATION CALCULATOR (EDIT YELLOW CELLS)"
    ws_q["B2"].font = title_font

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
        ("Total Estimated Joinery Hours", "=Executive_Dashboard!C16", "0.00", False),
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

    ws_q.column_dimensions["B"].width = 40
    ws_q.column_dimensions["C"].width = 22
    wb.save(output_path)


def generate_prework_pdf(building_info: dict, doors: list, output_path: str):
    doc = SimpleDocTemplate(output_path, pagesize=landscape(A4), leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleStyle", parent=styles["Heading1"], fontSize=15, textColor=colors.HexColor("#1B365D"), spaceAfter=4)
    sub_style = ParagraphStyle("SubStyle", parent=styles["Normal"], fontSize=9.5, textColor=colors.HexColor("#2C5282"), spaceAfter=10)
    cell_style = ParagraphStyle("CellStyle", parent=styles["Normal"], fontSize=8, leading=10)
    head_style = ParagraphStyle("HeadStyle", parent=styles["Normal"], fontSize=8.5, leading=10, textColor=colors.white, fontName="Helvetica-Bold")

    clean_site = str(building_info.get("building_name", "Commercial Site")).lstrip("–-— ").strip()
    contractor_esc = escape(str(building_info.get("contractor_company", "UK Fire Contractor")))
    client_esc = escape(str(building_info.get("client_company", "Commercial Client")))
    building_esc = escape(clean_site)
    date_esc = escape(str(building_info.get("survey_date", "")))
    std_esc = escape(str(building_info.get("standard_version", "BS 8214:2016")))

    elements = [
        Paragraph("UK FIRE DOOR REMEDIATION SCHEDULE & PRE-WORK COMPLIANCE REPORT", title_style),
        Paragraph(
            f"<b>Prepared for:</b> {contractor_esc} | <b>Client:</b> {client_esc} | <b>Site:</b> {building_esc} | "
            f"<b>Report Date:</b> {date_esc} | <b>Standard:</b> {std_esc}",
            sub_style
        )
    ]

    total_d = int(building_info.get("total_doors_surveyed", 0))
    fail_d = len(doors)
    no_access_d = int(building_info.get("no_access_count", 0))
    pass_d = int(building_info.get("passed_count", 0))
    p1_count = sum(1 for d in doors if "High" in str(d.get("priority", "")))

    kpi_table = Table(
        [
            ["Total Surveyed", "Passed", "Action Required", "No Access", "High Priority (P1)", "Governing Standard"],
            [str(total_d), str(pass_d), str(fail_d), str(no_access_d), str(p1_count), f"Fire Safety Regs 2022 & {building_info.get('standard_version', 'BS 8214:2016')}"]
        ],
        colWidths=[95, 75, 100, 85, 110, 325]
    )
    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1B365D")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#EDF2F7")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E0")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.extend([kpi_table, Spacer(1, 12)])

    headers = ["Door ID", "Floor", "Rating", "Recorded Defect (with Page Ref)", "Required BS 8214 Remedial Works", "Proposed Material", "Priority", "Site Evidence Status"]
    table_data = [[Paragraph(h, head_style) for h in headers]]
    for d in doors:
        door_id_esc = escape(str(d.get("door_id", "Unknown")))
        floor_esc = escape(str(d.get("floor", "Unknown")))
        rating_esc = escape(str(d.get("rating", "Unknown")))
        defect_esc = escape(str(d.get("defect", "Unspecified defect")))
        remedial_esc = escape(str(d.get("remedial_action", "MANUAL REVIEW REQUIRED")))
        part_esc = escape(str(d.get("part_needed", "UNMAPPED")))
        priority_esc = escape(str(d.get("priority", "Medium (P2)")))
        qty_val = d.get("qty", 0)
        page_tag = escape(f" (p.{d.get('source_page')})") if d.get("source_page") else ""

        table_data.append([
            Paragraph(door_id_esc, cell_style),
            Paragraph(floor_esc, cell_style),
            Paragraph(rating_esc, cell_style),
            Paragraph(f"{defect_esc}{page_tag}", cell_style),
            Paragraph(remedial_esc, cell_style),
            Paragraph(f"{escape(str(qty_val))}x {part_esc}", cell_style),
            Paragraph(priority_esc, cell_style),
            Paragraph("Awaiting Carpenter<br/>Tally Photo Upload", cell_style),
        ])

    main_table = Table(table_data, colWidths=[55, 75, 45, 175, 195, 115, 60, 70], repeatRows=1)
    main_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2C5282")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7FAFC")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    elements.extend([
        main_table,
        Spacer(1, 12),
        Paragraph(
            f"<b>Compliance Scope Notice:</b> Stage 1 Pre-Work Remediation Schedule generated under {std_esc}. "
            "Final Golden Thread Closure Sign-Off requires independent competent-person verification of post-repair photographic evidence.",
            sub_style
        )
    ])
    doc.build(elements)
