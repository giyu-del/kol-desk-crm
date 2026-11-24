from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

U = settings.AUTH_USER_MODEL
STAGES = ["New", "Contacted", "Replied", "Potential Collaboration", "Negotiating", "Confirmed / Upcoming", "In Progress", "Completed", "On Hold", "Declined"]
OUTCOMES = ["No Answer", "Spoke", "Message Sent", "Replied", "Interested", "Follow-up Needed"]
BLOCKERS = ["WhatsApp account restricted", "WhatsApp message failed", "Facebook Messenger restricted", "Facebook Page messaging unavailable", "Other outreach channel unavailable", "Invalid phone or failed call", "No valid contact method", "Broken or inaccessible profile", "Too few KOLs for niche or location", "Bangladesh audience share unavailable", "Missing or unverified engagement data", "No suitable KOL within budget", "Other"]
ROLES = ["Manager", "KOL Staff", "Finance", "Read-only"]

def options(values):
    return [(v, v) for v in values]

class Member(models.Model):
    user = models.OneToOneField(U, on_delete=models.CASCADE, related_name="member")
    role = models.CharField(max_length=24, choices=options(ROLES), default="Read-only")
    otp_secret = models.CharField(max_length=64, blank=True, editable=False)
    otp_counter = models.BigIntegerField(default=-1, editable=False)

class Config(models.Model):
    exchange_rate = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal("31.1"))
    whatsapp_template = models.TextField(blank=True)
    lists = models.JSONField(default=dict, blank=True)

class Creator(models.Model):
    name = models.CharField(max_length=240, blank=True)
    niche = models.CharField(max_length=240, blank=True)
    country = models.CharField(max_length=140, blank=True, default="Bangladesh")
    owner = models.ForeignKey(U, null=True, blank=True, on_delete=models.SET_NULL, related_name="creators")
    added_by = models.ForeignKey(U, null=True, blank=True, on_delete=models.SET_NULL, related_name="added_creators")
    source_staff = models.CharField(max_length=150, blank=True)
    created = models.DateTimeField(default=timezone.now)
    stage = models.CharField(max_length=40, choices=options(STAGES), default="New")
    phone = models.CharField(max_length=150, blank=True)
    email = models.EmailField(blank=True)
    notes = models.TextField(blank=True)
    payment_remarks = models.TextField(blank=True)
    updated = models.DateTimeField(auto_now=True)
    readers = models.ManyToManyField(U, blank=True, related_name="shared_creators")

    def __str__(self):
        return self.name or "Unnamed creator"

class Profile(models.Model):
    creator = models.ForeignKey(Creator, on_delete=models.CASCADE, related_name="profiles")
    platform = models.CharField(max_length=80, db_index=True)
    url = models.URLField(max_length=2500, blank=True)
    canonical = models.CharField(max_length=500, unique=True, null=True, blank=True)
    reference = models.BooleanField(default=False)
    bd_audience = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    followers = models.BigIntegerField(null=True, blank=True)
    views = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    likes = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    comments = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    shares = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    created = models.DateTimeField(default=timezone.now)

    @property
    def engagement(self):
        values = [x for x in (self.likes, self.comments, self.shares) if x is not None]
        return sum(values, Decimal(0)) if values else None

    def rate(self, denominator):
        total = self.engagement
        return total / denominator if total is not None and denominator and denominator > 0 else None

    @property
    def er_followers(self):
        return self.rate(self.followers)

    @property
    def er_views(self):
        return self.rate(self.views)

    def __str__(self):
        return f"{self.creator} · {self.platform}"

class Post(models.Model):
    """A tracked post (reel or photo) on one of a creator's profiles.

    This is the record the "Top 5 Creators" dashboard ranks. Metrics are
    per post and recorded by staff; a missing metric is not zero — a reel
    without views cannot rank, and a photo with any of reactions, comments
    or shares missing is excluded until the metrics are complete.
    Portfolio-only posts are shown in the creator's gallery but never rank.
    """
    profile = models.ForeignKey(Profile, on_delete=models.CASCADE, related_name="posts")
    post_type = models.CharField(max_length=10, choices=options(["Reel", "Photo"]), default="Reel")
    url = models.URLField(max_length=2500)
    published_at = models.DateField()
    views = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    reactions = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    comments = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    shares = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    portfolio_only = models.BooleanField(default=False)
    created_by = models.ForeignKey(U, null=True, on_delete=models.PROTECT)
    created = models.DateTimeField(default=timezone.now)
    updated = models.DateTimeField(auto_now=True)

    @property
    def interactions(self):
        values = [self.reactions, self.comments, self.shares]
        return sum(values, Decimal(0)) if all(v is not None for v in values) else None

    def __str__(self):
        return f"{self.profile.creator} · {self.profile.platform} · {self.post_type}"

class Shift(models.Model):
    staff = models.ForeignKey(U, on_delete=models.PROTECT)
    start = models.DateTimeField(default=timezone.now)
    end = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["staff"], condition=Q(end__isnull=True), name="one_open_shift")]

class Schedule(models.Model):
    staff = models.ForeignKey(U, on_delete=models.PROTECT)
    date = models.DateField()
    start = models.TimeField(null=True, blank=True)
    end = models.TimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["staff", "date"], name="one_schedule_per_day")]

class Activity(models.Model):
    profile = models.ForeignKey(Profile, on_delete=models.CASCADE, related_name="activities")
    actor = models.ForeignKey(U, null=True, on_delete=models.PROTECT)
    created = models.DateTimeField(default=timezone.now, db_index=True)
    channel = models.CharField(max_length=80)
    outcome = models.CharField(max_length=90)
    notes = models.TextField(blank=True)
    followup = models.DateTimeField(null=True, blank=True)
    done = models.BooleanField(default=False)
    click = models.BooleanField(default=False)

class Quote(models.Model):
    profile = models.ForeignKey(Profile, on_delete=models.CASCADE, related_name="quotes")
    actor = models.ForeignKey(U, null=True, on_delete=models.PROTECT)
    created = models.DateTimeField(default=timezone.now)
    kind = models.CharField(max_length=24, choices=options(["Creator quote", "Counteroffer"]), default="Creator quote")
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    currency = models.CharField(max_length=3, choices=options(["BDT", "RM"]), default="BDT")
    fx = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal("31.1"))
    format = models.CharField(max_length=32, choices=options(["Reels", "Mixed", "Livestream", "Posts / stories", "Other", "Unclear"]), default="Unclear")
    basis = models.CharField(max_length=32, choices=options(["Per Reel", "Package", "Recurring", "Unclear"]), default="Unclear")
    reels = models.PositiveIntegerField(null=True, blank=True)
    verified_unit = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    deliverables = models.TextField(blank=True)
    notes = models.TextField(blank=True)

    @property
    def bdt(self):
        return self.amount if self.currency == "BDT" else self.amount * self.fx

    def _unit(self):
        """Per-Reel price in the quote's own currency, or None.

        Uses the verified per-Reel price when set, otherwise derives it
        from a clear basis: "Per Reel" means the amount already is the
        per-Reel price, and "Package" with a reel count divides the
        amount by the reels. Any other basis (Recurring, Unclear) has
        no per-Reel price, so this returns None.
        """
        unit = self.verified_unit
        if unit is None:
            if self.basis == "Per Reel":
                unit = self.amount
            elif self.basis == "Package" and self.reels:
                unit = self.amount / self.reels
        if unit is None or unit <= 0:
            return None
        return unit

    @property
    def unit_rm(self):
        unit = self._unit()
        return None if unit is None else (unit if self.currency == "RM" else unit / self.fx)

    @property
    def unit_bdt(self):
        unit = self._unit()
        return None if unit is None else (unit if self.currency == "BDT" else unit * self.fx)

    def __str__(self):
        return f"{self.profile} · {self.currency} {self.amount} · {self.kind}"

class Campaign(models.Model):
    name = models.CharField(max_length=240)
    budget = models.DecimalField(max_digits=16, decimal_places=2)
    start = models.DateField(null=True, blank=True)
    end = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(U, null=True, on_delete=models.PROTECT)
    created = models.DateTimeField(default=timezone.now)

    APPROVED_STATES = ("Confirmed / Upcoming", "In Progress", "Completed")

    @property
    def committed(self):
        """BDT agreed on approved deals. Declined and unapproved deals excluded."""
        return sum((d.quote.bdt for d in self.deals.filter(state__in=self.APPROVED_STATES)), Decimal(0))

    @property
    def paid_bdt(self):
        """BDT actually paid on approved deals (RM payments converted at the quote's rate)."""
        total = Decimal(0)
        for d in self.deals.filter(state__in=self.APPROVED_STATES):
            total += d.paid if d.quote.currency == "BDT" else d.paid * d.quote.fx
        return total

    @property
    def remaining(self):
        return self.budget - self.committed

    def __str__(self):
        return self.name

class Deal(models.Model):
    quote = models.OneToOneField(Quote, on_delete=models.PROTECT, related_name="deal")
    campaign = models.ForeignKey(Campaign, null=True, blank=True, on_delete=models.SET_NULL, related_name="deals")
    actor = models.ForeignKey(U, null=True, on_delete=models.PROTECT)
    state = models.CharField(max_length=30, choices=options(["Pending Approval", "Confirmed / Upcoming", "In Progress", "Completed", "Declined"]), default="Pending Approval")
    start = models.DateField(null=True, blank=True)
    deadline = models.DateField(null=True, blank=True)
    due = models.DateField(null=True, blank=True)
    terms = models.TextField(blank=True)
    planned_publish = models.DateField(null=True, blank=True)
    published_at = models.DateField(null=True, blank=True)
    post_url = models.URLField(max_length=2500, blank=True)
    content_confirmed = models.BooleanField(default=False)
    content_confirmed_by = models.ForeignKey(U, null=True, blank=True, on_delete=models.PROTECT, related_name="content_confirmed_deals")
    content_confirmed_at = models.DateTimeField(null=True, blank=True)
    created = models.DateTimeField(default=timezone.now)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    @property
    def paid(self):
        return sum((p.amount for p in self.payments.all() if p.state == "Paid"), Decimal(0))

    @property
    def balance(self):
        return self.quote.amount - self.paid

    @property
    def settlement(self):
        if self.balance == 0:
            return "Paid"
        if self.due and self.due < timezone.localdate():
            return "Overdue"
        return "Partially Paid" if self.paid else "Unpaid"

    @property
    def tier(self):
        return "Cheap" if self.quote.bdt <= 6000 else "Medium" if self.quote.bdt <= 9000 else "Expensive"

    def __str__(self):
        return str(self.quote)

class Payment(models.Model):
    deal = models.ForeignKey(Deal, on_delete=models.PROTECT, related_name="payments")
    actor = models.ForeignKey(U, on_delete=models.PROTECT)
    token = models.UUIDField(unique=True)
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    state = models.CharField(max_length=24, default="Pending Approval")
    due = models.DateField(null=True, blank=True)
    method = models.CharField(max_length=100, blank=True)
    reference = models.CharField(max_length=160, blank=True)
    receipt = models.BinaryField(null=True, editable=False)
    created = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["method", "reference"], condition=Q(state="Paid"), name="unique_paid_reference")]

class Blocker(models.Model):
    staff = models.ForeignKey(U, on_delete=models.PROTECT)
    reviewer = models.ForeignKey(U, null=True, blank=True, on_delete=models.PROTECT, related_name="blockers_to_review")
    profiles = models.ManyToManyField(Profile, blank=True)
    shift = models.ForeignKey(Shift, null=True, on_delete=models.SET_NULL)
    issue = models.CharField(max_length=100, choices=options(BLOCKERS))
    channel = models.CharField(max_length=80, blank=True)
    stage = models.CharField(max_length=40, blank=True)
    link = models.URLField(max_length=2500, blank=True)
    note = models.TextField(blank=True)
    next_action = models.TextField(blank=True)
    state = models.CharField(max_length=24, choices=options(["Open", "In Review", "Resolved", "Withdrawn"]), default="Open")
    created = models.DateTimeField(default=timezone.now)

class Report(models.Model):
    shift = models.OneToOneField(Shift, on_delete=models.PROTECT)
    data = models.JSONField()
    note = models.TextField(blank=True)
    created = models.DateTimeField(default=timezone.now)

class Audit(models.Model):
    actor = models.ForeignKey(U, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=120)
    target = models.CharField(max_length=100)
    data = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(auto_now_add=True)

class ImportBatch(models.Model):
    digest = models.CharField(max_length=64, unique=True)
    filename = models.CharField(max_length=240)
    state = models.CharField(max_length=24, default="Preview")
    actor = models.ForeignKey(U, on_delete=models.PROTECT)
    created = models.DateTimeField(auto_now_add=True)

class SourceRow(models.Model):
    batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT, related_name="rows")
    sheet = models.CharField(max_length=80)
    number = models.PositiveIntegerField()
    raw = models.JSONField()
    result = models.CharField(max_length=32)
    reason = models.TextField(blank=True)
    profile = models.ForeignKey(Profile, null=True, on_delete=models.SET_NULL)
    records = models.JSONField(default=dict, blank=True)

class AssignmentRequest(models.Model):
    creator = models.ForeignKey(Creator, on_delete=models.CASCADE)
    staff = models.ForeignKey(U, on_delete=models.PROTECT)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["creator", "staff"], name="one_assignment_request")]

class LoginThrottle(models.Model):
    key = models.CharField(max_length=64, unique=True)
    attempts = models.PositiveIntegerField(default=0)
    since = models.DateTimeField(default=timezone.now)

class GroupDelivery(models.Model):
    report = models.OneToOneField(Report, on_delete=models.PROTECT)
    actor = models.ForeignKey(U, on_delete=models.PROTECT)
    state = models.CharField(max_length=24, default="Sending")
    message_id = models.CharField(max_length=40, blank=True)
    updated = models.DateTimeField(auto_now=True)
