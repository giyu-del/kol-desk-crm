from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from crm.models import Creator, Member, Profile

class Command(BaseCommand):
    help = "Create synthetic staff and profiles for a local demonstration."

    def add_arguments(self, parser):
        parser.add_argument("--password", required=True)

    def handle(self, **kwargs):
        if not settings.DEBUG:
            raise CommandError("Demo data requires CRM_DEBUG=1.")
        User = get_user_model()
        staff, _ = User.objects.get_or_create(username="demo-staff")
        staff.set_password(kwargs["password"])
        staff.save()
        Member.objects.update_or_create(user=staff, defaults={"role": "KOL Staff"})
        for name, niche, handle, followers, views, likes in [
            ("Dhaka Food Notes", "Food", "demo-food-notes", 45000, 8500, 360),
            ("Weekend Frame", "Travel", "demo-weekend-frame", 63000, 16000, 780),
            ("চায়ের গল্প", "Comedy", "demo-tea-stories", 98000, 26000, 1600),
        ]:
            key = "facebook.com/" + handle
            if not Profile.objects.filter(canonical=key).exists():
                creator = Creator.objects.create(name=name, niche=niche, owner=staff, added_by=staff)
                Profile.objects.create(creator=creator, platform="Facebook", url="https://" + key, canonical=key, followers=followers, views=views, likes=likes)
