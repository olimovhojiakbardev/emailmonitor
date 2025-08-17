import re, json, html
from urllib.parse import urlparse
import yaml  # <-- Import the YAML library

# --- CORRECTED PART: Load rules dynamically from the YAML file ---
try:
    with open("jbhunt_rules.yaml", "r") as f:
        cfg = yaml.safe_load(f)

    JBH_RULES = {
        "from_domains": tuple(cfg["jbhunt_detection"]["from_domains"]),
        "sender_names_contains": tuple(cfg["jbhunt_detection"]["sender_names_contains"]),
        "body_phrases": tuple(cfg["jbhunt_detection"]["body_phrases"]),
        "url_host_contains": tuple(cfg["jbhunt_detection"]["url_host_contains"]),
        "weights": {
            "from_domain": cfg["jbhunt_detection"]["evidence_weighting"]["from_domain_match"],
            "host": cfg["jbhunt_detection"]["evidence_weighting"]["host_contains_match"],
            "phrase": cfg["jbhunt_detection"]["evidence_weighting"]["phrase_match"],
            "sender_name": cfg["jbhunt_detection"]["evidence_weighting"]["sender_name_match"]
        },
        "threshold": cfg["jbhunt_detection"]["threshold"],
        "subject_patterns": cfg["jbhunt_subject_patterns"],
        "reply_rules": {
            "true_if": tuple(cfg["reply_heuristics"]["requires_reply_true_if"]),
            "false_if": tuple(cfg["reply_heuristics"]["requires_reply_false_if"]),
        },
        "footer_indicators": tuple(cfg["jbhunt_company_stamp"]["footer_indicators"]),
    }
except FileNotFoundError:
    print("FATAL: jbhunt_rules.yaml not found. Please ensure the file is in the same directory.")
    exit()
except KeyError as e:
    print(f"FATAL: Missing key in jbhunt_rules.yaml: {e}")
    exit()

def strip_html(text):
    if not isinstance(text, str): return ""
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    t = re.sub(r"(?is)<[^>]+>", " ", t)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()

def find_urls(text):
    if not isinstance(text, str): return []
    urls = re.findall(r'href=[\'"]([^\'"]+)[\'"]', text, flags=re.I)
    urls += re.findall(r'(https?://[^\s<>"\)\]]+)', text, flags=re.I)
    return list(dict.fromkeys(u.rstrip('.,);]') for u in urls))

def hosts(urls):
    hs = []
    for u in urls:
        try:
            h = urlparse(u).hostname
            if h: hs.append(h.lower())
        except: pass
    return hs

def score_jbhunt(email):
    score = 0
    from_field = email.get("from") or ""
    m = re.search(r'([A-Za-z0-9._%+\-]+)@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})', from_field)
    from_domain = m.group(2).lower() if m else None
    if from_domain and any(from_domain.endswith(d) for d in JBH_RULES["from_domains"]):
        score += JBH_RULES["weights"]["from_domain"]
    if any(s in from_field for s in JBH_RULES["sender_names_contains"]):
        score += JBH_RULES["weights"]["sender_name"]
    body = email.get("original_body") or ""
    url_hosts = hosts(find_urls(body))
    if any(any(tok in h for tok in JBH_RULES["url_host_contains"]) for h in url_hosts):
        score += JBH_RULES["weights"]["host"]
    text_body = strip_html(body).lower()
    if any(p.lower() in text_body for p in JBH_RULES["body_phrases"]):
        score += JBH_RULES["weights"]["phrase"]
    return score >= JBH_RULES["threshold"]

def subject_category(subject):
    s = (subject or "").lower()
    pats = JBH_RULES["subject_patterns"]
    for key in pats:
        if key=="ID_PATTERN": continue
        if re.search(pats[key], s): return key
    return "OTHER"

def extract_load_ids(subject):
    pat = re.compile(JBH_RULES["subject_patterns"]["ID_PATTERN"])
    return pat.findall(subject or "")

def stamp_position(text_body):
    t = (text_body or "").lower()
    idxs = [t.rfind(ind.lower()) for ind in JBH_RULES["footer_indicators"] if ind.lower() in t]
    if not idxs: return None
    pos = max(idxs); frac = pos / max(len(t),1)
    if frac < 0.2: return "header"
    if frac < 0.7: return "body"
    return "footer"

def classify(email):
    is_jbh = score_jbhunt(email)
    subj = email.get("subject") or ""
    cat = subject_category(subj)
    ids = extract_load_ids(subj)
    text_body = strip_html(email.get("original_body") or "")
    stamp_pos = stamp_position(text_body)
    needs = None
    reason = "No strong subject signal."
    if cat in JBH_RULES["reply_rules"]["true_if"]:
        needs = True; reason = f"Subject category '{cat}' indicates action is needed."
    elif cat in JBH_RULES["reply_rules"]["false_if"]:
        needs = False; reason = f"Subject category '{cat}' is informational."
    
    return {
        "is_jbhunt": is_jbh,
        "subject_category": cat,
        "load_ids": ids,
        "company_stamp_position": stamp_pos,
        "needs_response_pred": needs,
        "reason": reason
    }