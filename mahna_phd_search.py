"""
پایپ‌لاین جستجوی پوزیشن‌های PhD کاملاً بورسیه برای مهنا سلیمانی.
معماری: Tavily (جستجوی واقعی) -> فیلتر کلیدواژه (رایگان) -> Extract صفحه (رایگان با requests/BeautifulSoup،
با فallback به Tavily Extract) -> فیلترهای قطعی "بسته‌شده"/"محدودیت ملیت"/"ددلاین گذشته" -> یک فراخوانی
Gemini برای فرمت/تطبیق نهایی -> نوشتن در Google Sheets.

روی GitHub Actions اجرا می‌شود (بدون سقف زمانی سخت‌گیرانه‌ی Apps Script، با قابلیت تست محلی).
"""

import os
import re
import sys
import time
import json
import requests
from datetime import date
from dateutil import parser as date_parser

import gspread
from google.oauth2.service_account import Credentials
from bs4 import BeautifulSoup

# ==========================================
# تنظیمات از GitHub Secrets (environment variables)
# ==========================================
TAVILY_API_KEY = os.environ["TAVILY_API_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SHEET_ID = os.environ["SHEET_ID"]
SHEET_TAB_NAME = os.environ.get("SHEET_TAB_NAME", "Mahna - PhD Positions")
GCP_SERVICE_ACCOUNT_JSON = os.environ["GCP_SERVICE_ACCOUNT_JSON"]

# ==========================================
# پورتال‌ها — همان لیست تاییدشده‌ی مورد استفاده برای رضا (عمومی و مستقل از رشته).
# ==========================================
PORTALS = [
    # --- لیست قبلی ---
    "findaphd.com", "jobs.ac.uk", "academicpositions.com", "phdportal.com",
    "euraxess.ec.europa.eu", "daad.de", "jobbnorge.no", "academictransfer.com",
    "nature.com/naturecareers", "researchgate.net", "jobrxiv.org", "scholarshipdb.net",
    "phdfinder.com", "phdscanner.com", "gradschools.com", "thegradcafe.com",
    "scholarshippositions.com", "scholars4dev.com", "vacancyedu.com", "applykite.com",
    "academicjobsonline.org", "higheredjobs.com", "timeshighereducation.com/unijobs",
    "opportunitiesforafricans.com", "postdocjobs.com", "phds.org",
    # --- پورتال‌های جدید (دامنه تاییدشده) ---
    "phdhunter.org", "universitypositions.eu", "scholarshipsads.com", "inomics.com",
    "eures.europa.eu", "cordis.europa.eu", "mitacs.ca", "studyportals.com", "study.eu",
    "adum.fr", "abg.asso.fr", "varbi.com", "myscience.org", "research.fi",
    "research-in-germany.org", "funding-guide.de", "workindenmark.dk", "educanada.ca",
    "universityaffairs.ca", "scholarshipscanada.com", "marie-sklodowska-curie-actions.ec.europa.eu",
]

# ==========================================
# کوئری‌ها — سه کوئری فنی برای حوزه‌ی خوردگی/الکتروشیمی/مواد + یک کوئری ترکیبی
# برای بورسیه‌های عمومی/دانشکده‌ای و ملیت/کشور-مبدا‌محور (مشابه ساختار رضا).
# ==========================================
SEARCH_QUERIES = [
    "fully funded PhD position corrosion inhibitors materials engineering",
    "PhD scholarship electrochemistry corrosion protection of metals",
    "PhD studentship pipeline corrosion oil and gas top of line corrosion",
    "PhD scholarship faculty of engineering open call self-proposed research topic "
    "international students Iran developing countries fully funded",
]

KEYWORDS = [
    "corrosion", "corrosion inhibitor", "electrochemistry", "electrochemical",
    "top of line corrosion", "top-of-line corrosion", "co2 corrosion", "pipeline corrosion",
    "oil and gas corrosion", "surface engineering", "coating", "cathodic protection",
    "anodic protection", "materials characterization", "materials science", "metallography",
    "sem", "eds", "ftir", "additive manufacturing", "failure analysis",
    "phd", "doctoral", "fully funded",
    # برای پوشش بورسیه‌های عمومی/دانشکده‌ای و ملیت‌محور که کلمات فنی بالا را ندارند
    "scholarship", "faculty of engineering", "school of engineering", "graduate school",
    "international students", "any discipline", "open topic", "doctoral training",
    "self-funded", "propose your own", "developing countries",
]

CLOSED_PHRASES = [
    "position has been filled", "position is filled", "no longer accepting applications",
    "no longer available", "vacancy closed", "vacancy has closed", "applications are now closed",
    "applications closed", "this position is closed", "recruitment closed", "posting has expired",
    "this vacancy has expired", "job has expired", "advert has expired", "advertisement has expired",
]

NATIONALITY_RESTRICTION_PHRASES = [
    "uk home applicants only", "uk home students only", "home fee status only",
    "uk nationals only", "eligible uk students", "uk/eu students only",
    "home students only", "restricted to uk", "eu nationals only",
    "us citizens only", "us citizenship required", "must be a us citizen",
]

MAX_EXTRACT = 45
GEMINI_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash-lite"]


def tavily_search(domain: str, query: str) -> list[dict]:
    resp = requests.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
        json={
            "query": query,
            "search_depth": "basic",
            "max_results": 15,
            "include_domains": [domain],
        },
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"[Tavily] HTTP {resp.status_code} برای {domain}: {resp.text[:300]}")
        return []
    return resp.json().get("results", [])


def free_extract(url: str) -> str | None:
    try:
        resp = requests.get(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; PhDSearchBot/1.0)"}, timeout=15,
        )
        if resp.status_code != 200 or not resp.text:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ").split())
        return text if len(text) > 200 else None
    except Exception as e:
        print(f"[Free Extract] شکست برای {url}: {e}")
        return None


def tavily_extract_fallback(url: str) -> str | None:
    try:
        resp = requests.post(
            "https://api.tavily.com/extract",
            headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
            json={"urls": url, "extract_depth": "basic"},
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"[Tavily Extract] HTTP {resp.status_code} برای {url}: {resp.text[:300]}")
            return None
        results = resp.json().get("results", [])
        return results[0]["raw_content"] if results else None
    except Exception as e:
        print(f"[Tavily Extract] خطا برای {url}: {e}")
        return None


def contains_closed_phrase(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in CLOSED_PHRASES)


def contains_nationality_restriction(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in NATIONALITY_RESTRICTION_PHRASES)


def call_gemini(prompt: str) -> str | None:
    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
        payload = {"contents": [{"parts": [{"text": prompt}]}]}

        for attempt in range(2):
            try:
                resp = requests.post(url, json=payload, timeout=120)
            except requests.RequestException as e:
                print(f"[{model}] خطای شبکه: {e}")
                time.sleep(5)
                continue

            if resp.status_code == 200:
                data = resp.json()
                parts = data["candidates"][0]["content"]["parts"]
                return "".join(p.get("text", "") for p in parts)

            print(f"[{model}] HTTP {resp.status_code} — تلاش {attempt + 1}/2: {resp.text[:300]}")
            if resp.status_code == 429:
                time.sleep(15 * (attempt + 1))
            elif resp.status_code >= 500:
                time.sleep(8 * (attempt + 1))
            else:
                break

        print(f"مدل {model} جواب نداد — سوییچ به مدل بعدی (در صورت وجود)...")

    return None


def build_prompt(results: list[dict], today_str: str) -> str:
    blocks = []
    for i, r in enumerate(results, 1):
        kind = "Full page text (truncated)" if r.get("extracted") else "Search snippet"
        blocks.append(f"[{i}] Portal: {r['portal']}\nTitle: {r['title']}\nURL: {r['url']}\n{kind}: {r['content']}")
    results_text = "\n\n".join(blocks)

    return f"""You are given {len(results)} raw web search results below (already fetched from real portals — do not invent new ones). Extract and structure ONLY the results that are genuine, open, fully-funded PhD positions or PhD scholarship programs (outside the USA) matching this candidate:

CANDIDATE PROFILE (Mahna Soleymani):
- M.Sc. Materials Science and Engineering – Corrosion and Protection of Materials, Shiraz University (GPA 3.62/4.00, Ranked 1st of 10); B.Sc. Materials Science and Engineering, Yasuj University (Ranked 1st of 51)
- Thesis: synergistic inhibition of monoethylene glycol and corrosion inhibitors on Top-of-Line Corrosion (TLC). Additional research: ionic acidity buildup in glycol cycles, epoxy coating performance on carbon steel in saline wastewater, chemical treatment interactions in desalting units, internal corrosion prevention in firefighting water metal rings.
- One submitted paper (Top-of-Line Corrosion of API 5L X65 pipeline steel), two in-progress papers (TLC inhibitor synergy; failure analysis of corrosion damage and welded joints in slug catcher elbows).
- Lab/technical skills: metallography, electrochemical testing (Autolab, IviumStat), FTIR analysis, optical microscopy, SEM/EDS, quantitative image analysis (ImageJ), ZView, OriginLab, Design-Expert, basic ML (supervised/unsupervised, classification).
- English C1 (IELTS pending, next three months).
- Match on ANY (OR logic) of: corrosion engineering/science, electrochemistry, corrosion inhibitors, CO2 corrosion, top-of-line corrosion, pipeline/oil & gas corrosion, surface engineering/coatings, cathodic/anodic protection, materials characterization, failure analysis, additive manufacturing; OR lab/technical skills listed above.

TWO VALID TYPES OF RESULTS — include BOTH:
A) SPECIFIC positions: a named project/supervisor/topic that clearly matches the profile above.
B) GENERAL / "blanket" funding calls: a scholarship open to an entire faculty, school, department, or university (e.g. "Faculty of Engineering PhD Scholarships", "Graduate School doctoral fellowships, all disciplines", nationality/country-of-origin-based scholarships such as "scholarships for Iranian/international/developing-country students") where no single fixed topic or supervisor is named, but the funding covers engineering/materials science broadly enough that the candidate could plausibly apply with her own proposal. For type B results, set TopicSupervisor to something like "Open call — no fixed topic/supervisor (applicant proposes own project)" and note in Fit that it is a general/faculty-wide or nationality-based call rather than a specific project.

RULES:
1. Only use information present in the search results below — do NOT fabricate deadlines, links, or details not present in the text.
2. If a deadline is mentioned and it is clearly before {today_str}, EXCLUDE that result.
3. If no deadline is visible, include the position but set Deadline to "Not specified — check listing".
4. Use the exact URL given for each result, never modify or guess a URL.
5. The candidate is an international (Iranian) applicant, NOT a UK/EU/US national or resident. If the funding is explicitly restricted to "UK Home students", "UK/EU nationals", "US citizens", or similar residency/nationality-restricted funding that would exclude an international applicant, EXCLUDE that result. Conversely, a scholarship explicitly for "international students", "developing countries", or naming Iran/Middle East is a positive signal, not a reason to exclude.
6. Return STRICTLY a raw JSON array (no markdown fences, no extra text) with exact keys: University, Country, ProgramDept, TopicSupervisor, FundingType, Deadline, Fit, Link.

SEARCH RESULTS:
{results_text}"""


def normalize_url(url: str) -> str:
    return re.sub(r"/$", "", url.lower().split("?")[0].replace("https://", "").replace("http://", "")).strip()


def composite_key(uni: str, topic: str) -> str:
    return re.sub(r"[^a-z0-9]", "", f"{uni}_{topic}".lower())


def deadline_clearly_passed(deadline_text: str, today: date) -> bool:
    if not deadline_text:
        return False
    try:
        parsed = date_parser.parse(deadline_text, fuzzy=True, default=None)
        if parsed is None:
            return False
        return parsed.date() < today
    except (ValueError, OverflowError):
        return False


def get_sheet():
    creds_dict = json.loads(GCP_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(SHEET_TAB_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_TAB_NAME, rows=1000, cols=12)
        ws.append_row([
            "Candidate", "University", "Country", "Program/Dept", "Topic/Supervisor",
            "Funding Type", "Deadline", "Fit", "Link", "Date Added", "Status", "Link Verified",
        ])
    return ws


def link_alive(url: str) -> tuple[bool, str]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; PhDSearchBot/1.0)"}
    try:
        r = requests.head(url, timeout=10, allow_redirects=True, headers=headers)
        if r.status_code in (403, 405) or r.status_code >= 500:
            r = requests.get(url, timeout=15, allow_redirects=True, headers=headers, stream=True)
        if r.status_code >= 400:
            return False, ""
    except Exception:
        return False, ""

    try:
        full = requests.get(url, timeout=15, headers=headers)
        text = full.text[:20000]
        if contains_closed_phrase(BeautifulSoup(text, "html.parser").get_text(separator=" ")):
            return False, ""
        return True, text
    except Exception:
        return True, ""


def main():
    today_str = date.today().isoformat()
    ws = get_sheet()

    existing_rows = ws.get_all_values()[1:]
    existing_keys = set()
    for row in existing_rows:
        if len(row) < 9:
            continue
        uni, topic, link = row[1], row[4], row[8]
        if link:
            existing_keys.add(normalize_url(link))
        if uni and topic:
            existing_keys.add(composite_key(uni, topic))

    all_results = []
    for portal in PORTALS:
        for query in SEARCH_QUERIES:
            for r in tavily_search(portal, query):
                all_results.append({"portal": portal, "title": r.get("title", ""),
                                     "url": r.get("url", ""), "content": r.get("content", "")})
            time.sleep(0.3)

    seen_urls = set()
    deduped = []
    for r in all_results:
        key = normalize_url(r["url"])
        if key and key not in seen_urls:
            seen_urls.add(key)
            deduped.append(r)
    all_results = deduped

    print(f"Tavily: مجموعاً {len(all_results)} نتیجه‌ی خام و غیرتکراری از {len(PORTALS)} پورتال × {len(SEARCH_QUERIES)} کوئری.")
    if not all_results:
        sys.exit("هیچ نتیجه‌ای از Tavily برنگشت — کلید یا شبکه را بررسی کن.")

    scored = []
    for r in all_results:
        text = f"{r['title']} {r['content']}".lower()
        score = sum(1 for k in KEYWORDS if k in text)
        if score > 0:
            scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    to_extract = [r for _, r in scored[:MAX_EXTRACT]]
    print(f"بعد از فیلتر کلیدواژه: {len(scored)} نتیجه‌ی مرتبط؛ {len(to_extract)} تا برای Extract انتخاب شد.")

    dropped_closed = 0
    dropped_restricted = 0
    for r in to_extract:
        text = free_extract(r["url"]) or tavily_extract_fallback(r["url"])
        if text:
            if contains_closed_phrase(text):
                r["closed"] = True
                dropped_closed += 1
                continue
            if contains_nationality_restriction(text):
                r["closed"] = True
                dropped_restricted += 1
                continue
            r["content"] = text[:2000]
            r["extracted"] = True
        time.sleep(0.3)

    final_results = [r for r in to_extract if not r.get("closed")]
    print(f"{dropped_closed} پوزیشن به‌خاطر عبارت صریح «بسته‌شده» حذف شد؛ {dropped_restricted} پوزیشن به‌خاطر محدودیت ملیت/اقامت حذف شد.")

    prompt = build_prompt(final_results, today_str)
    raw_output = call_gemini(prompt)
    if not raw_output:
        sys.exit("هر دو مدل Gemini شکست خوردند — GitHub خودش ایمیل شکست را می‌فرستد.")

    cleaned = raw_output.replace("```json", "").replace("```", "").strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end == -1:
        sys.exit(f"خروجی Gemini قابل تبدیل به JSON نبود:\n{raw_output[:1000]}")

    try:
        positions = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as e:
        sys.exit(f"خطای پارس JSON: {e}\nخروجی خام:\n{raw_output[:1000]}")

    new_rows = []
    dropped_dead_link = 0
    dropped_expired_date = 0
    today_date = date.today()
    for p in positions:
        link = p.get("Link", "")
        clean_link = normalize_url(link)
        comp_key = composite_key(p.get("University", ""), p.get("TopicSupervisor", ""))
        if (clean_link and clean_link in existing_keys) or (comp_key and comp_key in existing_keys):
            continue

        if not link:
            continue

        if deadline_clearly_passed(p.get("Deadline", ""), today_date):
            dropped_expired_date += 1
            print(f"ددلاین گذشته (چک قطعی پایتون): {p.get('Deadline')} — {link}")
            continue

        is_alive, _ = link_alive(link)
        if not is_alive:
            dropped_dead_link += 1
            print(f"لینک رد شد (مرده یا صراحتاً بسته‌شده در بررسی نهایی): {link}")
            continue

        new_rows.append([
            "Mahna", p.get("University", ""), p.get("Country", ""), p.get("ProgramDept", ""),
            p.get("TopicSupervisor", ""), p.get("FundingType", ""), p.get("Deadline", ""),
            p.get("Fit", ""), link, today_str, "Not yet applied", "Yes",
        ])
        if clean_link:
            existing_keys.add(clean_link)
        if comp_key:
            existing_keys.add(comp_key)

    print(f"{dropped_dead_link} پوزیشن به‌خاطر لینک مرده/بسته در بررسی نهایی حذف شد؛ {dropped_expired_date} پوزیشن به‌خاطر ددلاین گذشته (چک قطعی) حذف شد.")

    if new_rows:
        ws.append_rows(new_rows)
        print(f"[Mahna] {len(new_rows)} پوزیشن جدید اضافه شد.")
    else:
        print("[Mahna] پوزیشن جدیدی یافت نشد.")


if __name__ == "__main__":
    main()
