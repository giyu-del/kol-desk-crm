import io
import uuid
from unittest.mock import patch
from decimal import Decimal
from datetime import datetime, timedelta

import pyotp
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook

from .imports import commit_batch, create_preview, reverse_batch
from .models import Activity, AssignmentRequest, Blocker, Creator, Deal, GroupDelivery, Member, Payment, Profile, Quote, Report, Shift
from .services import costs, costs_bdt, score_rows, social_url

User = get_user_model()

class CRMTests(TestCase):
    def setUp(self):
        self.staff = self.user("demo-staff", "KOL Staff")
        self.other = self.user("other-staff", "KOL Staff")
        self.manager = self.user("manager", "Manager")
        self.finance = self.user("finance", "Finance")
        self.admin = User.objects.create_superuser("admin", password="only-for-tests")
        self.client.force_login(self.staff)

    def user(self, username, role):
        user = User.objects.create_user(username, password="only-for-tests")
        Member.objects.create(user=user, role=role)
        return user

    def post(self, route, payload=None, *args):
        return self.client.post(reverse(route, args=args), payload or {}, secure=True)

    def get(self, route, *args):
        return self.client.get(reverse(route, args=args), secure=True)

    def profile(self, user=None, key="example"):
        obj = Creator.objects.create(name=key, owner=user or self.staff, added_by=user or self.staff, phone="01712345678")
        return Profile.objects.create(creator=obj, platform="Facebook", url=f"https://facebook.com/{key}",
            canonical=f"facebook.com/{key}", views=Decimal(1000), followers=4000, likes=Decimal(40), shares=Decimal(10))

    def test_engagement_and_percentiles(self):
        p = self.profile()
        self.assertEqual(p.er_followers, Decimal(".0125"))
        self.assertEqual(p.er_views, Decimal(".05"))
        p.views = 0
        self.assertIsNone(p.er_views)
        p.likes, p.shares = 0, None
        self.assertEqual(p.er_followers, 0)
        p.likes = None
        self.assertIsNone(p.er_followers)
        rows = [(1, "Facebook", (100, 50, 10)), (2, "Facebook", (200, 50, 0))]
        self.assertEqual(score_rows(rows, (45, 35, 20)), {1: Decimal("37.5"), 2: Decimal("62.5")})
        rows = [(1, "Facebook", (1, 1, 1, 10)), (2, "Facebook", (2, 2, 2, 20))]
        self.assertEqual(score_rows(rows, (45, 30, 10, 15), (0, 1, 2)), {1: 85, 2: 15})
        rows = [(1, "Facebook", (1, 1, 1, None)), (2, "Facebook", (2, 2, 2, 1))]
        self.assertEqual(score_rows(rows, (35, 25, 25, 15), (0,)), {1: Decimal("41.2"), 2: Decimal("57.5")})

    def test_bdt_rate_columns(self):
        p = self.profile()
        q = Quote.objects.create(profile=p, actor=self.staff, kind="Creator quote",
                                 amount=Decimal("6220"), currency="BDT",
                                 format="Reels", basis="Package", reels=2)
        # Same fixture as the end-to-end test: 6220 BDT / 2 reels = 3110 BDT per reel.
        self.assertEqual(q.unit_bdt, Decimal("3110"))
        self.assertEqual(q.unit_rm, 100)
        self.assertEqual(costs(q), (Decimal(2), Decimal(25), Decimal(100)))
        self.assertEqual(costs_bdt(q), (Decimal("62.2"), Decimal("777.5"), Decimal("3110")))
        # Fallback: a clear Per Reel basis derives a per-Reel price even
        # when the format is not Reels.
        mixed = Quote.objects.create(profile=p, actor=self.staff, kind="Creator quote",
                                     amount=Decimal("5000"), currency="BDT",
                                     format="Mixed", basis="Per Reel")
        self.assertEqual(mixed.unit_bdt, Decimal("5000"))
        self.assertIsNotNone(mixed.unit_rm)
        # An RM quote converts to BDT with the quote's fx rate.
        rm = Quote.objects.create(profile=p, actor=self.staff, kind="Creator quote",
                                  amount=Decimal("100"), currency="RM",
                                  format="Reels", basis="Per Reel")
        self.assertEqual(rm.unit_bdt, Decimal("3110"))
        # No clear basis means no per-unit price, in either currency.
        unclear = Quote.objects.create(profile=p, actor=self.staff, kind="Creator quote",
                                       amount=Decimal("9000"), currency="BDT",
                                       format="Reels", basis="Unclear")
        self.assertIsNone(unclear.unit_bdt)
        self.assertEqual(costs_bdt(unclear), (None, None, None))
        # The comparison page shows the new BDT columns and values.
        page = self.get("quotes")
        self.assertContains(page, "Cost / engagement (BDT)")
        self.assertContains(page, "3110")

    def test_my_followups_queue(self):
        now = timezone.now()
        overdue = Activity.objects.create(profile=self.profile(key="overdue-one"), actor=self.staff,
                                          channel="WhatsApp", outcome="Follow-up Needed",
                                          followup=now - timedelta(days=1))
        today = Activity.objects.create(profile=self.profile(key="today-two"), actor=self.staff,
                                        channel="Call", outcome="Spoke",
                                        followup=now + timedelta(hours=2))
        Activity.objects.create(profile=self.profile(key="later-three"), actor=self.staff,
                                channel="Facebook", outcome="Message Sent",
                                followup=now + timedelta(days=3))
        Activity.objects.create(profile=self.profile(key="done-one"), actor=self.staff,
                                channel="WhatsApp", outcome="Follow-up Needed",
                                followup=now - timedelta(days=2), done=True)
        peer_profile = self.profile(user=self.other, key="peer")
        Activity.objects.create(profile=peer_profile, actor=self.other,
                                channel="WhatsApp", outcome="Follow-up Needed",
                                followup=now - timedelta(days=1))
        page = self.get("my_followups")
        self.assertContains(page, "Overdue")
        self.assertContains(page, "Due today")
        self.assertContains(page, "Upcoming")
        content = page.content.decode()
        self.assertLess(content.index("overdue-one"), content.index("today-two"))
        self.assertLess(content.index("today-two"), content.index("later-three"))
        self.assertNotIn("done-one", content)
        self.assertNotIn("peer", content)
        groups = dict(page.context["queue_groups"])
        self.assertEqual([str(a.profile.creator) for a in groups["Overdue"]], ["overdue-one"])
        self.assertEqual([str(a.profile.creator) for a in groups["Upcoming"]], ["later-three"])
        # Mark done returns to the queue and removes the row.
        self.assertRedirects(self.post("finish_followup", {"next": "my_followups"}, overdue.pk),
                             reverse("my_followups"))
        self.assertTrue(Activity.objects.get(pk=overdue.pk).done)
        self.assertNotIn("overdue-one", self.get("my_followups").content.decode())
        # Snooze moves the due time exactly one day later.
        before = Activity.objects.get(pk=today.pk).followup
        self.post("snooze_followup", {}, today.pk)
        self.assertEqual(Activity.objects.get(pk=today.pk).followup, before + timedelta(days=1))
        self.assertContains(self.get("my_followups"), "today-two")
        # A manager sees the other staff member's follow-ups; Finance may not open the queue.
        self.client.force_login(self.manager)
        self.assertContains(self.get("my_followups"), "peer")
        self.client.force_login(self.finance)
        self.assertEqual(self.get("my_followups").status_code, 403)

    def test_duplicate_link_and_record_access(self):
        p = self.profile(user=self.other, key="c123")
        url = "https://www.facebook.com/c123/reels/?fbclid=abc"
        self.assertEqual(social_url(url)[1], "facebook.com/c123")
        self.assertEqual(self.get("creator", p.creator_id).status_code, 404)
        self.assertEqual(self.post("record", {"outcome": "Message Sent"}, p.pk).status_code, 404)
        self.assertContains(self.post("quick_add", {"url": url}), "Already in CRM")
        self.post("quick_add", {"url": url, "request_assignment": "yes"})
        self.assertEqual(Profile.objects.count(), 1)
        self.assertTrue(AssignmentRequest.objects.filter(staff=self.staff).exists())
        self.assertEqual(self.get("imports").status_code, 403)

    def test_end_to_end(self):
        self.assertEqual(self.get("home").status_code, 200)
        self.post("clock_in")
        self.post("clock_in")
        shift = Shift.objects.get(staff=self.staff)
        self.post("quick_add", {"url": "https://facebook.com/example", "name": "উদাহরণ"})
        p = Profile.objects.get()
        self.post("edit_creator", {"name": "উদাহরণ", "phone": "01712345678", "stage": "New"}, p.creator_id)
        self.post("edit_stats", {"followers": 4000, "views": 1000, "likes": 40, "shares": 10}, p.pk)
        self.profile(key="peer")
        self.assertContains(self.get("creator", p.creator_id), "WhatsApp")
        self.assertContains(self.post("contact", {}, p.pk, "whatsapp"), "Continue outreach")
        self.assertTrue(Activity.objects.get().click)
        self.assertEqual(self.get("report", shift.pk).context["report"]["Creators contacted"], 0)
        self.post("record", {"outcome": "Message Sent", "channel": "WhatsApp"}, p.pk)
        for _ in range(2):
            self.post("add_blocker", {"issue": "WhatsApp account restricted", "profile": p.pk})
        self.assertEqual(Blocker.objects.count(), 1)
        self.assertContains(self.get("negotiation"), "Ready to offer")
        self.assertEqual(self.post("add_quote", {"kind": "Creator quote", "amount": "6220", "currency": "BDT", "format": "Reels", "basis": "Package", "reels": 2}, p.pk).status_code, 302)
        self.post("add_quote", {"kind": "Counteroffer", "amount": "5000", "currency": "BDT", "format": "Reels", "basis": "Package", "reels": 2}, p.pk)
        q = Quote.objects.get(kind="Creator quote")
        self.assertEqual(q.unit_rm, 100)
        self.assertEqual(costs(q), (Decimal(2), Decimal(25), Decimal(100)))
        self.assertContains(self.get("quotes"), "6,220".replace(",", ""))
        self.post("add_deal", {}, q.pk)
        deal = Deal.objects.get()
        self.assertEqual(self.post("deal_state", {"state": "Confirmed / Upcoming"}, deal.pk).status_code, 403)
        self.client.force_login(self.manager)
        self.post("deal_state", {"state": "Confirmed / Upcoming"}, deal.pk)
        self.client.force_login(self.staff)
        token = uuid.uuid4()
        for _ in range(2):
            self.post("request_payment", {"amount": "2220", "token": token}, deal.pk)
        pmt = Payment.objects.get()
        self.assertEqual(self.post("payment_state", {"state": "Approved"}, pmt.pk).status_code, 403)
        self.client.force_login(self.finance)
        self.post("payment_state", {"state": "Approved"}, pmt.pk)
        self.post("payment_state", {"state": "Paid", "reference": "sample", "method": "Bank transfer"}, pmt.pk)
        self.assertEqual(Deal.objects.get().balance, 4000)
        self.client.force_login(self.staff)
        for _ in range(2):
            self.post("clock_out", {"note": "Private sample note"}, shift.pk)
        self.assertEqual(Report.objects.count(), 1)
        self.assertEqual(Report.objects.get().data["Creators contacted"], 1)
        self.assertContains(self.get("report", shift.pk), "Copy Group Report")
        self.assertNotIn("Private sample note", self.get("report", shift.pk).context["group"])
        self.assertEqual(self.client.get(reverse("report", args=[shift.pk]) + "?export=csv", secure=True).status_code, 200)

        pdf = self.client.get(reverse("report", args=[shift.pk]) + "?export=pdf", secure=True)
        self.assertTrue(pdf.content.startswith(b"%PDF"))

    def test_import_preview_commit_repeat_and_reverse(self):
        wb = Workbook()
        sheet = wb.active
        sheet.title = "Master Database"
        sheet.append(["Head"])
        sheet.append([datetime(2026, 11, 20), "demo-staff", "Facebook", "চায়ের গল্প",
                      "https://facebook.com/profile.php?id=123&fbclid=test", "Food", "Bangladesh", "63%", 22, 11, 1])
        sheet = wb.create_sheet("Outreach Pipeline")
        sheet.append(["Header"])
        sheet.append(["Header"])
        sheet.append(["চায়ের গল্প", "Facebook", "Food", "demo-staff", "WhatsApp", datetime(2026, 11, 20), "Yes", None, "Confirm",
                      None, None, None, None, None, None, None, None, "BDT 6,220", "2 Reels"])
        stream = io.BytesIO()
        wb.save(stream)
        content = stream.getvalue()
        batch = create_preview(content, "test.xlsx", self.admin)
        self.assertEqual(Creator.objects.count(), 0)
        commit_batch(batch, self.admin)
        commit_batch(create_preview(content, "repeat.xlsx", self.admin), self.admin)
        self.assertEqual((Profile.objects.count(), Quote.objects.count(), Deal.objects.count()), (1, 1, 1))
        self.assertEqual(Profile.objects.get().bd_audience, Decimal(".63"))
        self.assertEqual(Quote.objects.get().unit_rm, 100)
        reverse_batch(batch, self.admin)
        self.assertEqual((Profile.objects.count(), Quote.objects.count(), Deal.objects.count()), (0, 0, 0))
        self.assertEqual(batch.rows.count(), 2)

    def test_import_phone_dedup_and_preview_counts(self):
        def master_row(name, platform, phone, followers=100, views=1000, likes=10):
            return [datetime(2026, 11, 20), "demo-staff", platform, name, "", "Food", "Bangladesh",
                    None, followers, views, likes, 0, 0, None, None, "New", phone, None]
        wb = Workbook()
        sheet = wb.active
        sheet.title = "Master Database"
        sheet.append(["Head"])
        sheet.append(master_row("Phone One", "Facebook", "01712345678"))
        sheet.append(master_row("Phone Two", "TikTok", "01712-345678", followers=50, views=500, likes=5))
        sheet.append(master_row("Phone Three", "Facebook", "01812345678", followers=80, views=800, likes=8))
        stream = io.BytesIO()
        wb.save(stream)
        batch = create_preview(stream.getvalue(), "phones.xlsx", self.admin)
        rows = list(batch.rows.order_by("number"))
        self.assertEqual([r.result for r in rows], ["New", "Pending review", "New"])
        self.assertIn("Duplicate of an earlier row", rows[1].reason)
        self.client.force_login(self.admin)
        self.assertContains(self.get("import_detail", batch.pk), "Duplicates in this file")
        commit_batch(batch, self.admin)
        self.assertEqual(Creator.objects.count(), 2)
        self.assertEqual(Profile.objects.count(), 2)
        creator_a = Creator.objects.get(name="Phone One")
        self.assertEqual(batch.rows.get(number=3).profile.creator, creator_a)
        # A later file with the same phone but a new name matches the saved creator instead of creating one.
        wb2 = Workbook()
        sheet2 = wb2.active
        sheet2.title = "Master Database"
        sheet2.append(["Head"])
        sheet2.append(master_row("Renamed", "YouTube", "01712345678"))
        stream2 = io.BytesIO()
        wb2.save(stream2)
        batch2 = create_preview(stream2.getvalue(), "renamed.xlsx", self.admin)
        renamed = batch2.rows.get()
        self.assertEqual(renamed.profile.creator, creator_a)
        self.assertNotEqual(renamed.result, "New")
        commit_batch(batch2, self.admin)
        self.assertEqual(Creator.objects.count(), 2)
        # One phone shared by two saved creators stays ambiguous instead of picking one.
        for platform in ("Facebook", "Instagram"):
            creator = Creator.objects.create(name="Shared " + platform, phone="01799999999")
            Profile.objects.create(creator=creator, platform=platform)
        wb3 = Workbook()
        sheet3 = wb3.active
        sheet3.title = "Master Database"
        sheet3.append(["Head"])
        sheet3.append(master_row("Shared Phone", "TikTok", "01799999999", followers=10, views=100, likes=1))
        stream3 = io.BytesIO()
        wb3.save(stream3)
        batch3 = create_preview(stream3.getvalue(), "shared.xlsx", self.admin)
        shared = batch3.rows.get()
        self.assertEqual(shared.result, "Pending review")
        self.assertIn("Multiple possible matches", shared.reason)
        commit_batch(batch3, self.admin)
        self.assertEqual(Creator.objects.count(), 4)

    def test_privileged_login_requires_mfa(self):
        self.client.logout()
        member = Member.objects.create(user=self.admin, otp_secret=pyotp.random_base32())
        self.assertEqual(self.post("login", {"username": "admin", "password": "only-for-tests"}).status_code, 200)
        code = pyotp.TOTP(member.otp_secret).now()
        self.assertEqual(self.post("login", {"username": "admin", "password": "only-for-tests", "otp": code}).status_code, 302)
        self.client.logout()
        self.assertEqual(self.post("login", {"username": "admin", "password": "only-for-tests", "otp": code}).status_code, 200)

    @patch("crm.group_delivery.destination", return_value=("dummy-token", "dummy-group"))
    @patch("crm.group_delivery.deliver", return_value=("Sent", "42"))
    def test_group_send_requires_confirmation_and_is_idempotent(self, deliver, destination):
        self.post("clock_in")
        shift = Shift.objects.get(staff=self.staff)
        self.post("clock_out", {"note": "Sensitive private note"}, shift.pk)
        self.assertEqual(self.post("send_report", {}, shift.pk).status_code, 403)
        self.post("send_report", {"confirm": "yes"}, shift.pk)
        self.post("send_report", {"confirm": "yes"}, shift.pk)
        self.assertEqual(GroupDelivery.objects.get().state, "Sent")
        self.assertEqual(deliver.call_count, 1)
        self.assertNotIn("Sensitive private note", deliver.call_args.args[0])
