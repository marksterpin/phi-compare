#!/usr/bin/env python3
"""
Cyber Adviser — Private Health Insurance feed harvester
========================================================

Mirrors the home-loan-feed2 pattern, but for private health insurance.

Source of truth: PrivateHealth.gov.au dataset on data.gov.au, published
monthly by the Private Health Insurance Ombudsman. Every registered insurer
is required by law to lodge a Private Health Information Statement (PHIS) for
every product, so this single dataset is the complete, authoritative market.

Licence: CC BY 3.0 AU  (attribute "PrivateHealth.gov.au / PHIO").

What this does
--------------
1. Hits the data.gov.au CKAN API to discover the *latest* monthly ZIP
   (resource GUIDs change every month, so we never hardcode a URL).
2. Downloads + unzips it.
3. Parses every product PHIS XML file.
4. Writes slim, app-ready JSON, SHARDED BY STATE (so the app only ever
   downloads the ~one state it needs), plus a manifest.

Zero third-party dependencies — stdlib only — so the GitHub Action needs
no pip install and can't break on a dependency bump.

Run:  python3 harvest.py  ->  writes ./data/*.json
"""

import json
import os
import re
import sys
import zipfile
import io
import datetime
import urllib.request
import xml.etree.ElementTree as ET

# data.gov.au CKAN package for the PrivateHealth.gov.au dataset
PACKAGE_ID = "8ab10b1f-6eac-423c-abc5-bbffc31b216c"
CKAN_API = f"https://data.gov.au/data/api/3/action/package_show?id={PACKAGE_ID}"

# The PHIS XML default namespace
NS = "{http://admin.privatehealth.gov.au/ws/Schemas}"

OUT_DIR = "data"

# Fund code -> display name. Best-effort starter map; the harvester will also
# try to read a fund/insurer listing from the ZIP if one is present and merge
# it in. Unknown codes fall back to the raw code so nothing is ever dropped.
FUND_NAMES = {
    "NIB": "nib", "BUP": "Bupa", "MBP": "Medibank", "MPL": "Medibank",
    "AHM": "ahm", "HCF": "HCF", "HBF": "HBF", "GMF": "GMHBA", "GMH": "GMHBA",
    "AUF": "Australian Unity", "DHF": "Defence Health", "TUH": "TUH",
    "QCH": "Queensland Country", "HIF": "HIF", "FRA": "Frank",
    "PWA": "Phoenix Health", "CBH": "CBHS", "TFH": "Teachers Health",
    "WFD": "Westfund", "PEO": "Peoplecare", "ONE": "onemedifund",
    "STL": "St.LukesHealth", "HEA": "Health Partners", "LHS": "Latrobe Health",
    "RTW": "Reserve Bank Health", "EMH": "emergency services health",
    "ACA": "ACA Health", "DOC": "Doctors' Health Fund", "NMW": "Nurses & Midwives",
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "cyber-adviser-phi/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": "cyber-adviser-phi/1.0"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.read()


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def resource_date(res):
    """Best-effort date for a resource, used to pick the newest ZIP."""
    name = (res.get("name") or "") + " " + (res.get("url") or "")
    m = re.search(r"(\d{1,2})[-\s]?([a-z]{3})[-\s]?(\d{2,4})", name.lower())
    if m:
        d, mon, y = int(m.group(1)), _MONTHS.get(m.group(2)), int(m.group(3))
        if mon:
            if y < 100:
                y += 2000
            try:
                return datetime.date(y, mon, d)
            except ValueError:
                pass
    # fall back to CKAN timestamps
    for k in ("last_modified", "created"):
        v = res.get(k)
        if v:
            try:
                return datetime.datetime.fromisoformat(v.replace("Z", "")).date()
            except ValueError:
                pass
    return datetime.date.min


def pick_latest_zip(pkg):
    resources = pkg["result"]["resources"]
    zips = [r for r in resources
            if (r.get("format", "").lower() == "zip")
            or (r.get("url", "").lower().endswith(".zip"))]
    if not zips:
        raise RuntimeError("No ZIP resources found in the dataset.")
    zips.sort(key=resource_date, reverse=True)
    latest = zips[0]
    return latest["url"], resource_date(latest)


def t(el):
    return el.text.strip() if el is not None and el.text else ""


def find(el, tag):
    return el.find(NS + tag)


def num(s):
    try:
        return round(float(s), 2)
    except (TypeError, ValueError):
        return None


def parse_product(root):
    """Map one PHIS <Product> element into a slim record. Returns None to skip."""
    if root.tag != NS + "Product":
        return None

    status = root.get("Status", "")
    prod_status = t(find(root, "ProductStatus"))   # Open / Closed
    if prod_status and prod_status.lower() != "open":
        return None  # only keep products on sale

    fund_code = t(find(root, "FundCode"))
    rec = {
        "id": root.get("ProductID", ""),
        "fundCode": fund_code,
        "fund": FUND_NAMES.get(fund_code, fund_code),
        "code": t(find(root, "TableCode")),
        "name": t(find(root, "Name")),
        "state": t(find(root, "State")),
        "scope": t(find(root, "Category")),        # Single/Couple/Family/SingleParent
        "type": t(find(root, "ProductType")),      # Hospital/GeneralHealth/Combined
        "mlsExempt": t(find(root, "MedicareLevySurchargeExempt")).lower() == "true",
        "premium": num(t(find(root, "PremiumNoRebate"))),   # gross monthly, pre-rebate
        "premiumRebated": num(t(find(root, "Premium"))),    # base-tier rebated
        "hospComponent": num(t(find(root, "PremiumHospitalComponent"))),
        "tier": "",
        "gapCover": False,
        "accommodation": "",
        "ambulance": "",
        "excessAdmission": None,
        "excessPolicy": None,
        "copay": "",
        "categories": {},   # clinical category -> Covered/Restricted/NotCovered
        "extras": {},       # extras service -> {covered, waiting, limit}
        "waiting": {},
        "features": "",
    }

    hosp = find(root, "HospitalCover")
    if hosp is not None:
        rec["gapCover"] = hosp.get("GapCoverProvided", "").lower() == "true"
        # Tier: modern feed carries Gold/Silver/Bronze/Basic. Historically this
        # lived in <ClassificationHospital>; some iterations add a dedicated tier
        # element. Capture whichever is present. CONFIRM against the current XSD.
        rec["tier"] = (t(find(hosp, "HospitalTier"))
                       or t(find(hosp, "ProductTier"))
                       or t(find(hosp, "ClassificationHospital")))
        rec["accommodation"] = t(find(hosp, "Accommodation"))
        rec["ambulance"] = t(find(hosp, "HospitalAmbulance"))
        rec["features"] = t(find(hosp, "OtherProductFeatures"))

        ms = find(hosp, "MedicalServices")
        if ms is not None:
            for s in ms.findall(NS + "MedicalService"):
                title = s.get("Title")
                if title:
                    rec["categories"][title] = s.get("Cover", "")

        ex = find(hosp, "Excesses")
        if ex is not None:
            rec["excessAdmission"] = num(t(find(ex, "ExcessPerAdmission")))
            rec["excessPolicy"] = num(t(find(ex, "ExcessPerPolicy")))

        cp = find(hosp, "CoPayments")
        if cp is not None:
            rec["copay"] = cp.get("CoPaymentType", "")

        wp = find(hosp, "WaitingPeriods")
        if wp is not None:
            for w in wp.findall(NS + "WaitingPeriod"):
                title = w.get("Title")
                if title:
                    rec["waiting"][title] = f"{t(w)} {w.get('Unit','')}".strip()

    gen = find(root, "GeneralHealthCover")
    if gen is not None:
        if not rec["tier"]:
            rec["tier"] = t(find(gen, "ClassificationGeneralHealth"))
        ghs = find(gen, "GeneralHealthServices")
        if ghs is not None:
            for s in ghs.findall(NS + "GeneralHealthService"):
                title = s.get("Title")
                if not title:
                    continue
                covered = s.get("Covered", "0") == "1"
                rec["extras"][title] = {
                    "covered": covered,
                    "waiting": t(find(s, "WaitingPeriod")),
                }
        # benefit limits -> attach per-service annual limit
        bl = find(gen, "BenefitLimits")
        if bl is not None:
            for lim in bl.findall(NS + "BenefitLimit"):
                title = lim.get("Title")
                per = find(lim, "LimitPerPerson")
                if title and per is not None and title in rec["extras"]:
                    rec["extras"][title]["limit"] = num(t(per))

    return rec


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    log("Discovering latest release via CKAN...")
    pkg = fetch_json(CKAN_API)
    url, date = pick_latest_zip(pkg)
    source_month = date.strftime("%Y-%m") if date != datetime.date.min else "unknown"
    log(f"Latest release: {source_month}  ({url})")

    log("Downloading ZIP...")
    raw = fetch_bytes(url)
    log(f"  {len(raw)//1024} KB")

    products = []
    skipped = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = z.namelist()
        # Optional: merge a fund/insurer listing if the ZIP ships one.
        for n in names:
            low = n.lower()
            if low.endswith(".xml") and ("fund" in low or "insurer" in low):
                try:
                    froot = ET.fromstring(z.read(n))
                    for f in froot.iter():
                        code = f.get("Code") or f.get("FundCode")
                        name = f.get("Name") or f.get("FundName")
                        if code and name:
                            FUND_NAMES[code] = name
                except ET.ParseError:
                    pass

        xml_files = [n for n in names if n.lower().endswith(".xml")]
        log(f"Parsing {len(xml_files)} XML files...")
        for n in xml_files:
            try:
                root = ET.fromstring(z.read(n))
            except ET.ParseError:
                skipped += 1
                continue
            rec = parse_product(root)
            if rec and rec.get("state"):
                products.append(rec)
            else:
                skipped += 1

    log(f"Parsed {len(products)} open products (skipped {skipped}).")

    # Shard by state AND cover type, so the app downloads only the slice it
    # needs (e.g. someone after hospital-only cover never fetches extras rows).
    # ProductType values: Hospital / Combined / GeneralHealth.
    by = {}
    for p in products:
        st, ty = p["state"], (p["type"] or "Other")
        by.setdefault(st, {}).setdefault(ty, []).append(p)

    generated = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    states_meta, files, written = {}, {}, []
    for st in sorted(by):
        type_counts = {}
        for ty in sorted(by[st]):
            items = by[st][ty]
            fn = f"phi-{st}-{ty}.json"
            files[f"{st}-{ty}"] = fn
            type_counts[ty] = len(items)
            payload = {"sourceMonth": source_month, "generated": generated,
                       "state": st, "type": ty, "products": items}
            with open(os.path.join(OUT_DIR, fn), "w") as f:
                json.dump(payload, f, separators=(",", ":"))
            written.append(fn)
        states_meta[st] = {"total": sum(type_counts.values()), "types": type_counts}

    manifest = {
        "source": "PrivateHealth.gov.au / Private Health Insurance Ombudsman",
        "licence": "CC BY 3.0 AU",
        "sourceMonth": source_month,
        "generated": generated,
        "total": len(products),
        "states": states_meta,   # { QLD: {total, types:{Hospital:n,...}}, ... }
        "files": files,          # { "QLD-Hospital": "phi-QLD-Hospital.json", ... }
    }
    with open(os.path.join(OUT_DIR, "manifest.json"), "w") as f:
        json.dump(manifest, f, separators=(",", ":"))

    log(f"Wrote manifest.json + {len(written)} shards across {len(states_meta)} states.")
    log(f"Done. Source month: {source_month}")


if __name__ == "__main__":
    main()
