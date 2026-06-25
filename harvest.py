#!/usr/bin/env python3
"""
Cyber Adviser - Private Health Insurance feed harvester
=======================================================

Source of truth: PrivateHealth.gov.au dataset on data.gov.au, published
~monthly by the Private Health Insurance Ombudsman. Every registered insurer
must lodge a Private Health Information Statement (PHIS) for every product, so
this single dataset is the complete, authoritative market.

Licence: CC BY 3.0 AU  (attribute "PrivateHealth.gov.au / PHIO").

Pipeline:
  1. CKAN API -> discover the latest monthly ZIP (resource GUIDs change monthly).
  2. Download + unzip.
  3. Parse every <Product> found, NAMESPACE-AGNOSTICALLY (matches by local tag
     name, so a changed schema URI can't silently zero the output).
  4. Write slim JSON sharded by state x cover type, plus a manifest.
  5. If nothing parses, write data/_diagnostics.txt showing the real structure
     so the parser can be corrected without guesswork.

Zero third-party dependencies (stdlib only).
Run:  python3 harvest.py  ->  ./data/*.json
"""

import json, os, re, sys, io, zipfile, datetime, urllib.request
import xml.etree.ElementTree as ET

PACKAGE_ID = "8ab10b1f-6eac-423c-abc5-bbffc31b216c"
CKAN_API = f"https://data.gov.au/data/api/3/action/package_show?id={PACKAGE_ID}"
OUT_DIR = "data"

# Fund code -> display name (best effort; unknown codes fall back to the code).
FUND_NAMES = {
    "NIB":"nib","BUP":"Bupa","MBP":"Medibank","MPL":"Medibank","AHM":"ahm",
    "HCF":"HCF","HBF":"HBF","GMF":"GMHBA","GMH":"GMHBA","AUF":"Australian Unity",
    "DHF":"Defence Health","TUH":"TUH","QCH":"Queensland Country","HIF":"HIF",
    "FRA":"Frank","PWA":"Phoenix Health","CBH":"CBHS","TFH":"Teachers Health",
    "WFD":"Westfund","PEO":"Peoplecare","ONE":"onemedifund","STL":"St.LukesHealth",
    "HEA":"Health Partners","LHS":"Latrobe Health","RTW":"Reserve Bank Health",
    "EMH":"Emergency Services Health","ACA":"ACA Health","DOC":"Doctors' Health Fund",
    "NMW":"Nurses & Midwives",
}

def log(*a): print(*a, file=sys.stderr, flush=True)

# ---- namespace-agnostic XML helpers -------------------------------------
def lname(tag):
    """Local tag name, stripping any {namespace} prefix."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else tag

def kids(el, name):
    return [c for c in list(el) if lname(c.tag) == name]

def kid(el, name):
    for c in list(el):
        if lname(c.tag) == name:
            return c
    return None

def ktext(el, name):
    c = kid(el, name)
    return (c.text or "").strip() if c is not None and c.text else ""

def num(s):
    try: return round(float(s), 2)
    except (TypeError, ValueError): return None

# ---- networking ----------------------------------------------------------
def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent":"cyber-adviser-phi/1.1"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))

def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent":"cyber-adviser-phi/1.1"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.read()

_MONTHS = {m:i for i,m in enumerate(
    ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"], 1)}

def resource_date(res):
    name = (res.get("name") or "") + " " + (res.get("url") or "")
    m = re.search(r"(\d{1,2})[-\s]?([a-z]{3})[-\s]?(\d{2,4})", name.lower())
    if m:
        d, mon, y = int(m.group(1)), _MONTHS.get(m.group(2)), int(m.group(3))
        if mon:
            if y < 100: y += 2000
            try: return datetime.date(y, mon, d)
            except ValueError: pass
    for k in ("last_modified","created"):
        v = res.get(k)
        if v:
            try: return datetime.datetime.fromisoformat(v.replace("Z","")).date()
            except ValueError: pass
    return datetime.date.min

def pick_latest_zip(pkg):
    res = pkg["result"]["resources"]
    zips = [r for r in res if r.get("format","").lower()=="zip"
            or r.get("url","").lower().endswith(".zip")]
    if not zips:
        raise RuntimeError("No ZIP resources in dataset.")
    zips.sort(key=resource_date, reverse=True)
    return zips[0]["url"], resource_date(zips[0])

# ---- parse one <Product> -------------------------------------------------
def parse_product(p):
    prod_status = ktext(p, "ProductStatus")
    if prod_status and prod_status.lower() == "closed":
        return None  # skip products no longer on sale

    fund_code = ktext(p, "FundCode")
    rec = {
        "id": p.get("ProductID") or ktext(p,"TableCode") or "",
        "fundCode": fund_code,
        "fund": FUND_NAMES.get(fund_code, fund_code or "Unknown"),
        "code": ktext(p, "TableCode"),
        "name": ktext(p, "Name"),
        "state": ktext(p, "State"),
        "scope": ktext(p, "Category"),
        "type": ktext(p, "ProductType"),
        "mlsExempt": ktext(p, "MedicareLevySurchargeExempt").lower() == "true",
        "premium": num(ktext(p, "PremiumNoRebate")) or num(ktext(p,"Premium")),
        "premiumRebated": num(ktext(p, "Premium")),
        "hospComponent": num(ktext(p, "PremiumHospitalComponent")),
        "tier": "", "gapCover": False, "accommodation": "", "ambulance": "",
        "excessAdmission": None, "excessPolicy": None, "copay": "",
        "categories": {}, "extras": {}, "waiting": {}, "features": "",
    }

    h = kid(p, "HospitalCover")
    if h is not None:
        rec["gapCover"] = h.get("GapCoverProvided","").lower() == "true"
        rec["tier"] = (ktext(h,"HospitalTier") or ktext(h,"ProductTier")
                       or ktext(h,"Tier") or ktext(h,"ClassificationHospital"))
        rec["accommodation"] = ktext(h, "Accommodation")
        rec["ambulance"] = ktext(h, "HospitalAmbulance")
        rec["features"] = ktext(h, "OtherProductFeatures")
        ms = kid(h, "MedicalServices")
        if ms is not None:
            for s in kids(ms, "MedicalService"):
                title = s.get("Title")
                if title: rec["categories"][title] = s.get("Cover","")
        ex = kid(h, "Excesses")
        if ex is not None:
            rec["excessAdmission"] = num(ktext(ex,"ExcessPerAdmission"))
            rec["excessPolicy"] = num(ktext(ex,"ExcessPerPolicy"))
        cp = kid(h, "CoPayments")
        if cp is not None: rec["copay"] = cp.get("CoPaymentType","")
        wp = kid(h, "WaitingPeriods")
        if wp is not None:
            for w in kids(wp, "WaitingPeriod"):
                title = w.get("Title")
                if title:
                    rec["waiting"][title] = f"{(w.text or '').strip()} {w.get('Unit','')}".strip()

    g = kid(p, "GeneralHealthCover")
    if g is not None:
        if not rec["tier"]:
            rec["tier"] = ktext(g, "ClassificationGeneralHealth")
        ghs = kid(g, "GeneralHealthServices")
        if ghs is not None:
            for s in kids(ghs, "GeneralHealthService"):
                title = s.get("Title")
                if not title: continue
                rec["extras"][title] = {"covered": s.get("Covered","0")=="1",
                                        "waiting": ktext(s,"WaitingPeriod")}
        bl = kid(g, "BenefitLimits")
        if bl is not None:
            for lim in kids(bl, "BenefitLimit"):
                title = lim.get("Title")
                per = kid(lim, "LimitPerPerson")
                if title and per is not None and title in rec["extras"]:
                    rec["extras"][title]["limit"] = num((per.text or "").strip())

    return rec

# ---- diagnostics when nothing parses ------------------------------------
def write_diagnostics(zf, names):
    lines = ["PHI harvester diagnostics", "=========================",
             f"Total entries in ZIP: {len(names)}", "",
             "First 40 entries:"]
    lines += ["  " + n for n in names[:40]]
    xmls = [n for n in names if n.lower().endswith(".xml")]
    lines += ["", f"XML files: {len(xmls)}"]
    if xmls:
        sample = xmls[0]
        lines += ["", f"--- structure of: {sample} ---"]
        try:
            root = ET.fromstring(zf.read(sample))
            lines.append(f"root tag (local): {lname(root.tag)}   raw: {root.tag}")
            lines.append("first-level children (local names):")
            for c in list(root)[:25]:
                lines.append(f"  - {lname(c.tag)}")
            # how many <Product> anywhere?
            cnt = sum(1 for e in root.iter() if lname(e.tag) == "Product")
            lines.append(f"<Product> elements in this file: {cnt}")
            raw = zf.read(sample).decode("utf-8", "replace")
            lines += ["", "--- first 1600 chars of raw XML ---", raw[:1600]]
        except Exception as e:
            lines.append(f"(could not parse sample: {e})")
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "_diagnostics.txt"), "w") as f:
        f.write("\n".join(lines))
    log("Wrote data/_diagnostics.txt (open it in the repo and share it).")

# ---- main ----------------------------------------------------------------
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

    products, prod_elems, xml_count, parse_errs = [], 0, 0, 0
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = z.namelist()
        xml_files = [n for n in names if n.lower().endswith(".xml")]
        log(f"ZIP has {len(names)} entries, {len(xml_files)} XML files.")
        for n in xml_files:
            xml_count += 1
            try:
                root = ET.fromstring(z.read(n))
            except ET.ParseError:
                parse_errs += 1
                continue
            # find every <Product> in the file (handles 1-per-file or many).
            elems = [root] if lname(root.tag) == "Product" else \
                    [e for e in root.iter() if lname(e.tag) == "Product"]
            for pe in elems:
                prod_elems += 1
                rec = parse_product(pe)
                if rec and rec.get("state") and rec.get("name"):
                    products.append(rec)

        log(f"Found {prod_elems} <Product> elements; kept {len(products)} "
            f"(parse errors: {parse_errs}).")

        if not products:
            write_diagnostics(z, names)

    # shard by state x type
    by = {}
    for p in products:
        by.setdefault(p["state"], {}).setdefault(p["type"] or "Other", []).append(p)

    generated = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    states_meta, files, written = {}, {}, []
    for st in sorted(by):
        counts = {}
        for ty in sorted(by[st]):
            items = by[st][ty]
            fn = f"phi-{st}-{ty}.json"
            files[f"{st}-{ty}"] = fn
            counts[ty] = len(items)
            with open(os.path.join(OUT_DIR, fn), "w") as f:
                json.dump({"sourceMonth":source_month,"generated":generated,
                           "state":st,"type":ty,"products":items},
                          f, separators=(",",":"))
            written.append(fn)
        states_meta[st] = {"total": sum(counts.values()), "types": counts}

    manifest = {
        "source":"PrivateHealth.gov.au / Private Health Insurance Ombudsman",
        "licence":"CC BY 3.0 AU", "sourceMonth":source_month, "generated":generated,
        "total":len(products), "states":states_meta, "files":files,
    }
    with open(os.path.join(OUT_DIR,"manifest.json"),"w") as f:
        json.dump(manifest, f, separators=(",",":"))

    if products:
        log(f"Wrote manifest.json + {len(written)} shards across {len(states_meta)} states.")
    else:
        log("WARNING: 0 products parsed. See data/_diagnostics.txt for the real "
            "XML structure, then adjust the element names in parse_product().")
    log(f"Done. Source month: {source_month}")

if __name__ == "__main__":
    main()
