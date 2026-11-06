from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model
from api.models import UserRole


class Command(BaseCommand):
    help = "Create or approve the first Priceora admin account."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--email", required=True)
        parser.add_argument("--employee-id", required=True)
        parser.add_argument("--password", required=True)

    def handle(self, *args, **options):
        User = get_user_model()
        email = options["email"].strip().lower()
        if User.objects.filter(username=email).exists():
            auth_user = User.objects.get(username=email)
            auth_user.set_password(options["password"])
            auth_user.is_active = True
            auth_user.save()
        else:
            auth_user = User.objects.create_user(username=email, email=email, password=options["password"], first_name=options["name"])
        auth_user.full_name = options["name"]
        auth_user.email = email
        auth_user.employee_code = options["employee_id"]
        auth_user.role = UserRole.ADMIN
        auth_user.requested_role = UserRole.ADMIN
        auth_user.is_active = True
        auth_user.is_approved = True
        auth_user.is_staff = True
        auth_user.is_superuser = True
        auth_user.save()
        self.stdout.write(self.style.SUCCESS(f"Admin ready: {email}"))
