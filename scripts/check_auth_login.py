"""Isolated authentication regression checks; never connects to the app database."""
import os
import sys
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from django.conf import settings

settings.configure(
    SECRET_KEY="isolated-test", USE_TZ=True,
    INSTALLED_APPS=["django.contrib.auth", "django.contrib.contenttypes", "api", "rest_framework.authtoken"],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    AUTH_USER_MODEL="api.PriceUser",
    AUTHENTICATION_BACKENDS=["api.auth_backends.UsernameOrEmailBackend"],
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    REST_FRAMEWORK={"DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.TokenAuthentication"]},
)
import django
django.setup()
from django.contrib.auth import authenticate, get_user_model
from django.contrib.admin.forms import AdminAuthenticationForm
from django.core.management import call_command
from django.db import connection
from django.test import RequestFactory
from rest_framework.test import APIRequestFactory, force_authenticate
from api.user_management import UserManagementView
from rest_framework.authtoken.models import Token

User = get_user_model()
with connection.schema_editor() as editor:
    editor.create_model(User)
    editor.create_model(Token)


class LoginTests(TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ, {"DJANGO_SUPERUSER_PASSWORD": "test-secret"}), patch("django.core.management.base.BaseCommand.check_migrations"):
            call_command("createsuperuser", username="Admin", email="admin@example.test", interactive=False, verbosity=0)
        User.objects.create_user(username="admin@example.test", email="admin@example.test", password="other-secret", is_approved=True)
        User.objects.create_user(username="pending", email="pending@example.test", password="test-secret")

    def test_command_sets_application_fields(self):
        user = User.objects.get(username="Admin")
        self.assertTrue(user.is_staff and user.is_active and user.is_superuser and user.is_approved)
        self.assertEqual(user.role, "admin")

    def test_frontend_email_and_employee_code(self):
        user = User.objects.create_user(username="employee", email="employee@example.test", employee_code="EMP001", password="test-secret", is_approved=True)
        for identifier in ("EMP001", " emp001 ", "EMPLOYEE@example.test"):
            self.assertEqual(authenticate(login_identifier=identifier, password="test-secret").pk, user.pk)
        self.assertIsNone(authenticate(login_identifier="employee", password="test-secret"))
        self.assertIsNone(authenticate(login_identifier="EMP001", password="wrong"))

    def test_frontend_duplicate_email_rejected(self):
        self.assertIsNone(authenticate(login_identifier="admin@example.test", password="test-secret"))

    def test_frontend_pending_and_disabled_rejected(self):
        self.assertIsNone(authenticate(login_identifier="pending@example.test", password="test-secret"))
        user = User.objects.create_superuser(username="inactive-api", email="inactive@example.test", password="test-secret")
        user.is_active = False
        user.save()
        self.assertIsNone(authenticate(login_identifier="inactive@example.test", password="test-secret"))

    def test_superuser_username_authenticates(self):
        self.assertEqual(authenticate(username="Admin", password="test-secret").username, "Admin")

    def test_admin_form_accepts_superuser(self):
        form = AdminAuthenticationForm(RequestFactory().get("/admin/"), data={"username": "Admin", "password": "test-secret"})
        self.assertTrue(form.is_valid(), form.errors)

    def test_collision_does_not_select_another_account(self):
        self.assertIsNone(authenticate(username="admin@example.test", password="test-secret"))

    def test_wrong_password_and_pending_account_rejected(self):
        self.assertIsNone(authenticate(username="Admin", password="wrong"))
        self.assertIsNone(authenticate(username="pending", password="test-secret"))

    def test_unique_email_login(self):
        User.objects.create_user(username="unique", email="unique@example.test", password="test-secret", is_approved=True)
        self.assertEqual(authenticate(username="UNIQUE@example.test", password="test-secret").username, "unique")

    def test_disabled_superuser_stays_disabled(self):
        user = User.objects.create_superuser(username="disabled", password="test-secret")
        user.is_active = False
        user.save()
        self.assertIsNone(authenticate(username="disabled", password="test-secret"))


class ManagementTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = User.objects.create_superuser(username="management-admin", password="test-secret")
        cls.manager = User.objects.create_user(username="management-manager", role="manager", is_approved=True)
        cls.user = User.objects.create_user(username="management-user", role="user", is_approved=True)

    def request(self, actor, method, data=None, target=None):
        request = getattr(APIRequestFactory(), method)("/auth/users/", data or {}, format="json")
        force_authenticate(request, user=actor)
        return UserManagementView.as_view()(request, **({"user_id": target.pk} if target else {}))

    def test_user_cannot_access_management(self):
        for method in ("get", "post", "patch"):
            response = self.request(self.user, method, {"role": "admin"}, self.manager if method == "patch" else None)
            self.assertEqual(response.status_code, 403)

    def test_token_header_list_and_create(self):
        token, _ = Token.objects.get_or_create(user=self.admin)
        factory = APIRequestFactory()
        bad = factory.get('/auth/users/', HTTP_AUTHORIZATION=f'Bearer {token.key}')
        self.assertEqual(UserManagementView.as_view()(bad).status_code, 401)
        request = factory.get('/auth/users/', HTTP_AUTHORIZATION=f'Token {token.key}')
        response = UserManagementView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        self.assertIn(str(self.admin.pk), [u['id'] for u in response.data['data']])
        request = factory.post('/auth/users/', dict(full_name='Token Test', email='token-test@example.test', employee_id='TOKEN-TEST', role='user', password='test-secret', confirm_password='test-secret'), format='json', HTTP_AUTHORIZATION=f'Token {token.key}')
        response = UserManagementView.as_view()(request)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data['data']['is_approved'])

    def test_manager_list_hides_admin_and_superusers(self):
        response = self.request(self.manager, "get")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(u["role"] != "admin" and not u["is_superuser"] for u in response.data["data"]))
        self.assertIn(str(self.admin.pk), [u["id"] for u in self.request(self.admin, "get").data["data"]])

    def test_manager_cannot_modify_admin_or_promote_to_admin(self):
        self.assertEqual(self.request(self.manager, "patch", {"role": "user"}, self.admin).status_code, 403)
        self.assertEqual(self.request(self.manager, "patch", {"role": "admin"}, self.user).status_code, 403)

    def test_manager_can_edit_role_without_approving(self):
        target = User.objects.create_user(username="pending-management", is_active=False)
        self.assertEqual(self.request(self.manager, "patch", {"role": "manager"}, target).status_code, 200)
        target.refresh_from_db()
        self.assertEqual(target.role, "manager")
        self.assertFalse(target.is_approved)
        self.assertEqual(self.request(self.manager, "patch", {"is_approved": True}, target).status_code, 200)

    def test_only_role_and_approval_editable(self):
        self.assertEqual(self.request(self.admin, "patch", {"email": "new@example.test"}, self.user).status_code, 400)
        self.assertEqual(self.request(self.admin, "patch", {"is_approved": "false"}, self.user).status_code, 400)

    def test_rejected_registration_can_be_accepted_again(self):
        target = User.objects.create_user(username="rejected-user", email="rejected@example.test", password="test-secret", is_active=False)
        response = self.request(self.manager, "patch", {"is_approved": False}, target)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['data']['is_rejected'])
        self.assertFalse(response.data['data']['is_active'])
        self.assertIsNone(authenticate(login_identifier="rejected@example.test", password="test-secret"))
        self.assertEqual(self.request(self.manager, "patch", {"is_approved": True}, target).status_code, 200)
        target.refresh_from_db()
        self.assertFalse(target.is_rejected)
        self.assertIsNotNone(authenticate(login_identifier="rejected@example.test", password="test-secret"))

    def test_approved_account_can_be_rejected_without_removal(self):
        target = User.objects.create_user(username='accepted-target', is_approved=True)
        token = Token.objects.create(user=target)
        self.assertEqual(self.request(self.admin, "patch", {"is_approved": False}, target).status_code, 200)
        target.refresh_from_db()
        self.assertFalse(target.is_approved)
        self.assertTrue(target.is_rejected)
        self.assertFalse(Token.objects.filter(key=token.key).exists())
        self.assertIn(str(target.pk), [u['id'] for u in self.request(self.admin, 'get').data['data']])

    def test_delete_removes_from_list_and_blocks_login(self):
        target = User.objects.create_user(username='delete-target', email='delete@example.test', is_approved=True, password='test-secret')
        token = Token.objects.create(user=target)
        self.assertEqual(self.request(self.manager, 'delete', target=target).status_code, 200)
        target.refresh_from_db()
        self.assertTrue(target.is_deleted)
        self.assertFalse(target.is_active)
        self.assertFalse(Token.objects.filter(key=token.key).exists())
        self.assertNotIn(str(target.pk), [u['id'] for u in self.request(self.admin, 'get').data['data']])
        self.assertIsNone(authenticate(login_identifier='delete@example.test', password='test-secret'))
        self.assertEqual(self.request(self.admin, 'patch', {'is_approved': True}, target).status_code, 404)

    def test_delete_permission_boundaries(self):
        self.assertEqual(self.request(self.user, 'delete', target=self.manager).status_code, 403)
        self.assertEqual(self.request(self.manager, 'delete', target=self.admin).status_code, 403)
        self.assertEqual(self.request(self.admin, 'delete', target=self.admin).status_code, 403)

    def test_add_user_is_approved_and_manager_cannot_create_admin(self):
        data = dict(full_name="New Colleague", email="colleague@example.test", employee_id="EMP-MGMT", role="manager", password="test-secret", confirm_password="test-secret")
        response = self.request(self.manager, "post", data)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["data"]["is_approved"])
        self.assertTrue(response.data["data"]["is_active"])
        self.assertIsNotNone(authenticate(login_identifier="EMP-MGMT", password="test-secret"))
        data.update(email="not-admin@example.test", employee_id="EMP-NO", role="admin")
        self.assertEqual(self.request(self.manager, "post", data).status_code, 403)


if __name__ == "__main__":
    main()
