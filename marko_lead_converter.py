import csv

INPUT_FILE = "results.csv"
OUTPUT_FILE = "marko_leads.csv"

ISSUE = "No online ordering found"
PERSONALIZATION_LINE = "Tried to place an order and couldn't find a way to do it"
MESSAGE = (
    "Hey — I tried to place an order from your site and couldn't find a way to do it. "
    "Not sure if that's intentional, but you might be losing people who don't want to call. "
    "I can fix that pretty quickly if you want."
)


def run():
    with open(INPUT_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    leads = [r for r in rows if r.get("status", "").strip() == "NO ORDERING PATH FOUND"]

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "website", "issue", "personalization_line", "message"])
        writer.writeheader()
        for r in leads:
            writer.writerow({
                "name": r["name"],
                "website": r["website"],
                "issue": ISSUE,
                "personalization_line": PERSONALIZATION_LINE,
                "message": MESSAGE,
            })

    print(f"Done. {len(leads)} lead(s) written to {OUTPUT_FILE}")


if __name__ == "__main__":
    run()
