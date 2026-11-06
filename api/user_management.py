from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.authtoken.models import Token
from api.models import PriceUser, UserRole


def role_of(user):
    return UserRole.ADMIN if user.is_superuser else user.role


def user_data(user):
    return {"id": str(user.pk), "name": user.full_name or user.get_full_name() or user.username,
            "email": user.email, "employee_id": user.employee_code, "role": role_of(user),
            "is_superuser": user.is_superuser, "is_active": user.is_active,
            "is_rejected": user.is_rejected,
            "is_approved": user.is_approved or user.is_superuser}


class NewUserSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=255)
    email = serializers.EmailField(max_length=150)
    employee_id = serializers.CharField(max_length=100)
    role = serializers.ChoiceField(choices=UserRole.choices, default=UserRole.USER)
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    confirm_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, data):
        data["email"] = data["email"].lower()
        if data["password"] != data["confirm_password"]:
            raise serializers.ValidationError("Passwords do not match.")
        if PriceUser.objects.filter(Q(email__iexact=data["email"]) | Q(username__iexact=data["email"])).exists():
            raise serializers.ValidationError("Email is already registered.")
        if PriceUser.objects.filter(employee_code__iexact=data["employee_id"]).exists():
            raise serializers.ValidationError("Employee code is already registered.")
        try:
            validate_password(data["password"], PriceUser(username=data["email"], email=data["email"], full_name=data["full_name"]))
        except ValidationError as exc:
            raise serializers.ValidationError({"password": exc.messages})
        return data


class UserManagementView(APIView):
    permission_classes = [IsAuthenticated]

    def permitted(self, request):
        return not request.user.is_deleted and request.user.is_active and (request.user.is_superuser or request.user.is_approved) and role_of(request.user) in {UserRole.ADMIN, UserRole.MANAGER}

    def denied(self):
        return Response({"success": False, "message": "You are not allowed to manage this account."}, status=403)

    def get(self, request):
        if not self.permitted(request):
            return self.denied()
        users = PriceUser.objects.filter(is_deleted=False).exclude(pk=request.user.pk).order_by("is_approved", "full_name", "username")
        if role_of(request.user) == UserRole.MANAGER:
            users = users.exclude(Q(role=UserRole.ADMIN) | Q(is_superuser=True))
        return Response({"success": True, "data": [user_data(u) for u in users]})

    def post(self, request):
        if not self.permitted(request):
            return self.denied()
        serializer = NewUserSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({"success": False, "message": "Check the entered user details.", "errors": serializer.errors}, status=400)
        data = serializer.validated_data
        if data["role"] == UserRole.ADMIN and role_of(request.user) != UserRole.ADMIN:
            return self.denied()
        try:
            with transaction.atomic():
                user = PriceUser.objects.create_user(
                    username=data["email"], email=data["email"], full_name=data["full_name"],
                    employee_code=data["employee_id"], password=data["password"],
                    role=data["role"], requested_role=data["role"], is_active=True, is_approved=True,
                )
        except IntegrityError:
            return Response({"success": False, "message": "Email or employee code is already registered."}, status=400)
        return Response({"success": True, "data": user_data(user), "message": "User added and approved."}, status=201)

    def patch(self, request, user_id):
        if not self.permitted(request):
            return self.denied()
        with transaction.atomic():
            target = get_object_or_404(PriceUser.objects.select_for_update(), pk=user_id, is_deleted=False)
            actor_role = role_of(request.user)
            if actor_role == UserRole.MANAGER and role_of(target) == UserRole.ADMIN:
                return self.denied()
            if not request.data or set(request.data) - {"role", "is_approved"}:
                return Response({"success": False, "message": "Only role and approval may be changed."}, status=400)
            fields = []
            if "role" in request.data:
                role = request.data["role"]
                if role not in UserRole.values:
                    return Response({"success": False, "message": "Invalid role."}, status=400)
                if (actor_role == UserRole.MANAGER and role == UserRole.ADMIN) or target.is_superuser or target.pk == request.user.pk:
                    return self.denied()
                target.role = role
                fields.append("role")
            if "is_approved" in request.data:
                approved = request.data["is_approved"]
                if type(approved) is not bool:
                    return Response({"success": False, "message": "Approval must be true or false."}, status=400)
                if target.is_superuser or target.pk == request.user.pk:
                    return self.denied()
                target.is_approved = approved
                target.is_active = approved
                target.is_rejected = not approved
                fields += ["is_approved", "is_active", "is_rejected"]
            target.save(update_fields=fields)
            if not target.is_active:
                Token.objects.filter(user=target).delete()
        return Response({"success": True, "data": user_data(target), "message": "User updated."})

    def delete(self, request, user_id):
        if not self.permitted(request):
            return self.denied()
        with transaction.atomic():
            target = get_object_or_404(PriceUser.objects.select_for_update(), pk=user_id, is_deleted=False)
            if (target.is_superuser or target.pk == request.user.pk
                    or (role_of(request.user) == UserRole.MANAGER and role_of(target) == UserRole.ADMIN)):
                return self.denied()
            # Preserve historical ownership/audit records while removing the
            # account from user management and preventing any further login.
            target.is_deleted = True
            target.is_active = False
            target.save(update_fields=["is_deleted", "is_active"])
            Token.objects.filter(user=target).delete()
        return Response({"success": True, "message": "User removed."})
