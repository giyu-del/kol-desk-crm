import pyotp
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from crm.models import Member
from crm.services import audit

class Command(BaseCommand):
    help = "Enroll or rotate a user's authenticator secret from a private terminal."

    def add_arguments(self, parser):
        parser.add_argument("username")

    @transaction.atomic
    def handle(self, **options):
        user = get_user_model().objects.get(username=options["username"])
        member, _ = Member.objects.select_for_update().get_or_create(user=user)
        member.otp_secret = pyotp.random_base32()
        member.otp_counter = -1
        member.save()
        from django.contrib.sessions.models import Session
        keys = [session.pk for session in Session.objects.all() if session.get_decoded().get("_auth_user_id") == str(user.pk)]
        Session.objects.filter(pk__in=keys).delete()
        audit(None, "Authenticator rotated", user.pk)
        self.stdout.write("Add this key manually to your authenticator app: " + member.otp_secret)
