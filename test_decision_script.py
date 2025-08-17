import json
from email_jbhunt_prep import classify
from pathlib import Path

emails = json.loads(Path("emails.json").read_text(encoding="utf-8"))
if isinstance(emails, dict) and "emails" in emails:
    emails = emails["emails"]
elif isinstance(emails, dict):
    emails = list(emails.values())

print("id, from, subject, is_jbhunt, subject_category, needs_response_pred")
for e in emails[:200]:
    res = classify(e)
    print(",".join([
        str(e.get("id","")),
        str(e.get("from","")).replace(","," "),
        str(e.get("subject","")).replace(","," "),
        str(res["is_jbhunt"]),
        str(res["subject_category"]),
        str(res["needs_response_pred"])
    ]))
