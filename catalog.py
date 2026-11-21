# UK Fire Door Remediation Parts Catalog (Standard UK Trade Supply Rates)

PARTS_CATALOG = {
    "DAMPER_600X300_60": {
        "code": "DAMPER_600X300_60",
        "description": "600x300mm 60-min Intumescent Fire Damper Grille (Steel Louvres)",
        "unit": "Unit",
        "unit_price_gbp": 145.00,
    },
    "DOORSET_FD60S_SGL": {
        "code": "DOORSET_FD60S_SGL",
        "description": "Certified FD60S Single Fire Doorset Replacement (926x2040mm Pre-Hung Assembly)",
        "unit": "Set",
        "unit_price_gbp": 780.00,
    },
    "DOORSET_FD60S_DBL": {
        "code": "DOORSET_FD60S_DBL",
        "description": "Bespoke FD60S Double Doorset Replacement (1050+1050x2100mm)",
        "unit": "Set",
        "unit_price_gbp": 1150.00,
    },
    "DOORSET_FD30S_SGL": {
        "code": "DOORSET_FD30S_SGL",
        "description": "Certified FD30S Single Fire Doorset Replacement (Pre-Hung Assembly)",
        "unit": "Set",
        "unit_price_gbp": 580.00,
    },
    "INTUM_STRIP_15X4_2100": {
        "code": "INTUM_STRIP_15X4_2100",
        "description": "15x4mm Plain Brown Intumescent Strip (2.1m Trade Length)",
        "unit": "Length (2.1m)",
        "unit_price_gbp": 7.35,
    },
    "INTUM_STRIP_20X4_2100": {
        "code": "INTUM_STRIP_20X4_2100",
        "description": "20x4mm FD60 Intumescent & Cold Smoke Brush Strip (2.1m Trade Length)",
        "unit": "Length (2.1m)",
        "unit_price_gbp": 10.10,
    },
    "INTUM_STRIP_15X4": {
        "code": "INTUM_STRIP_15X4",
        "description": "15x4mm Plain Brown Intumescent Strip (Per Metre)",
        "unit": "Metre",
        "unit_price_gbp": 3.50,
    },
    "INTUM_STRIP_20X4": {
        "code": "INTUM_STRIP_20X4",
        "description": "20x4mm FD60 Intumescent & Cold Smoke Brush Strip (Per Metre)",
        "unit": "Metre",
        "unit_price_gbp": 4.80,
    },
    "HINGE_PAD_1MM": {
        "code": "HINGE_PAD_1MM",
        "description": "1mm Graphite Intumescent Hinge Pad",
        "unit": "Pad",
        "unit_price_gbp": 1.25,
    },
    "HINGE_GRADE13_PAIR": {
        "code": "HINGE_GRADE13_PAIR",
        "description": "Grade 13 Fire-Rated Ball Bearing Butt Hinges (Pair)",
        "unit": "Pair",
        "unit_price_gbp": 14.50,
    },
    "DROP_SEAL_838": {
        "code": "DROP_SEAL_838",
        "description": "838mm Surface-Mounted Drop-Down Acoustic/Smoke Threshold Seal",
        "unit": "Unit",
        "unit_price_gbp": 42.00,
    },
    "DROP_SEAL_915": {
        "code": "DROP_SEAL_915",
        "description": "915mm Automatic Drop-Down Acoustic/Smoke Threshold Seal",
        "unit": "Unit",
        "unit_price_gbp": 48.00,
    },
    "DROP_SEAL_1000": {
        "code": "DROP_SEAL_1000",
        "description": "1000mm Heavy-Duty Drop-Down Acoustic/Smoke Threshold Seal",
        "unit": "Unit",
        "unit_price_gbp": 54.00,
    },
    "ASTRAGAL_SET_2100": {
        "code": "ASTRAGAL_SET_2100",
        "description": "2.1m Centre Meeting-Stile Intumescent & Smoke Astragal Seal Set",
        "unit": "Set",
        "unit_price_gbp": 38.00,
    },
    "SIGN_FD60_RIGID": {
        "code": "SIGN_FD60_RIGID",
        "description": "Rigid Photoluminescent FD60 Fire Door Keep Shut Sign",
        "unit": "Unit",
        "unit_price_gbp": 3.20,
    },
    "SIGN_FD30_RIGID": {
        "code": "SIGN_FD30_RIGID",
        "description": "Rigid Photoluminescent FD30 Fire Door Keep Shut Sign",
        "unit": "Unit",
        "unit_price_gbp": 3.20,
    },
    "SIGN_KEEP_LOCKED": {
        "code": "SIGN_KEEP_LOCKED",
        "description": "Rigid Photoluminescent Fire Door Keep Locked Shut Sign",
        "unit": "Unit",
        "unit_price_gbp": 3.20,
    },
    "SIGN_KEEP_CLEAR": {
        "code": "SIGN_KEEP_CLEAR",
        "description": "Rigid Photoluminescent Automatic Fire Door Keep Clear Sign",
        "unit": "Unit",
        "unit_price_gbp": 3.50,
    },
    "CLOSER_OH_EN3": {
        "code": "CLOSER_OH_EN3",
        "description": "Overhead Fire Door Closer EN 1154 Size 3-4 (CE/UKCA Marked)",
        "unit": "Unit",
        "unit_price_gbp": 68.00,
    },
    "LOCK_INTUM_JACKET": {
        "code": "LOCK_INTUM_JACKET",
        "description": "Intumescent Lock / Latch Kit Jacket",
        "unit": "Unit",
        "unit_price_gbp": 6.50,
    },
    "NO_PARTS_REQ": {
        "code": "NO_PARTS_REQ",
        "description": "No Parts Required",
        "unit": "None",
        "unit_price_gbp": 0.00,
    },
}


def catalog_prompt_summary() -> str:
    lines = []
    for code, item in PARTS_CATALOG.items():
        lines.append(
            f"- {code}: {item['description']} (Unit: {item['unit']}, £{item['unit_price_gbp']:.2f})"
        )
    return "\n".join(lines)
