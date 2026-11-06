from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q


class UsernameOrEmailBackend(ModelBackend):
    """Use the same unambiguous account lookup for admin and API login."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        User = get_user_model()
        frontend_login = "login_identifier" in kwargs
        identifier = str(kwargs.get("login_identifier") if frontend_login else (username or kwargs.get(User.USERNAME_FIELD) or "")).strip()
        if not identifier or password is None:
            return None
        if frontend_login:
            matches = list(User._default_manager.filter(
                Q(email__iexact=identifier) | Q(employee_code__iexact=identifier)
            )[:2])
            user = matches[0] if len(matches) == 1 else None
        else:
            user = User._default_manager.filter(username=identifier).first()
        if user is None and not frontend_login:
            matches = list(User._default_manager.filter(email__iexact=identifier)[:2])
            user = matches[0] if len(matches) == 1 else None
        if user is None:
            User().set_password(password)
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None

    def user_can_authenticate(self, user):
        return not user.is_deleted and super().user_can_authenticate(user) and (user.is_superuser or user.is_approved)
