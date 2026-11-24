import csv
import hashlib
import uuid
from decimal import Decimal, InvalidOperation
from urllib.parse import quote as encode
from datetime import timedelta

import pyotp
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST
from django import forms

from .forms import CreatorForm, DealForm, PaymentForm, QuoteForm, ScheduleForm, StatsForm
from .models import (Activity, AssignmentRequest, Audit, BLOCKERS, Blocker, Config, Creator,
                     Deal, GroupDelivery, LoginThrottle, Member, OUTCOMES, Payment, Profile, Quote, Report, Schedule, Shift)
from .services import (COMPLETED_CONTACTS, REPLIES, audit, band, costs, costs_bdt, creators_for,
                       group_report, phone_number, report_data, role, score_rows, social_url)

User = get_user_model()
MANAGE = ("Admin", "Manager")
EDIT = (*MANAGE, "KOL Staff")
FINANCE = ("Admin", "Finance")

def allowed(request, roles):
    if role(request.user) not in roles:
        raise PermissionDenied

def profiles_for(request):
    return Profile.objects.filter(creator__in=creators_for(request.user))

def config():
    return Config.objects.get_or_create(pk=1)[0]

def page(request, title, **context):
    return render(request, "page.html", {"title": title, "role": role(request.user), **context})

def form_page(request, title, form, done, *, token=None):
    if request.method == "POST" and form.is_valid():
        try:
            return done(form)
        except (ValueError, ValidationError, IntegrityError) as error:
            form.add_error(None, str(error) if not isinstance(error, IntegrityError) else "This record already exists.")
    return page(request, title, form=form, token=token)

def health(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return HttpResponse("OK", content_type="text/plain")

def sign_in(request):
    if request.user.is_authenticated:
        return redirect("home")
    error = ""
    if request.method == "POST":
        name = request.POST.get("username", "").strip()[:150]
        # Key by account so client-controlled forwarding headers cannot bypass the limit.
        key = hashlib.sha256(name.casefold().encode()).hexdigest()
        with transaction.atomic():
            limiter, _ = LoginThrottle.objects.select_for_update().get_or_create(key=key)
            if limiter.since < timezone.now() - timedelta(minutes=15):
                limiter.attempts = 0
                limiter.since = timezone.now()
            user = None
            if limiter.attempts < 8:
                limiter.attempts += 1
                user = authenticate(request, username=name, password=request.POST.get("password", ""))
            if user and role(user) in (*MANAGE, "Finance"):
                member, _ = Member.objects.select_for_update().get_or_create(user=user)
                counter = int(timezone.now().timestamp()) // 30
                code = request.POST.get("otp", "")
                match = next((c for c in (counter, counter - 1, counter + 1) if c > member.otp_counter and member.otp_secret and pyotp.utils.strings_equal(pyotp.TOTP(member.otp_secret).generate_otp(c), code)), None)
                if match is None:
                    user = None
                else:
                    member.otp_counter = match
                    member.save(update_fields=["otp_counter"])
            if user:
                limiter.attempts = 0
                limiter.save()
                login(request, user)
                return redirect("home")
            limiter.save()
        error = "Sign-in failed. Check your credentials and authenticator code, or try again in 15 minutes."
    return render(request, "login.html", {"error": error})

@login_required
@require_POST
def sign_out(request):
    logout(request)
    return redirect("login")

@login_required
def home(request):
    creators = creators_for(request.user)
    activities = Activity.objects.filter(profile__creator__in=creators, click=False)
    if role(request.user) not in MANAGE:
        activities = activities.filter(actor=request.user)
    if role(request.user) in ("Finance", "Read-only"):
        activities = activities.none()
    today = timezone.localdate()
    quotes = Quote.objects.filter(profile__creator__in=creators, kind="Creator quote")
    blockers = Blocker.objects.exclude(state__in=["Resolved", "Withdrawn"])
    if role(request.user) not in MANAGE:
        blockers = blockers.filter(staff=request.user)
    approved = Deal.objects.filter(quote__profile__creator__in=creators, state__in=["Confirmed / Upcoming", "In Progress", "Completed"])
    start_week = today - timedelta(days=today.weekday())
    managers = role(request.user) in MANAGE
    working = list(Shift.objects.filter(end__isnull=True, staff__member__role="KOL Staff").select_related("staff")) if managers else []
    upcoming = Schedule.objects.filter(date__gte=today).select_related("staff").order_by("date")[:8] if managers else None
    return page(request, "Overview", dashboard=True, working=working, upcoming=upcoming, open_blockers=blockers.order_by("-created")[:8],
                new_replies=activities.filter(outcome__in=REPLIES).select_related("profile__creator").order_by("-created")[:8],
                quotes_to_review=quotes.filter(deal__isnull=True).select_related("profile__creator").order_by("-created")[:8],
                can_manage=managers,
                stats=[("Creators", creators.count()), ("Contacted today", activities.filter(created__date=today, outcome__in=COMPLETED_CONTACTS).values("profile__creator").distinct().count()),
                       ("Replied today", activities.filter(created__date=today, outcome__in=REPLIES).values("profile__creator").distinct().count()),
                       ("New quotes today", quotes.filter(created__date=today).count()), ("Open blockers", blockers.count()), ("Potential KOLs", creators.filter(stage="Potential Collaboration").count()),
                       ("Confirmed this week", approved.filter(confirmed_at__date__gte=start_week).count())],
                followups=activities.filter(done=False, followup__lte=timezone.now()).select_related("profile__creator").order_by("followup")[:30],
                recent=creators.order_by("-updated")[:8], shift=Shift.objects.filter(staff=request.user, end__isnull=True).first())

@login_required
def creators(request):
    query = creators_for(request.user).prefetch_related("profiles", "owner")
    term = request.GET.get("q", "").strip()
    if term:
        query = query.filter(Q(name__icontains=term) | Q(niche__icontains=term) | Q(profiles__url__icontains=term))
    for param, field in (("platform", "profiles__platform"), ("stage", "stage"), ("owner", "owner__username")):
        if request.GET.get(param):
            query = query.filter(**{field: request.GET[param]})
    sort = request.GET.get("sort", "-updated")
    if sort not in ("name", "-name", "stage", "-stage", "-updated", "niche"):
        sort = "-updated"
    rows = Paginator(query.distinct().order_by(sort, "pk"), 40).get_page(request.GET.get("page"))
    return page(request, "Creators", creators=rows, creator_page=True)

@login_required
@require_POST
def quick_add(request):
    allowed(request, EDIT)
    try:
        platform, canonical, reference, url = social_url(request.POST.get("url", "").strip())
        name = request.POST.get("name", "").strip()
        if len(name) > 240 or len(url) > 2500:
            raise ValueError("Name or URL is too long.")
        creator_id = request.POST.get("creator")
        existing_creator = get_object_or_404(creators_for(request.user), pk=creator_id) if creator_id and creator_id.isdigit() else None
        with transaction.atomic():
            match = Profile.objects.filter(canonical=canonical).first() if canonical else Profile.objects.filter(reference=True, url=url).first()
            if not match:
                creator = existing_creator or Creator.objects.create(name=name, owner=request.user, added_by=request.user)
                Profile.objects.create(creator=creator, platform=platform, canonical=canonical, url=url, reference=reference)
                messages.success(request, "Creator link saved.")
                return redirect("creator", pk=creator.pk)
    except IntegrityError:
        match = Profile.objects.get(canonical=canonical)
    except ValueError as error:
        messages.error(request, str(error))
        return redirect("creators")
    if creators_for(request.user).filter(pk=match.creator_id).exists():
        messages.info(request, "Already in CRM.")
        return redirect("creator", pk=match.creator_id)
    if request.POST.get("request_assignment") == "yes":
        AssignmentRequest.objects.get_or_create(creator=match.creator, staff=request.user)
        messages.info(request, "Assignment requested.")
        return redirect("creators")
    return page(request, "Already in CRM", duplicate_url=url)

@login_required
def creator(request, pk):
    obj = get_object_or_404(creators_for(request.user), pk=pk)
    profile_list = list(obj.profiles.all())
    activities = Activity.objects.filter(profile__creator=obj).select_related("actor", "profile").order_by("-created") if role(request.user) != "Finance" else Activity.objects.none()
    quotes = Quote.objects.filter(profile__creator=obj).select_related("profile", "actor", "deal").order_by("-created")
    return page(request, str(obj), creator=obj, profiles=profile_list, activity=activities[:100], quote_history=quotes,
                edit=role(request.user) in EDIT, outcomes=OUTCOMES, issues=BLOCKERS,
                members=User.objects.filter(is_active=True, member__role="KOL Staff") if role(request.user) in MANAGE else [])

@login_required
def edit_creator(request, pk):
    allowed(request, EDIT)
    obj = get_object_or_404(creators_for(request.user), pk=pk)
    def save(form):
        if form.cleaned_data["stage"] in ("Confirmed / Upcoming", "In Progress", "Completed") and form.cleaned_data["stage"] != old:
            raise ValueError("Update the stage from the approved deal.")
        form.save()
        return redirect("creator", pk=pk)
    old = obj.stage
    return form_page(request, "Edit creator", CreatorForm(request.POST if request.method == "POST" else None, instance=obj), save)

@login_required
def edit_stats(request, pk):
    allowed(request, EDIT)
    obj = get_object_or_404(profiles_for(request), pk=pk)
    def save(form):
        form.save()
        Creator.objects.filter(pk=obj.creator_id).update(updated=timezone.now())
        return redirect("creator", pk=obj.creator_id)
    return form_page(request, "Edit profile metrics", StatsForm(request.POST if request.method == "POST" else None, instance=obj), save)

@login_required
@require_POST
def assign(request, pk):
    allowed(request, MANAGE)
    creator = get_object_or_404(Creator, pk=pk)
    user = get_object_or_404(User, pk=request.POST.get("staff"), is_active=True, member__role="KOL Staff")
    with transaction.atomic():
        creator.owner = user
        creator.save(update_fields=["owner", "updated"])
        AssignmentRequest.objects.filter(creator=creator, staff=user).delete()
        audit(request.user, "Assign creator", creator.pk, staff=user.pk)
    return redirect("creator", pk=pk)

@login_required
@require_POST
def contact(request, pk, channel):
    allowed(request, EDIT)
    obj = get_object_or_404(profiles_for(request), pk=pk)
    number = phone_number(obj.creator.phone)
    urls = {"call": f"tel:+{number}" if number else "", "whatsapp": f"https://wa.me/{number}?text={encode(config().whatsapp_template)}" if number else "",
            "profile": obj.url, "email": f"mailto:{obj.creator.email}" if obj.creator.email else ""}
    if channel == "messenger" and obj.platform == "Facebook" and obj.canonical:
        identity = obj.canonical.split("/", 1)[1].removeprefix("profile.php?id=")
        if "/" not in identity:
            urls[channel] = f"https://m.me/{encode(identity)}"
    target = urls.get(channel)
    if not target:
        messages.error(request, "Add a valid contact method first.")
        return redirect("creator", pk=obj.creator_id)
    Activity.objects.create(profile=obj, actor=request.user, channel=obj.platform if channel == "profile" else channel.title(), outcome=("Dialer" if channel == "call" else channel.title()) + " launch requested", click=True)
    return render(request, "channel.html", {"title": "Continue outreach", "role": role(request.user), "target": target, "creator": obj.creator, "profile": obj, "channel": channel, "outcomes": OUTCOMES, "template": config().whatsapp_template})

@login_required
@require_POST
def record(request, pk):
    allowed(request, EDIT)
    obj = get_object_or_404(profiles_for(request), pk=pk)
    outcome = request.POST.get("outcome")
    if outcome not in OUTCOMES:
        raise PermissionDenied
    raw = request.POST.get("followup", "")
    try:
        follow = parse_datetime(raw) if raw else None
        if raw and not follow:
            raise ValueError
        if follow and timezone.is_naive(follow):
            follow = timezone.make_aware(follow)
    except ValueError:
        messages.error(request, "Enter a valid follow-up date and time.")
        return redirect("creator", pk=obj.creator_id)
    with transaction.atomic():
        Activity.objects.create(profile=obj, actor=request.user, outcome=outcome, followup=follow,
                                channel=request.POST.get("channel", obj.platform)[:80], notes=request.POST.get("notes", "")[:8000])
        new = {"Spoke": "Contacted", "Message Sent": "Contacted", "Replied": "Replied", "Interested": "Potential Collaboration"}.get(outcome)
        if new and obj.creator.stage in ("New", "Contacted", "Replied"):
            if obj.creator.stage != "Replied" or new != "Contacted":
                obj.creator.stage = new
                obj.creator.save(update_fields=["stage", "updated"])
    return redirect("creator", pk=obj.creator_id)

@login_required
@require_POST
def finish_followup(request, pk):
    allowed(request, EDIT)
    activity = get_object_or_404(Activity, pk=pk, profile__in=profiles_for(request))
    activity.done = True
    activity.save(update_fields=["done"])
    if request.POST.get("next") == "my_followups":
        return redirect("my_followups")
    return redirect("outreach")

@login_required
def my_followups(request):
    allowed(request, EDIT)
    items = Activity.objects.filter(profile__in=profiles_for(request), done=False, followup__isnull=False).select_related("profile__creator", "actor").order_by("followup")
    if role(request.user) not in MANAGE:
        items = items.filter(actor=request.user)
    if role(request.user) in MANAGE and request.GET.get("staff"):
        items = items.filter(actor__username__iexact=request.GET["staff"])
    now = timezone.now()
    today = timezone.localdate()
    overdue, due_today, upcoming = [], [], []
    for item in items:
        if item.followup <= now:
            overdue.append(item)
        elif timezone.localtime(item.followup).date() == today:
            due_today.append(item)
        else:
            upcoming.append(item)
    return page(request, "My follow-ups", my_followups=True, is_manager=role(request.user) in MANAGE,
                queue_groups=[("Overdue", overdue), ("Due today", due_today), ("Upcoming", upcoming)])

@login_required
@require_POST
def snooze_followup(request, pk):
    allowed(request, EDIT)
    activity = get_object_or_404(Activity, pk=pk, profile__in=profiles_for(request))
    if activity.followup and not activity.done:
        activity.followup = activity.followup + timedelta(days=1)
        activity.save(update_fields=["followup"])
    return redirect("my_followups")

@login_required
def outreach(request):
    items = Activity.objects.filter(profile__in=profiles_for(request)).select_related("profile__creator", "actor").order_by("-created")
    if role(request.user) in ("Finance", "Read-only"):
        raise PermissionDenied
    return page(request, "Outreach", outreach=True, activity=Paginator(items, 50).get_page(request.GET.get("page")))

@login_required
def add_quote(request, pk):
    allowed(request, EDIT)
    profile = get_object_or_404(profiles_for(request), pk=pk)
    def save(form):
        with transaction.atomic():
            obj = form.save(commit=False)
            obj.actor, obj.profile, obj.fx = request.user, profile, config().exchange_rate
            obj.save()
            if profile.creator.stage in ("New", "Contacted", "Replied", "Potential Collaboration"):
                profile.creator.stage = "Negotiating"
                profile.creator.save(update_fields=["stage", "updated"])
            audit(request.user, "Added offer", obj.pk)
        return redirect("creator", pk=profile.creator_id)
    return form_page(request, "Record offer", QuoteForm(request.POST if request.method == "POST" else None), save)

@login_required
def negotiation(request):
    eligible = list(Profile.objects.filter(reference=False).select_related("creator"))
    scores = score_rows(((p.pk, p.platform, (p.views if p.views and p.views > 0 else None, p.er_views, p.er_followers)) for p in eligible), (45, 35, 20))
    allowed_ids = set(profiles_for(request).values_list("pk", flat=True))
    quoted = Quote.objects.filter(kind="Creator quote", profile_id__in=allowed_ids).order_by("pk").select_related("profile")
    latest = {q.profile_id: q for q in quoted}
    filter_quote = request.GET.get("quoted")
    rows = []
    for p in eligible:
        if p.pk not in allowed_ids or (request.GET.get("platform") and request.GET["platform"].lower() not in p.platform.lower()):
            continue
        q = latest.get(p.pk)
        if (filter_quote == "yes" and not q) or (filter_quote == "no" and q):
            continue
        score = scores.get(p.pk)
        low, high, color = band(score)
        rows.append((p, score, low, high, color, q))
    rows.sort(key=lambda r: (r[1] is None, -(r[1] or 0), str(r[0].creator).casefold()))
    return page(request, "Negotiation", negotiation=Paginator(rows, 50).get_page(request.GET.get("page")))

@login_required
def quotes(request):
    all_quotes = list(Quote.objects.filter(kind="Creator quote").select_related("profile__creator"))
    quote_costs = {q.pk: costs(q) for q in all_quotes}
    quote_costs_bdt = {q.pk: costs_bdt(q) for q in all_quotes}
    scores = score_rows(((q.pk, q.profile.platform, (quote_costs[q.pk][2], quote_costs[q.pk][0], quote_costs[q.pk][1], q.profile.views)) for q in all_quotes), (45, 30, 10, 15), (0, 1, 2))
    visible = set(profiles_for(request).values_list("pk", flat=True))
    rows = [(q, scores.get(q.pk), quote_costs[q.pk], quote_costs_bdt[q.pk]) for q in all_quotes if q.profile_id in visible and request.GET.get("platform", "").casefold() in q.profile.platform.casefold()]
    rows.sort(key=lambda r: (r[1] is None, -(r[1] or 0), r[0].bdt, r[0].pk))
    return page(request, "Rate card comparison", quotes=Paginator(rows, 50).get_page(request.GET.get("page")))

@login_required
def add_deal(request, pk):
    allowed(request, EDIT)
    quote = get_object_or_404(Quote, pk=pk, profile__in=profiles_for(request))
    if hasattr(quote, "deal"):
        return redirect("deal", pk=quote.deal.pk)
    def save(form):
        obj = form.save(commit=False)
        obj.actor, obj.quote = request.user, quote
        obj.save()
        audit(request.user, "Submitted deal", obj.pk)
        return redirect("deal", pk=obj.pk)
    return form_page(request, "Submit deal for approval", DealForm(request.POST if request.method == "POST" else None), save)

def deal_query(request):
    return Deal.objects.filter(quote__profile__in=profiles_for(request)).select_related("quote__profile__creator").prefetch_related("payments")

@login_required
def deals(request):
    approved = list(Deal.objects.filter(state__in=["Confirmed / Upcoming", "In Progress", "Completed"]).select_related("quote__profile"))
    scores = score_rows(((d.pk, d.quote.profile.platform, (costs(d.quote)[2], d.quote.profile.er_views, d.quote.profile.views, d.quote.profile.er_followers)) for d in approved), (35, 25, 25, 15), (0,), allow_single=True)
    rows = [(d, scores.get(d.pk)) for d in deal_query(request) if request.GET.get("platform", "").casefold() in d.quote.profile.platform.casefold()
            and request.GET.get("niche", "").casefold() in d.quote.profile.creator.niche.casefold()]
    if request.GET.get("budget"):
        try:
            limit = Decimal(request.GET["budget"])
            if limit.is_finite():
                rows = [(d, s) for d, s in rows if d.quote.bdt <= limit]
        except InvalidOperation:
            pass
    rows.sort(key=lambda r: (r[1] is None, -(r[1] or 0), r[0].pk))
    return page(request, "Confirmed rate card & deals", deals=Paginator(rows, 50).get_page(request.GET.get("page")))

@login_required
def deal(request, pk):
    obj = get_object_or_404(deal_query(request), pk=pk)
    upcoming = obj.state in ("Confirmed / Upcoming", "In Progress", "Completed")
    return page(request, "Deal detail", deal=obj, token=uuid.uuid4(),
                may_request=upcoming and role(request.user) in (*EDIT, "Finance"), finance=role(request.user) in FINANCE,
                transitions={"Pending Approval": ("Confirmed / Upcoming", "Declined"), "Confirmed / Upcoming": ("In Progress",), "In Progress": ("Completed",)}.get(obj.state, ()))

@login_required
@require_POST
def deal_state(request, pk):
    allowed(request, MANAGE)
    with transaction.atomic():
        obj = get_object_or_404(Deal.objects.select_for_update(), pk=pk)
        new = request.POST.get("state")
        valid = {"Pending Approval": ("Confirmed / Upcoming", "Declined"), "Confirmed / Upcoming": ("In Progress",), "In Progress": ("Completed",)}
        if new not in valid.get(obj.state, ()):
            raise PermissionDenied
        obj.state = new
        if new == "Confirmed / Upcoming":
            obj.confirmed_at = timezone.now()
        obj.save()
        if new != "Declined":
            Creator.objects.filter(pk=obj.quote.profile.creator_id).update(stage=new, updated=timezone.now())
        audit(request.user, "Deal state", pk, state=new)
    return redirect("deal", pk=pk)

@login_required
def payments(request):
    if role(request.user) == "Read-only":
        raise PermissionDenied
    rows = [(d, None) for d in deal_query(request).exclude(state__in=["Pending Approval", "Declined"]).order_by("due", "pk")]
    return page(request, "Payments", deals=Paginator(rows, 50).get_page(request.GET.get("page")), payment_view=True)

@login_required
def request_payment(request, pk):
    allowed(request, (*EDIT, "Finance"))
    obj = get_object_or_404(deal_query(request), pk=pk)
    token = request.POST.get("token") if request.method == "POST" else uuid.uuid4()
    def save(form):
        with transaction.atomic():
            current = Deal.objects.select_for_update().get(pk=obj.pk)
            key = uuid.UUID(str(token))
            if Payment.objects.filter(token=key, deal=current, actor=request.user).exists():
                return redirect("deal", pk=pk)
            if current.state in ("Pending Approval", "Declined"):
                raise ValueError("The deal needs approval.")
            entry = form.save(commit=False)
            reserved = sum(p.amount for p in current.payments.filter(state__in=["Pending Approval", "Approved", "Paid"]))
            if entry.amount + reserved > current.quote.amount:
                raise ValueError("The amount exceeds the unreserved balance.")
            entry.deal, entry.actor, entry.token = current, request.user, key
            entry.save()
            audit(request.user, "Payment requested", entry.pk)
        return redirect("deal", pk=pk)
    return form_page(request, "Request payment", PaymentForm(request.POST if request.method == "POST" else None), save, token=token)

@login_required
@require_POST
def payment_state(request, pk):
    allowed(request, FINANCE)
    with transaction.atomic():
        payment = get_object_or_404(Payment.objects.select_for_update(), pk=pk)
        new = request.POST.get("state")
        valid = {"Pending Approval": ("Approved", "Rejected"), "Approved": ("Paid", "Rejected"), "Paid": ("Reversed",)}
        if new not in valid.get(payment.state, ()):
            messages.error(request, "That payment transition is unavailable.")
            return redirect("deal", pk=payment.deal_id)
        if new == "Reversed" and not request.POST.get("reason", "").strip():
            messages.error(request, "Enter a reversal reason.")
            return redirect("deal", pk=payment.deal_id)
        if new == "Paid":
            payment.reference = request.POST.get("reference", "").strip()[:160]
            payment.method = request.POST.get("method", "").strip()[:100]
            if not payment.reference or not payment.method or Payment.objects.filter(state="Paid", reference=payment.reference, method=payment.method).exclude(pk=pk).exists():
                messages.error(request, "Enter a unique transaction reference and method.")
                return redirect("deal", pk=payment.deal_id)
            receipt = request.FILES.get("receipt")
            if receipt:
                if receipt.size > 3 * 1024 * 1024:
                    messages.error(request, "Receipt must be 3 MB or smaller.")
                    return redirect("deal", pk=payment.deal_id)
                payment.receipt = receipt.read()
        payment.state = new
        payment.save()
        audit(request.user, "Payment state", pk, state=new, reason=request.POST.get("reason", "")[:1000])
    return redirect("deal", pk=payment.deal_id)

@login_required
def receipt(request, pk):
    allowed(request, FINANCE)
    payment = get_object_or_404(Payment, pk=pk, receipt__isnull=False)
    return HttpResponse(bytes(payment.receipt), content_type="application/octet-stream", headers={"Content-Disposition": 'attachment; filename="payment-receipt"'})

@login_required
def attendance(request):
    allowed(request, EDIT)
    today = timezone.localdate()
    shifts = Shift.objects.select_related("staff").order_by("-start")
    schedule = Schedule.objects.filter(date__gte=today).select_related("staff").order_by("date")
    team = []
    if role(request.user) in MANAGE:
        for staff in User.objects.filter(is_active=True, member__role="KOL Staff"):
            if shifts.filter(staff=staff, end__isnull=True).exists():
                state = "Working"
            elif shifts.filter(staff=staff, end__date=today).exists():
                state = "Clocked Out"
            elif schedule.filter(staff=staff, date=today).exists():
                state = "Not Clocked In"
            else:
                state = "Off Today"
            team.append((staff, state))
    else:
        shifts = shifts.filter(staff=request.user)
        schedule = schedule.filter(staff=request.user)
    return page(request, "Attendance", attendance=True, team=team, schedule=schedule[:50], shifts=shifts[:80],
                shift=Shift.objects.filter(staff=request.user, end__isnull=True).first())

@login_required
def schedule_staff(request):
    allowed(request, MANAGE)
    form = ScheduleForm(request.POST if request.method == "POST" else None)
    form.fields["staff"].queryset = User.objects.filter(is_active=True, member__role="KOL Staff")
    def save(form):
        data = form.cleaned_data
        Schedule.objects.update_or_create(staff=data["staff"], date=data["date"], defaults={k: data[k] for k in ("start", "end")})
        audit(request.user, "Schedule updated", data["staff"].pk, date=str(data["date"]))
        return redirect("attendance")
    return form_page(request, "Schedule staff", form, save)

@login_required
@require_POST
def clock_in(request):
    allowed(request, EDIT)
    with transaction.atomic():
        User.objects.select_for_update().get(pk=request.user.pk)
        Shift.objects.get_or_create(staff=request.user, end__isnull=True)
    return redirect("attendance")

@login_required
@require_POST
def clock_out(request, pk):
    allowed(request, EDIT)
    with transaction.atomic():
        shift = get_object_or_404(Shift.objects.select_for_update(), pk=pk, staff=request.user)
        if not shift.end:
            shift.end = timezone.now()
            shift.save(update_fields=["end"])
        report, created = Report.objects.get_or_create(shift=shift, defaults={"data": report_data(shift, shift.end), "note": request.POST.get("note", "")[:5000]})
        if created:
            audit(request.user, "Clock out", shift.pk)
    return redirect("report", pk=shift.pk)

@login_required
def correct_shift(request, pk):
    allowed(request, MANAGE)
    obj = get_object_or_404(Shift, pk=pk)
    class Correction(forms.Form):
        start = forms.DateTimeField()
        end = forms.DateTimeField(required=False)
        reason = forms.CharField(max_length=1000)
    form = Correction(request.POST if request.method == "POST" else None, initial={"start": timezone.localtime(obj.start), "end": timezone.localtime(obj.end) if obj.end else None})
    def save(form):
        data = form.cleaned_data
        if data["end"] and data["end"] < data["start"]:
            raise ValueError("Clock out must be after clock in.")
        with transaction.atomic():
            shift = Shift.objects.select_for_update().get(pk=pk)
            audit(request.user, "Corrected shift", pk, start=str(shift.start), end=str(shift.end), reason=data["reason"])
            shift.start, shift.end = data["start"], data["end"]
            shift.save()
            if shift.end:
                Report.objects.update_or_create(shift=shift, defaults={"data": report_data(shift, shift.end)})
        return redirect("attendance")
    return form_page(request, "Correct attendance", form, save)

@login_required
def blockers(request):
    allowed(request, EDIT)
    rows = Blocker.objects.exclude(state="Withdrawn").select_related("staff", "reviewer").prefetch_related("profiles__creator")
    if role(request.user) not in MANAGE:
        rows = rows.filter(staff=request.user)
    for key, field in (("staff", "staff__username"), ("state", "state"), ("channel", "channel"), ("issue", "issue")):
        if request.GET.get(key):
            rows = rows.filter(**{field + "__icontains": request.GET[key]})
    if request.GET.get("date"):
        try:
            rows = rows.filter(created__date=forms.DateField().clean(request.GET["date"]))
        except ValidationError:
            pass
    return page(request, "Blockers", blockers=Paginator(rows.order_by("-created"), 40).get_page(request.GET.get("page")),
                issues=BLOCKERS, reviewers=User.objects.filter(is_active=True).filter(Q(member__role="Manager") | Q(is_superuser=True)))

@login_required
@require_POST
def add_blocker(request):
    allowed(request, EDIT)
    issue = request.POST.get("issue")
    if issue not in BLOCKERS:
        raise PermissionDenied
    pid = request.POST.get("profile")
    profile = get_object_or_404(profiles_for(request), pk=pid) if pid and pid.isdigit() else None
    channel = "WhatsApp" if issue.startswith("WhatsApp") else "Facebook" if issue.startswith("Facebook") else request.POST.get("channel", profile.platform if profile else "")[:80]
    link = request.POST.get("link", "")
    if link:
        try:
            link = social_url(link)[3][:2500]
        except ValueError:
            link = ""
    with transaction.atomic():
        User.objects.select_for_update().get(pk=request.user.pk)
        item = None
        if issue.endswith("restricted"):
            item = Blocker.objects.filter(staff=request.user, channel=channel, issue=issue, state__in=["Open", "In Review"]).first()
        if not item:
            item = Blocker.objects.create(staff=request.user, issue=issue, channel=channel, link=link,
                stage=profile.creator.stage if profile else "", shift=Shift.objects.filter(staff=request.user, end__isnull=True).first())
        if profile:
            item.profiles.add(profile)
    messages.success(request, "Blocker reported. Add a note or undo it below.")
    return redirect(reverse("blockers") + f"#blocker-{item.pk}")

@login_required
@require_POST
def change_blocker(request, pk):
    allowed(request, EDIT)
    is_manager = role(request.user) in MANAGE
    item = get_object_or_404(Blocker.objects.all() if is_manager else Blocker.objects.filter(staff=request.user), pk=pk)
    action = request.POST.get("action")
    if action == "undo" and item.staff_id == request.user.pk and item.state == "Open":
        item.state = "Withdrawn"
    elif action == "save":
        item.note = request.POST.get("note", "")[:5000]
        if is_manager:
            if request.POST.get("state") in ("Open", "In Review", "Resolved"):
                item.state = request.POST["state"]
            item.next_action = request.POST.get("next_action", "")[:5000]
            reviewer = request.POST.get("reviewer", "")
            item.reviewer = get_object_or_404(User.objects.filter(is_active=True).filter(Q(member__role="Manager") | Q(is_superuser=True)), pk=reviewer) if reviewer.isdigit() else None
    else:
        raise PermissionDenied
    item.save()
    return redirect("blockers")

def report_scope(request):
    allowed(request, EDIT)
    shifts = Shift.objects.select_related("staff", "report").order_by("-start")
    if role(request.user) not in MANAGE:
        shifts = shifts.filter(staff=request.user)
    return shifts

@login_required
def reports(request):
    shifts = report_scope(request)
    archive = None
    if role(request.user) in MANAGE:
        from .models import SourceRow
        archive = SourceRow.objects.filter(batch__state__in=["Committed", "Reversal reviewed"], sheet__in=["Daily Update Tracker", "Weekly Friday Report"]).order_by("sheet", "number")
    if request.GET.get("staff"):
        shifts = shifts.filter(staff__username__iexact=request.GET["staff"])
    for key, operator in (("from", "gte"), ("to", "lte")):
        if request.GET.get(key):
            try:
                shifts = shifts.filter(**{f"start__date__{operator}": forms.DateField().clean(request.GET[key])})
            except ValidationError:
                pass
    if request.GET.get("export") == "csv":
        response = csv_response("team-report.csv")
        writer = csv.writer(response)
        writer.writerow(["Staff", "Date", "Clock in", "Clock out", "Minutes worked", "Creators added", "Creators contacted", "Creators replied", "Quotes collected", "Counteroffers", "Deals confirmed", "Payment requests", "Blockers"])
        for shift in shifts.filter(report__isnull=False):
            row = shift.report.data
            writer.writerow([csv_value(row.get(k, "")) for k in ["Staff", "Date", "Clock in", "Clock out", "Minutes worked", "Creators added", "Creators contacted", "Creators replied", "Quotes collected", "Counteroffers", "Deals confirmed", "Payment requests", "Blockers"]])
        return response
    weekly = {}
    for shift in shifts.filter(report__isnull=False)[:1000]:
        day = timezone.localtime(shift.start).date()
        friday = day + timedelta(days=(4 - day.weekday()) % 7)
        key = (friday, shift.staff.username)
        row = weekly.setdefault(key, [0] * 5)
        for index, label in enumerate(["Creators added", "Creators contacted", "Creators replied", "Quotes collected", "Deals confirmed"]):
            row[index] += shift.report.data[label]
    return page(request, "Reports", reporting=True, shifts=Paginator(shifts, 40).get_page(request.GET.get("page")), archive=archive[:100] if archive is not None else None, weekly=list(weekly.items()))

def csv_response(name):
    return HttpResponse(content_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}"'})

def csv_value(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value

@login_required
def report(request, pk):
    shift = get_object_or_404(report_scope(request), pk=pk)
    saved = getattr(shift, "report", None)
    data = saved.data if saved else report_data(shift, shift.end)
    export = request.GET.get("export")
    if export == "csv":
        response = csv_response("shift-report.csv")
        writer = csv.writer(response)
        writer.writerow(["Status", "Final" if saved else "Draft"])
        writer.writerows((key, csv_value(value)) for key, value in data.items())
        if saved:
            writer.writerow(["Handover", csv_value(saved.note)])
        return response
    if export == "pdf":
        from .pdf import report_pdf
        content = report_pdf("End-of-day report" if saved else "Draft shift report", data, saved.note if saved else "")
        return HttpResponse(content, content_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="shift-report.pdf"'})
    if export == "print":
        return render(request, "print.html", {"data": data, "saved": saved})
    from .group_delivery import destination
    return page(request, "End-of-day report" if saved else "Draft shift report", saved=saved, report=data, shift=shift, group=group_report(data),
                delivery=getattr(saved, "groupdelivery", None) if saved else None,
                share_enabled=bool(saved and (shift.staff_id == request.user.pk or role(request.user) in MANAGE) and all(destination())))

@login_required
@require_POST
def send_report(request, pk):
    from .group_delivery import deliver, destination
    shift = get_object_or_404(report_scope(request), pk=pk, report__isnull=False)
    if not all(destination()) or request.POST.get("confirm") != "yes":
        raise PermissionDenied
    with transaction.atomic():
        saved = Report.objects.select_for_update().get(shift=shift)
        record, created = GroupDelivery.objects.get_or_create(report=saved, defaults={"actor": request.user})
        if not created and record.state != "Failed":
            return redirect("report", pk=pk)
        record.actor = request.user
        record.state = "Sending"
        record.save()
    state, message_id = deliver(group_report(saved.data))
    record.state = state
    record.message_id = message_id
    record.save()
    audit(request.user, "Group report delivery", saved.pk, state=state)
    return redirect("report", pk=pk)

@login_required
def preferences(request):
    allowed(request, ("Admin",))
    class ConfigForm(forms.ModelForm):
        class Meta:
            model = Config
            fields = ["exchange_rate", "whatsapp_template"]
        def clean_exchange_rate(self):
            value = self.cleaned_data["exchange_rate"]
            if value <= 0:
                raise ValidationError("Exchange rate must be positive.")
            return value
    def save(form):
        form.save()
        audit(request.user, "Updated settings", "configuration")
        return redirect("home")
    return form_page(request, "Settings", ConfigForm(request.POST if request.method == "POST" else None, instance=config()), save)

@login_required
def users(request):
    allowed(request, ("Admin",))
    return page(request, "Users", users=User.objects.select_related("member").order_by("username"))

@login_required
def user_edit(request, pk=None):
    from django.contrib.auth.password_validation import validate_password
    allowed(request, ("Admin",))
    obj = get_object_or_404(User, pk=pk) if pk else None
    if obj and obj.is_superuser:
        raise PermissionDenied
    class UserForm(forms.Form):
        username = forms.CharField(max_length=150)
        first_name = forms.CharField(max_length=150, required=False, label="Staff display name")
        role = forms.ChoiceField(choices=[(r, r) for r in ("Manager", "KOL Staff", "Finance", "Read-only")])
        password = forms.CharField(widget=forms.PasswordInput, required=not obj)
        active = forms.BooleanField(required=False, initial=True)
    form = UserForm(request.POST if request.method == "POST" else None, initial={"username": obj.username, "first_name": obj.first_name, "role": role(obj), "active": obj.is_active} if obj else None)
    def save(form):
        data = form.cleaned_data
        with transaction.atomic():
            user = obj or User()
            user.username, user.first_name, user.is_active = data["username"], data["first_name"], data["active"]
            if data["password"]:
                validate_password(data["password"], user)
                user.set_password(data["password"])
            prior = role(obj) if obj else None
            user.save()
            member, _ = Member.objects.get_or_create(user=user)
            member.role = data["role"]
            if prior != member.role or not user.is_active:
                from django.contrib.sessions.models import Session
                sessions = [session.pk for session in Session.objects.filter(expire_date__gte=timezone.now()) if session.get_decoded().get("_auth_user_id") == str(user.pk)]
                Session.objects.filter(pk__in=sessions).delete()
            if member.role in ("Manager", "Finance") and not member.otp_secret:
                member.otp_secret = pyotp.random_base32()
                member.save()
                audit(request.user, "User and MFA configured", user.pk, role=member.role)
                return page(request, "Authenticator setup", otp_secret=member.otp_secret, otp_account=user.username)
            member.save()
            audit(request.user, "User updated", user.pk, role=member.role, active=user.is_active)
        return redirect("users")
    return form_page(request, "Edit user" if obj else "Create user", form, save)

@login_required
def assignment_requests(request):
    allowed(request, MANAGE)
    return page(request, "Assignment requests", requests=AssignmentRequest.objects.select_related("creator", "staff").order_by("-created"))


@login_required
def data_export(request):
    allowed(request, ("Admin",))
    from .models import ImportBatch, SourceRow
    models = [Creator, Profile, Activity, Quote, Deal, Payment, Shift, Schedule, Report, Blocker, ImportBatch, SourceRow]
    data = {}
    for model in models:
        fields = [field.attname for field in model._meta.concrete_fields if field.name != "receipt"]
        data[model.__name__] = list(model.objects.values(*fields))
    data["BlockerProfiles"] = list(Blocker.profiles.through.objects.values("blocker_id", "profile_id"))
    audit(request.user, "Data export", "CRM")
    return JsonResponse(data, json_dumps_params={"ensure_ascii": False}, headers={"Content-Disposition": 'attachment; filename="kol-desk-data.json"', "Cache-Control": "no-store"})

@login_required
def audit_log(request):
    allowed(request, ("Admin",))
    return page(request, "Audit history", audit_items=Paginator(Audit.objects.select_related("actor").order_by("-at"), 50).get_page(request.GET.get("page")))
