# UK Fire Door Remediation Parts Catalog (GUIDE PRICES)

PARTS_CATALOG = {
    "DAMPER_600X300_60": {
        "code": "DAMPER_600X300_60",
        "description": "600x300mm 60-min Intumescent Fire Damper Grille & Signs [GUIDE PRICE]",
        "unit": "Unit",
        "unit_price_gbp": 145.00,
    },
    "DOORSET_FD60S_DBL": {
        "code": "DOORSET_FD60S_DBL",
        "description": "Bespoke FD60S Double Doorset Replacement (1050+1050x2100mm) [GUIDE PRICE]",
        "unit": "Set",
        "unit_price_gbp": 1150.00,
    },
    "DOORSET_FD60S_SGL": {
        "code": "DOORSET_FD60S_SGL",
        "description": "Bespoke Certified FD60S Single Doorset (926x2040mm Factory-Hung) [GUIDE PRICE]",
        "unit": "Set",
        "unit_price_gbp": 850.00,
    },
    "DOORSET_FD30S_SGL": {
        "code": "DOORSET_FD30S_SGL",
        "description": "FD30S Single Fire Doorset Replacement [GUIDE PRICE]",
        "unit": "Set",
        "unit_price_gbp": 580.00,
    },
    "INTUM_STRIP_15X4": {
        "code": "INTUM_STRIP_15X4",
        "description": "15x4mm Plain Brown Intumescent Strip & Hinge Pads Pack [GUIDE PRICE]",
        "unit": "Pack",
        "unit_price_gbp": 18.00,
    },
    "INTUM_STRIP_20X4": {
        "code": "INTUM_STRIP_20X4",
        "description": "20x4mm FD60 Intumescent & Cold Smoke Brush Strip [GUIDE PRICE]",
        "unit": "Metre",
        "unit_price_gbp": 4.80,
    },
    "HINGE_PAD_1MM": {
        "code": "HINGE_PAD_1MM",
        "description": "1mm Intumescent Hinge Pad [GUIDE PRICE]",
        "unit": "Pad",
        "unit_price_gbp": 1.25,
    },
    "DROP_SEAL_915": {
        "code": "DROP_SEAL_915",
        "description": "915mm Automatic Drop-Down Smoke Threshold Seal & Hinge Pads [GUIDE PRICE]",
        "unit": "Set",
        "unit_price_gbp": 48.00,
    },
    "DROP_SEAL_838": {
        "code": "DROP_SEAL_838",
        "description": "838mm Surface-Mounted Drop-Down Acoustic/Smoke Threshold Seal [GUIDE PRICE]",
        "unit": "Unit",
        "unit_price_gbp": 42.00,
    },
    "ASTRAGAL_PAIR_SET": {
        "code": "ASTRAGAL_PAIR_SET",
        "description": "2.1m Meeting-Stile Astragal & 2x 838mm Drop-Down Threshold Seals [GUIDE PRICE]",
        "unit": "Pair Set",
        "unit_price_gbp": 74.00,
    },
    "SIGN_FD60_RIGID": {
        "code": "SIGN_FD60_RIGID",
        "description": "Rigid Photoluminescent FD60 Fire Door Sign [GUIDE PRICE]",
        "unit": "Unit",
        "unit_price_gbp": 3.20,
    },
    "SIGN_FD30_RIGID": {
        "code": "SIGN_FD30_RIGID",
        "description": "Rigid Photoluminescent FD30 Fire Door Sign [GUIDE PRICE]",
        "unit": "Unit",
        "unit_price_gbp": 3.20,
    },
    "CLOSER_OH_EN3": {
        "code": "CLOSER_OH_EN3",
        "description": "Overhead Fire Door Closer EN3-4 (CE/UKCA Marked) [GUIDE PRICE]",
        "unit": "Unit",
        "unit_price_gbp": 68.00,
    },
    "LOCK_INTUM_JACKET": {
        "code": "LOCK_INTUM_JACKET",
        "description": "Intumescent Lock / Latch Kit Jacket [GUIDE PRICE]",
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
    return "\n".join(
        f"- {code}: {item['description']} (Unit: {item['unit']})"
        for code, item in PARTS_CATALOG.items()
)
      
