from pathlib import Path
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db.models import Count
from crm.imports import commit_batch, create_preview

class Command(BaseCommand):
    help = "Preview a workbook or apply it to the selected database."

    def add_arguments(self, parser):
        parser.add_argument("workbook", type=Path)
        parser.add_argument("--admin", required=True)
        parser.add_argument("--commit", action="store_true")

    def handle(self, **options):
        user = get_user_model().objects.get(username=options["admin"], is_superuser=True)
        path = options["workbook"]
        batch = create_preview(path.read_bytes(), path.name, user)
        if options["commit"]:
            commit_batch(batch, user)
        for row in batch.rows.values("sheet", "result").annotate(total=Count("id")).order_by("sheet", "result"):
            self.stdout.write(f"{row['sheet']:28} {row['result']:18} {row['total']:6}")
