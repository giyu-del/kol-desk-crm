import re
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import parse_qs, urlsplit

from django.db.models import Q
from django.utils import timezone

from .models import Activity, Audit, Blocker, Creator, Deal, Member, Payment, Quote

def role(user):
    if user.is_superuser:
        return "Admin"
    return getattr(getattr(user, "member", None), "role", "Read-only")

def creators_for(user):
    if role(user) in ("Admin", "Manager"):
        return Creator.objects.all()
    if role(user) == "Finance":
        return Creator.objects.filter(profiles__quotes__deal__isnull=False).distinct()
    if role(user) == "KOL Staff":
        return Creator.objects.filter(owner=user)
    return Creator.objects.filter(readers=user)

def audit(user, action, target, **data):
    Audit.objects.create(actor=user, action=action, target=str(target), data=data)

# Automatic follow-up for a new pipeline stage: (days until due, task text).
# No entry means no automatic task for that stage.
STAGE_FOLLOWUPS = {
    "Contacted": (5, "Follow up if no reply"),
    "Replied": (1, "Send the brief and offer details"),
    "Potential Collaboration": (2, "Follow up on the collaboration"),
    "Negotiating": (2, "Follow up on the negotiation"),
    "Confirmed / Upcoming": (2, "Share the brief and confirm the publish date"),
    "In Progress": (2, "Check the post is live"),
}

def schedule_stage_followup(creator, stage, actor, due=None, profile=None):
    """Create the automatic follow-up for a new stage, unless one is already waiting.

    `due` overrides the rule's day count (used for "In Progress", due on the
    planned publish date). Returns the created Activity, or None.
    """
    rule = STAGE_FOLLOWUPS.get(stage)
    if rule is None:
        return None
    days, note = rule
    profile = profile or creator.profiles.order_by("pk").first()
    if profile is None:
        return None
    if Activity.objects.filter(profile=profile, done=False, outcome="Follow-up Needed", notes=note).exists():
        return None
    when = due or timezone.now() + timedelta(days=days)
    return Activity.objects.create(profile=profile, actor=actor, channel="System",
                                   outcome="Follow-up Needed", notes=note, followup=when)

def publish_due(day):
    """9:00 on the planned publish date, or None when no date is set."""
    if not day:
        return None
    return timezone.make_aware(datetime.combine(day, time(9, 0)))

def social_url(raw):
    url = urlsplit(raw if "://" in raw else "https://" + raw)
    host = (url.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if url.scheme not in ("https", "http") or url.username or url.password or "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
        raise ValueError("Enter a valid public profile link.")
    aliases = {"fb.com": "facebook.com", "youtu.be": "youtube.com"}
    host = aliases.get(host, host)
    platforms = {"facebook.com": "Facebook", "instagram.com": "Instagram", "tiktok.com": "TikTok", "youtube.com": "YouTube", "t.me": "Telegram", "x.com": "X"}
    platform = platforms.get(host, "TikTok" if host in ("vm.tiktok.com", "vt.tiktok.com") else host[:80])
    parts = [part for part in url.path.strip("/").split("/") if part]
    if not parts:
        raise ValueError("Paste a profile, Page, or reference link.")
    reference = bool(set(parts) & {"share", "reel", "posts", "post", "video", "videos", "watch", "shorts", "p", "status"}) or host in ("vm.tiktok.com", "vt.tiktok.com") or url.hostname == "youtu.be"
    path = "/".join(parts)
    if host == "facebook.com":
        if parts[0] == "profile.php":
            identity = parse_qs(url.query).get("id", [""])[0]
            if not identity.isdigit():
                raise ValueError("Facebook profile link needs a valid profile ID.")
            path = "profile.php?id=" + identity
        elif not reference:
            path = path.lower() if parts[0] in ("people", "pages") else parts[0].lower()
    key = f"{host}/{path}"
    if len(key) > 500:
        raise ValueError("Profile link is too long.")
    return platform, None if reference else key, reference, url.geturl()

def phone_number(raw):
    if not re.fullmatch(r"[+0-9 .()-]+", raw):
        return None
    digits = re.sub(r"\D", "", raw)
    if re.fullmatch(r"01[3-9]\d{8}", digits):
        digits = "88" + digits
    elif re.fullmatch(r"1[3-9]\d{8}", digits):
        digits = "880" + digits
    return digits if re.fullmatch(r"8801[3-9]\d{8}", digits) else None

def score_rows(rows, weights, cheaper=(), *, allow_single=False):
    groups = defaultdict(list)
    for item, platform, metrics in rows:
        if platform and all(x is not None for x in metrics[:3]):
            groups[platform.casefold().replace(" ", "")].append((item, metrics))
    scores = {}
    for group in groups.values():
        if len(group) < 2 and not allow_single:
            continue
        ranks = []
        for index in range(len(weights)):
            values = sorted((m[index] for _, m in group if m[index] is not None), reverse=index in cheaper)
            ranks.append({})
            start = 0
            while start < len(values):
                end = start + 1
                while end < len(values) and values[end] == values[start]:
                    end += 1
                ranks[-1][values[start]] = Decimal(50) if len(values) == 1 else Decimal(50 * (start + end - 1)) / (len(values) - 1)
                start = end
        for item, metrics in group:
            present = [(rank[v], w) for rank, v, w in zip(ranks, metrics, weights) if v is not None]
            scores[item] = (sum(rank * w for rank, w in present) / sum(w for _, w in present)).quantize(Decimal(".1"), rounding=ROUND_HALF_UP)
    return scores

def _costs(unit, profile):
    """(cost per engagement, cost per 1k followers, cost per 1k views)
    for a per-Reel price in one currency. Any figure whose underlying
    data is missing comes back as None."""
    if unit is None:
        return None, None, None
    cpe = unit / profile.engagement if profile.engagement and profile.views else None
    cpf = unit * 1000 / profile.followers if profile.followers else None
    cpv = unit * 1000 / profile.views if profile.views else None
    return cpe, cpf, cpv

def audience_check(profile):
    """Simple fake-follower check from the numbers already in the CRM.

    Returns {"verdict": ..., "flags": [...]}. The flags are plain-language
    warnings, not proof: a real audience can trip one, so nothing is blocked
    or labelled fake. Growth and comment-authenticity checks are not possible
    here — the CRM keeps no follower history and no comment text.
    """
    flags = []
    engagement = profile.engagement
    followers = profile.followers
    if engagement is None and followers is None:
        return {"verdict": "Needs data",
                "flags": ["Add followers, views and engagement numbers to check this audience."]}
    er = profile.er_followers
    if followers and followers >= 1000 and er is not None:
        if er < Decimal("0.005"):
            flags.append(f"Very low engagement: only {er:.1%} of followers interact. "
                         "Bought or inactive followers are possible.")
        elif er > Decimal("0.25"):
            flags.append(f"Unusually high engagement: {er:.1%} of followers interact. "
                         "Engagement pods or bought engagement are possible.")
        if profile.views is not None and profile.views / followers < Decimal("0.05"):
            flags.append(f"Views are much lower than followers: average views are "
                         f"{profile.views / followers:.1%} of the follower count. "
                         "Many followers may be bought or inactive.")
    if profile.likes and profile.comments is not None and engagement:
        share = profile.comments / engagement
        if share < Decimal("0.01"):
            flags.append(f"Almost no comments: only {share:.1%} of engagement is comments. "
                         "Real audiences usually comment more.")
    if not flags:
        verdict = "Looks healthy"
    elif len(flags) < 3:
        verdict = "Worth a look"
    else:
        verdict = "Check carefully"
    return {"verdict": verdict, "flags": flags}

def costs(quote):
    return _costs(quote.unit_rm, quote.profile)

def costs_bdt(quote):
    """Same three per-unit costs as costs(), in BDT instead of RM."""
    return _costs(quote.unit_bdt, quote.profile)

def band(score):
    if score is None:
        return None, None, ""
    if score >= 85:
        return 9100, 15000, "strong"
    if score >= 70:
        return 8100, 9000, "good"
    if score >= 50:
        return 7100, 8000, "review"
    return 5000, 6500, "low"

COMPLETED_CONTACTS = ["Spoke", "Message Sent", "Replied", "Interested"]
REPLIES = ["Replied", "Interested"]

def report_data(shift, end=None):
    end = end or timezone.now()
    span = (shift.start, end)
    activity = Activity.objects.filter(actor=shift.staff, created__range=span, click=False)
    blockers = Blocker.objects.filter(staff=shift.staff).exclude(state="Withdrawn").filter(Q(created__range=span) | Q(created__lte=end, state__in=["Open", "In Review"]))
    quotes = Quote.objects.filter(actor=shift.staff, created__range=span)
    return {
        "Staff": shift.staff.get_full_name() or shift.staff.username,
        "Date": str(timezone.localtime(shift.start).date()),
        "Clock in": timezone.localtime(shift.start).strftime("%H:%M"),
        "Clock out": timezone.localtime(end).strftime("%H:%M") if shift.end else "Open",
        "Minutes worked": int((end - shift.start).total_seconds() / 60),
        "Creators added": Creator.objects.filter(added_by=shift.staff, created__range=span).count(),
        "Creators contacted": activity.filter(outcome__in=COMPLETED_CONTACTS).values("profile__creator").distinct().count(),
        "Creators replied": activity.filter(outcome__in=REPLIES).values("profile__creator").distinct().count(),
        "Quotes collected": quotes.filter(kind="Creator quote").count(),
        "Counteroffers": quotes.filter(kind="Counteroffer").count(),
        "Deals confirmed": Deal.objects.filter(actor=shift.staff, confirmed_at__range=span).count(),
        "Potential collaborations": list(Creator.objects.filter(owner=shift.staff, stage="Potential Collaboration").values_list("name", flat=True)),
        "Blockers": list(blockers.values_list("issue", "state")),
        "Payment requests": Payment.objects.filter(actor=shift.staff, created__range=span).count(),
    }

def group_report(data):
    lines = [f"{data['Staff']} · {data['Date']} · {data['Minutes worked']} min",
             f"Added {data['Creators added']} | Contacted {data['Creators contacted']} | Replied {data['Creators replied']}",
             f"Quotes {data['Quotes collected']} | Counteroffers {data['Counteroffers']} | Confirmed {data['Deals confirmed']}"]
    if data["Blockers"]:
        lines.append("Blockers: " + ", ".join(sorted({issue for issue, _ in data["Blockers"]})))
    return "\n".join(lines)
