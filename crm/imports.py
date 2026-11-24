import csv
import hashlib
import io
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from openpyxl import load_workbook

from .models import Activity, Creator, Deal, ImportBatch, Profile, Quote, SourceRow
from .services import audit, social_url, phone_number
from .views import User, allowed, config, csv_response, csv_value, page

MASTER = "Master Database"
OUTREACH = {"Outreach Pipeline", "TOP KOL Confirmation", "INFO"}
HISTORY = {"Daily Update Tracker", "Weekly Friday Report"}
DERIVED = {"Dashboard", "KOL Negotiation Rates", "Rate Card Comparison", "Confirmed KOL Rate Card"}
WIDTHS = {MASTER: 23, "Outreach Pipeline": 20, "TOP KOL Confirmation": 21, "INFO": 18, "Daily Update Tracker": 11,
          "Weekly Friday Report": 17, "Setup": 8, "WhatsApp Test": 2, "Dashboard": 16, "KOL Negotiation Rates": 12,
          "Rate Card Comparison": 25, "Confirmed KOL Rate Card": 83}
STAGES = {"confirm": "Confirmed / Upcoming", "confirmed": "Confirmed / Upcoming", "rejected": "Declined", "reject": "Declined",
          "replied": "Replied", "interested": "Potential Collaboration", "contacted": "Contacted", "negotiating": "Negotiating",
          "not contacted": "New", "follow up": "Contacted", "follow-up": "Contacted"}

def clean(value):
    return str(value if value is not None else "").strip()

def normalized(value):
    return " ".join(clean(value).casefold().split())

def platform(value):
    name = normalized(value).replace(" ", "")
    return {"fb": "Facebook", "facebook": "Facebook", "tiktok": "TikTok", "ig": "Instagram", "instagram": "Instagram", "youtube": "YouTube"}.get(name, clean(value)[:80])

def numeric(value):
    if value is None or clean(value) in ("", "-", "—"):
        return None
    raw = re.sub(r"^(?:BDT\.?|RM\.?|৳)\s*", "", clean(value), flags=re.I).replace(",", "").replace(" ", "")
    percent = raw.endswith("%")
    raw = raw.removesuffix("%")
    if re.fullmatch(r"\d{1,3}(\.\d{3})+\.\d{2}", raw):
        raw = raw.replace(".", "", raw.count(".") - 1)
    try:
        result = Decimal(raw)
        return (result / 100 if percent else result) if result.is_finite() and 0 <= result < Decimal("10000000000000") else None
    except InvalidOperation:
        return None

def dated(value):
    if not value:
        return None
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
    return timezone.make_aware(result) if timezone.is_naive(result) else result

def serial(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)

def scan(content):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if sum(z.file_size for z in archive.infolist()) > 250 * 1024 * 1024:
            raise ValueError("The expanded workbook exceeds the import limit.")
    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
    cached = load_workbook(io.BytesIO(content), read_only=True, data_only=True, keep_links=False)
    try:
        for sheet in workbook:
            width = WIDTHS.get(sheet.title, 40)
            start = 2 if sheet.title in {MASTER, "TOP KOL Confirmation", "INFO"} else 3 if sheet.title in HISTORY | {"Outreach Pipeline", "Setup", "KOL Negotiation Rates", "Rate Card Comparison"} else 1
            formulas = sheet.iter_rows(min_row=start, max_col=width)
            values = cached[sheet.title].iter_rows(min_row=start, max_col=width)
            for number, (cells, outputs) in enumerate(zip(formulas, values), start):
                if number > 50000:
                    raise ValueError("Workbook exceeds the row limit.")
                inputs = [serial(c.value) if c.data_type != "f" else None for c in cells]
                errors = {str(i + 1): serial(c.value) for i, c in enumerate(cells) if c.data_type == "e"}
                computed = {str(i + 1): serial(o.value) for i, (c, o) in enumerate(zip(cells, outputs)) if c.data_type == "f" and o.value is not None}
                if sheet.title in DERIVED:
                    # Keep the visible rows and any confirmed-rate-card source/helper values.
                    if sheet.title != "Confirmed KOL Rate Card" and not any(x is not None and x != "" for x in inputs):
                        continue
                    if sheet.title == "Confirmed KOL Rate Card" and not any(str(i) in computed for i in (2, 28, 59)) and not any(x is not None and x != "" for x in inputs):
                        continue
                elif not any(x is not None and x != "" for x in inputs):
                    continue
                result = "Derived" if sheet.title in DERIVED else "Historical" if sheet.title in HISTORY else "Configuration" if sheet.title in ("Setup", "WhatsApp Test") else "Pending review"
                yield SourceRow(sheet=sheet.title, number=number, raw={"cells": inputs, "cached": computed, "errors": errors}, result=result)
    finally:
        workbook.close()
        cached.close()

def creator_data(row, users):
    x = row.raw["cells"]
    staff = clean(x[1])
    user = users.get(normalized(staff))
    return {"name": clean(x[3])[:240], "niche": clean(x[5])[:240], "country": clean(x[6])[:140],
            "owner": user, "source_staff": staff[:150], "created": dated(x[0]) or row.batch.created,
            "stage": stage(x[15]) or "New", "phone": clean(x[16])[:150],
            "payment_remarks": clean(x[20]), "notes": clean(x[21])}

def stage(value):
    return STAGES.get(re.sub(r"[^a-z -]", "", normalized(value)).strip())

def identity(row):
    a = row.raw["cells"]
    if row.sheet == MASTER:
        name, pl, link = a[3], a[2], a[4]
    elif row.sheet == "TOP KOL Confirmation":
        name, pl, link = a[0], a[2], a[1]
    else:
        name, pl, link = a[0], a[1], None
    return clean(name), platform(pl), clean(link)

def name_available(row):
    return bool(identity(row)[0])

def match_profile(row, by_url, by_name, by_phone=None, phone=None):
    name, pl, link = identity(row)
    key, ref, problem = None, False, ""
    if link:
        try:
            detected, key, ref, link = social_url(link)
            pl = pl or detected
        except ValueError:
            problem = "Invalid profile link."
    if key and key in by_url:
        return by_url[key], pl, key, ref, link, problem
    if phone and by_phone:
        matches = by_phone.get(phone, [])
        if len(matches) == 1 and (not key or not matches[0].canonical or matches[0].canonical == key):
            return matches[0], pl, key, ref, link, problem
        if len(matches) > 1 or matches and key:
            return None, pl, key, ref, link, "Multiple possible matches or conflicting profile link."
    matches = by_name.get((normalized(name), normalized(pl)), [])
    if len(matches) == 1 and (not key or not matches[0].canonical or matches[0].canonical == key):
        return matches[0], pl, key, ref, link, problem
    if len(matches) > 1 or matches and key:
        return None, pl, key, ref, link, "Multiple possible matches or conflicting profile link."
    if not name and not key:
        problem = "Missing creator identity."
    if ref and row.sheet == MASTER:
        problem = "Reference link needs a creator profile URL."
    return None, pl, key, ref, link, problem

def reconcile(batch, commit=False):
    by_url, by_name, by_phone = {}, {}, {}
    for item in Profile.objects.select_related("creator"):
        if item.canonical:
            by_url[item.canonical] = item
        by_name.setdefault((normalized(item.creator.name), normalized(item.platform)), []).append(item)
        number = phone_number(item.creator.phone) if item.creator.phone else None
        if number:
            by_phone.setdefault(number, []).append(item)
    users = {normalized(u.username): u for u in User.objects.filter(is_active=True, member__role="KOL Staff")}
    users.update({normalized(u.get_full_name()): u for u in users.values() if u.get_full_name()})
    settings = config()
    rows = list(batch.rows.order_by("pk"))
    rows.sort(key=lambda row: 0 if row.sheet == "Setup" else 1 if row.sheet == MASTER else 2)
    for row in rows:
        if row.sheet == "Setup":
            if commit:
                for i, value in enumerate(row.raw["cells"]):
                    if value:
                        entries = settings.lists.setdefault(str(i + 1), [])
                        if clean(value) not in entries:
                            entries.append(clean(value))
                settings.save(update_fields=["lists"])
            continue
        if row.sheet == "WhatsApp Test":
            if commit and row.number == 2 and not settings.whatsapp_template and row.raw["cells"][1]:
                settings.whatsapp_template = clean(row.raw["cells"][1])
                settings.save(update_fields=["whatsapp_template"])
            continue
        if row.sheet not in OUTREACH | {MASTER}:
            continue
        row.records, row.reason = {}, ""
        phone = phone_number(clean(row.raw["cells"][16])) if row.sheet == MASTER else None
        matched, pl, key, ref, link, problem = match_profile(row, by_url, by_name, by_phone, phone)
        if row.sheet == MASTER and matched and matched.pk is None:
            row.reason = "Duplicate of an earlier row in this file."
        row.result = "Matched" if matched else "New"
        if problem or (not matched and (row.sheet != MASTER or not pl)):
            row.result = "Pending review"
            row.reason = problem or "No unique master profile found."
            if row.sheet == MASTER and not (ref and name_available(row) and pl) or not matched and row.sheet != MASTER:
                continue
        if row.sheet == MASTER:
            a = row.raw["cells"]
            data = creator_data(row, users)
            field_map = {"bd_audience": a[7], "followers": a[8], "views": a[9], "likes": a[10], "comments": a[11], "shares": a[12]}
            metrics = {name: numeric(value) for name, value in field_map.items()}
            flags = [name for name, value in field_map.items() if clean(value) and metrics[name] is None and normalized(value) not in ("not provided", "-", "n/a", "—")]
            if metrics["bd_audience"] is not None and metrics["bd_audience"] > 1:
                metrics["bd_audience"] = metrics["bd_audience"] / 100 if metrics["bd_audience"] <= 100 else None
            if metrics["followers"] is not None:
                if metrics["followers"] % 1:
                    flags.append("followers")
                    metrics["followers"] = None
                else:
                    metrics["followers"] = int(metrics["followers"])
            if str(a[16] or "").strip() and not phone_number(clean(a[16])) and normalized(a[16]) not in ("n/a", "not provided", "no contact", "-"):
                flags.append("phone")
            if a[0] and not dated(a[0]):
                flags.append("date added")
            if matched:
                differences = [field for field, value in metrics.items() if value is not None and getattr(matched, field) != value]
                differences += [field for field in ("name", "niche", "country", "phone", "notes", "payment_remarks") if data[field] and data[field] != getattr(matched.creator, field)]
                if link and key and key != matched.canonical:
                    differences.append("profile link")
                if differences:
                    flags.append("conflicts with CRM: " + ", ".join(differences))
            else:
                obj = Creator(**data)
                if commit:
                    obj.save()
                matched = Profile(creator=obj, platform=pl, canonical=key, reference=ref, url=link, **metrics)
                if commit:
                    matched.save()
                    row.records = {"creator": obj.pk, "profile": matched.pk, "baseline": obj.updated.isoformat()}
                if key:
                    by_url[key] = matched
                by_name.setdefault((normalized(obj.name), normalized(pl)), []).append(matched)
                if phone:
                    by_phone.setdefault(phone, []).append(matched)
            contact_date = dated(a[17])
            if a[17] and not contact_date:
                flags.append("date contacted")
            if commit and contact_date and matched.pk:
                activity, created = Activity.objects.get_or_create(
                    profile=matched, created=contact_date, channel="Imported",
                    defaults={"actor": data["owner"], "outcome": "Historical outreach"},
                )
                if created:
                    row.records["activity"] = activity.pk
            if flags or row.raw["errors"]:
                row.result = "Pending review"
                row.reason = "; ".join(filter(None, [row.reason, "Check: " + ", ".join(flags + (["Excel cell error"] if row.raw["errors"] else []))]))
        else:
            a = row.raw["cells"]
            if row.sheet == "TOP KOL Confirmation":
                staff, method, contact_date, status, amount_raw, scope, note = a[4], a[5], a[6], a[9], a[18], a[19], a[20]
                amount_raw = amount_raw or row.raw["cached"].get("19")
            elif row.sheet == "INFO":
                staff, method, contact_date, status, amount_raw, scope, note = a[3], a[4], a[5], a[8], None, a[16], a[17]
            else:
                staff, method, contact_date, status, amount_raw, scope, note = a[3], a[4], a[5], a[8], a[17], a[18], a[19]
            actor = users.get(normalized(staff))
            timestamp = dated(contact_date)
            amount = numeric(amount_raw)
            confirmed = stage(status) == "Confirmed / Upcoming"
            reasons = []
            if clean(amount_raw) and not amount:
                reasons.append("Invalid quote")
            if contact_date and not timestamp:
                reasons.append("Invalid contact date")
            if confirmed and not amount:
                reasons.append("Confirmed status has no validated quote in this row")
            if commit and matched.pk:
                if timestamp and not Activity.objects.filter(profile=matched, actor=actor, channel=clean(method)[:80], created=timestamp, click=False).exists():
                    activity = Activity.objects.create(profile=matched, actor=actor, channel=clean(method)[:80], created=timestamp,
                        outcome="Replied" if stage(status) == "Replied" else "Historical outreach", notes=clean(note))
                    row.records["activity"] = activity.pk
                if amount:
                    text = clean(scope)
                    reel_count = re.search(r"\b(\d+)\s+(?:fb\s+|tiktok\s+|instagram\s+)?reels?\b", text, re.I)
                    ambiguous = bool(re.search(r"\d+\s*[-–/+]\s*\d+|\b(?:live|stream|posts?|stories|promo|monthly|weekly|daily|month|week|day)\b", text, re.I))
                    per_reel = bool(re.search(r"(?:per\s+|/\s*|each\s+)reel\b", text, re.I))
                    exact = Quote.objects.filter(profile=matched, amount=amount, currency="BDT", deliverables=text, kind="Creator quote")
                    quote = exact.first()
                    if not quote:
                        if Quote.objects.filter(profile=matched, kind="Creator quote").exists():
                            reasons.append("Conflicting or additional historical quote")
                        quote = Quote.objects.create(profile=matched, actor=actor, amount=amount, fx=settings.exchange_rate, created=timestamp or batch.created, deliverables=text, notes=clean(note),
                           format="Reels" if (per_reel or reel_count) and not ambiguous else "Unclear",
                           basis="Per Reel" if per_reel and not ambiguous else "Package" if reel_count and not ambiguous else "Unclear",
                           reels=int(reel_count.group(1)) if reel_count and not ambiguous else None)
                        row.records["quote"] = quote.pk
                    if confirmed:
                        deal, created = Deal.objects.get_or_create(quote=quote, defaults={"actor": actor, "state": "Confirmed / Upcoming", "created": timestamp or batch.created})
                        if created:
                            row.records["deal"] = deal.pk
            if reasons:
                row.result = "Pending review"
                row.reason = "; ".join(reasons)
        row.profile_id = matched.pk if matched else None
    SourceRow.objects.bulk_update([r for r in rows if r.sheet in OUTREACH | {MASTER}], ["result", "reason", "profile", "records"], batch_size=400)

def create_preview(content, filename, user):
    digest = hashlib.sha256(content).hexdigest()
    with transaction.atomic():
        batch, created = ImportBatch.objects.get_or_create(digest=digest, defaults={"actor": user, "filename": filename[:240]})
        if created:
            rows = list(scan(content))
            for row in rows:
                row.batch = batch
            SourceRow.objects.bulk_create(rows, batch_size=400)
            reconcile(batch)
            audit(user, "Workbook preview", batch.pk, rows=len(rows))
    return batch

def commit_batch(batch, user):
    with transaction.atomic():
        batch = ImportBatch.objects.select_for_update().get(pk=batch.pk)
        if batch.state != "Preview":
            return
        reconcile(batch, commit=True)
        batch.state = "Committed"
        batch.save(update_fields=["state"])
        audit(user, "Workbook committed", batch.pk)

def reverse_batch(batch, user):
    with transaction.atomic():
        batch = ImportBatch.objects.select_for_update().get(pk=batch.pk)
        if batch.state != "Committed":
            return
        rows = list(batch.rows.order_by("-pk"))
        safe_creators = set()
        for row in rows:
            ids = row.records
            if ids.get("creator") and Creator.objects.filter(pk=ids["creator"], updated=ids["baseline"]).exists():
                safe_creators.add(ids["creator"])
        for row in rows:
            ids = row.records
            if not ids:
                continue
            # Imported records with subsequent changes or relations remain available for review.
            if row.profile_id:
                owner_id = Profile.objects.filter(pk=row.profile_id).values_list("creator_id", flat=True).first()
            else:
                owner_id = None
            if owner_id not in safe_creators:
                row.result = "Pending review"
                row.reason = "Reversal requires review: existing or edited creator."
                row.save(update_fields=["result", "reason"])
                continue
            try:
                with transaction.atomic():
                    if ids.get("deal"):
                        Deal.objects.filter(pk=ids["deal"], state="Confirmed / Upcoming", confirmed_at__isnull=True, payments__isnull=True).delete()
                    if ids.get("quote"):
                        Quote.objects.filter(pk=ids["quote"]).delete()
                    if ids.get("activity"):
                        Activity.objects.filter(pk=ids["activity"], done=False).delete()
            except ProtectedError:
                row.result, row.reason = "Pending review", "Reversal requires review: dependent deal or payment."
                row.save(update_fields=["result", "reason"])
        for row in rows:
            if row.records.get("creator") not in safe_creators:
                continue
            creator = Creator.objects.get(pk=row.records["creator"])
            if creator.profiles.count() == 1 and not Quote.objects.filter(profile__creator=creator).exists() and not Activity.objects.filter(profile__creator=creator).exists() and not creator.profiles.filter(blocker__isnull=False).exists():
                creator.delete()
                row.result, row.reason = "Reversed", ""
            else:
                row.result, row.reason = "Pending review", "Reversal requires review: creator has additional records."
            row.save(update_fields=["result", "reason"])
        batch.state = "Reversal reviewed"
        batch.save(update_fields=["state"])
        audit(user, "Workbook reversal", batch.pk)

@login_required
def index(request):
    allowed(request, ("Admin",))
    if request.method == "POST":
        upload = request.FILES.get("workbook")
        if not upload or not upload.name.lower().endswith(".xlsx") or upload.size > 12 * 1024 * 1024:
            messages.error(request, "Choose an XLSX workbook up to 12 MB.")
        else:
            try:
                return redirect("import_detail", pk=create_preview(upload.read(), upload.name, request.user).pk)
            except (ValueError, KeyError, zipfile.BadZipFile, OSError):
                messages.error(request, "The workbook could not be read or exceeded import limits.")
    return page(request, "Excel imports", importing=True, batches=ImportBatch.objects.order_by("-created"))

@login_required
def detail(request, pk):
    from django.core.paginator import Paginator
    from django.db.models import Count
    allowed(request, ("Admin",))
    batch = get_object_or_404(ImportBatch, pk=pk)
    if request.method == "POST":
        if request.POST.get("action") == "commit":
            commit_batch(batch, request.user)
        elif request.POST.get("action") == "reverse":
            reverse_batch(batch, request.user)
        elif request.POST.get("action") == "resolve":
            row_id = request.POST.get("row")
            row = get_object_or_404(batch.rows, pk=row_id, result="Pending review")
            note = request.POST.get("resolution", "").strip()
            if note and batch.state != "Preview":
                row.result, row.reason = "Reviewed", note[:5000]
                row.save(update_fields=["result", "reason"])
                audit(request.user, "Import row reviewed", row.pk)
        return redirect("import_detail", pk=pk)
    rows = batch.rows.order_by("sheet", "number")
    if request.GET.get("export") == "csv":
        response = csv_response("reconciliation.csv")
        writer = csv.writer(response)
        writer.writerow(["Sheet", "Excel row", "Result", "Reason", "Profile ID", "Raw values", "Recorded IDs"])
        for row in rows.iterator():
            writer.writerow([row.sheet, row.number, row.result, csv_value(row.reason), row.profile_id, str(row.raw), str(row.records)])
        return response
    if request.GET.get("review"):
        rows = rows.filter(result="Pending review")
    counts = batch.rows.values("sheet", "result").annotate(total=Count("id")).order_by("sheet", "result")
    duplicate_count = batch.rows.filter(sheet=MASTER, reason__icontains="Duplicate of an earlier row").count()
    return page(request, "Import reconciliation", batch=batch, import_rows=Paginator(rows, 50).get_page(request.GET.get("page")), counts=counts, duplicate_count=duplicate_count)
