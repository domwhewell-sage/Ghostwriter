# Standard Libraries
import logging
from datetime import datetime

# Django Imports
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import AccessMixin
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Q
from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse
from django.shortcuts import resolve_url

# 3rd Party Libraries
import jwt
from urllib.parse import urlparse

# Ghostwriter Libraries
from ghostwriter.oplog.models import Oplog
from ghostwriter.reporting.models import Archive, Report
from ghostwriter.rolodex.models import (
    Client,
    ClientInvite,
    Project,
    ProjectAssignment,
    ProjectInvite,
)

# Using __name__ resolves to ghostwriter.utils
logger = logging.getLogger(__name__)

User = get_user_model()


def get_jwt_from_request(request):
    """
    Fetch the JSON Web Token from a ``request`` object's ``META`` attribute. The
    token is in the ``Authorization`` header with the ``Bearer `` prefix.

    **Parameters**

    ``request``
        Django ``request`` object
    """
    return request.META.get("HTTP_AUTHORIZATION", " ").split(" ")[1]


def jwt_encode(payload):
    """
    Encode a JWT token.

    **Parameters**

    ``payload``
        Plaintext JWT payload to be signed
    """
    return jwt.encode(
        payload,
        settings.GRAPHQL_JWT["JWT_SECRET_KEY"],
        settings.GRAPHQL_JWT["JWT_ALGORITHM"],
    )


def jwt_decode(token):
    """
    Decode a JWT token.

    **Parameters**

    ``token``
        Encoded JWT payload
    """
    return jwt.decode(
        token,
        settings.GRAPHQL_JWT["JWT_SECRET_KEY"],
        options={
            "verify_exp": settings.GRAPHQL_JWT["JWT_VERIFY_EXPIRATION"],
            "verify_aud": settings.GRAPHQL_JWT["JWT_AUDIENCE"],
            "verify_signature": settings.GRAPHQL_JWT["JWT_VERIFY"],
        },
        leeway=10,
        audience=settings.GRAPHQL_JWT["JWT_AUDIENCE"],
        issuer=None,
        algorithms=[settings.GRAPHQL_JWT["JWT_ALGORITHM"]],
    )


def jwt_decode_no_verification(token):
    """
    Decode a JWT token without verifying anything. Used for logs and trusted
    :model:`api:APIKey` entries.

    **Parameters**

    ``token``
        Encoded JWT payload from ``generate_jwt``
    """
    return jwt.decode(
        token,
        settings.GRAPHQL_JWT["JWT_SECRET_KEY"],
        options={
            "verify_exp": False,
            "verify_aud": False,
            "verify_signature": False,
        },
        leeway=10,
        audience=settings.GRAPHQL_JWT["JWT_AUDIENCE"],
        issuer=None,
        algorithms=[settings.GRAPHQL_JWT["JWT_ALGORITHM"]],
    )


def get_jwt_payload(token):
    """
    Attempt to decode and verify the JWT token and return the payload.

    **Parameters**

    ``token``
        Encoded JWT payload to be decoded
    """
    try:
        payload = jwt_decode(token)
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, jwt.DecodeError) as exception:
        try:
            bad_token = jwt_decode_no_verification(token)
            logger.warning("%s error (%s) with this payload: %s", type(exception).__name__, exception, bad_token)
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, jwt.DecodeError) as verify_exception:
            logger.error("%s error with this payload: %s", verify_exception, token)
        payload = None
    return payload


def generate_jwt(user, exp=None):
    """
    Generate a JWT token for the user. The token will expire after the
    ``JWT_EXPIRATION_DELTA`` setting unless the ``exp`` parameter is set.

    **Parameters**

    ``user``
        The :model:`users.User` object for the token
    ``exp``
        The expiration timestamp for the token
    ``exclude_hasura``
        If ``True``, the token will not contain the Hasura claims
    """
    jwt_iat = datetime.utcnow()
    if exp:
        jwt_expires = exp
    else:
        jwt_datetime = jwt_iat + settings.GRAPHQL_JWT["JWT_EXPIRATION_DELTA"]
        jwt_expires = int(jwt_datetime.timestamp())

    payload = {
        "sub": str(user.id),
        "sub_name": user.username,
        "sub_email": user.email,
        "aud": settings.GRAPHQL_JWT["JWT_AUDIENCE"],
        "iat": jwt_iat.timestamp(),
        "exp": jwt_expires,
    }

    return payload, jwt_encode(payload)


def generate_hasura_error_payload(error_message, error_code):
    """
    Generate a standard error payload for Hasura.

    Ref: https://hasura.io/docs/latest/graphql/core/actions/action-handlers.html

    **Parameters**

    ``error_message``
        Error message to be returned
    ``error_code``
        Error code to be returned
    """
    return {
        "message": error_message,
        "extensions": {
            "code": error_code,
        },
    }


def verify_graphql_request(headers):
    """
    Verify that the request is a valid request from Hasura using the
    ``HASURA_ACTION_SECRET`` secret shared between Django and Hasura.

    **Parameters**

    ``headers``
        Headers from a Django ``request`` object
    """
    hasura_action_secret = headers.get("Hasura-Action-Secret")
    if hasura_action_secret is None:
        return False

    if hasura_action_secret == settings.HASURA_ACTION_SECRET:
        return True
    return False


def get_user_from_token(token):
    """
    Get the user from the JWT token.

    **Parameters**

    ``token``
        Decoded JWT payload
    """
    user_obj = User.objects.get(id=token["sub"])
    return user_obj


def verify_user_is_privileged(user):
    """
    Verify that the user holds a privileged role or the ``is_staff`` flag.

    **Parameters**

    ``user``
        The :model:`users.User` object
    """
    if user.role in (
        "admin",
        "manager",
    ):
        return True
    return user.is_staff


def verify_project_access(user, project):
    """
    Verify that the user has access to the project.

    **Parameters**

    ``user``
        The :model:`users.User` object
    ``project``
        The :model:`rolodex.Project` object
    """
    logger.info("Verifying project access for %s with %s role", user, user.role)
    if user.role in ("admin", "manager") or user.is_staff:
        return True

    assignments = ProjectAssignment.objects.filter(operator=user, project=project)
    client_invites = ClientInvite.objects.filter(user=user, client=project.client)
    project_invites = ProjectInvite.objects.filter(user=user, project=project)
    if any([assignments, client_invites, project_invites]):
        return True
    return False


def verify_client_access(user, client):
    """
    Verify that the user has access to the client.

    **Parameters**

    ``user``
        The :model:`users.User` object
    ``client``
        The :model:`rolodex.Client` object
    """
    if user.role in ("admin", "manager") or user.is_staff:
        return True

    assignments = ProjectAssignment.objects.filter(operator=user, project__client=client)
    client_invites = ClientInvite.objects.filter(user=user, client=client)
    project_invites = ProjectInvite.objects.filter(user=user, project__client=client)
    if any([assignments, client_invites, project_invites]):
        return True
    return False


def verify_finding_access(user, mode):
    """
    Verify that the user is flagged as being able to create and/or edit findings in the global library.

    **Parameters**

    ``user``
        The :model:`users.User` object

    ``mode``
        The mode to check for. Valid values are ``create`` and ``edit``.
    """
    if user.role in ("admin", "manager") or user.is_staff:
        return True

    if mode == "create" and user.enable_finding_create:
        return True

    elif mode == "edit" and user.enable_finding_edit:
        return True

    elif mode == "delete" and user.enable_finding_delete:
        return True

    return False


def get_client_list(user):
    """
    Retrieve a filtered list of :model:`rolodex.Client` entries based on the user's role.

    Privileged users will receive all entries. Non-privileged users will receive only those entries to which they
    have access.

    **Parameters**

    ``user``
        The :model:`users.User` object
    """
    if verify_user_is_privileged(user):
        clients = Client.objects.all().order_by("name")
    else:
        clients = (
            Client.objects.filter(
                Q(clientinvite__user=user)
                | Q(project__projectinvite__user=user)
                | Q(project__projectassignment__operator=user)
            )
            .order_by("name")
            .distinct()
        )
    return clients


def get_project_list(user):
    """
    Retrieve a filtered list of :model:`rolodex.Project` entries based on the user's role.

    Privileged users will receive all entries. Non-privileged users will receive only those entries to which they
    have access.

    **Parameters**

    ``user``
        The :model:`users.User` object
    """
    if verify_user_is_privileged(user):
        projects = Project.objects.select_related("client").all().order_by("complete", "client")
    else:
        projects = (
            Project.objects.select_related("client")
            .filter(
                Q(projectinvite__user=user) | Q(client__clientinvite__user=user) | Q(projectassignment__operator=user)
            )
            .order_by("complete", "client")
            .distinct()
        )
    return projects


def get_logs_list(user):
    """
    Retrieve a filtered list of :model:`oplog.Oplog` entries based on the user's role.

    Privileged users will receive all logs. Non-privileged users will receive only those entries to which they
    have access.

    **Parameters**

    ``user``
        The :model:`users.User` object
    """
    if verify_user_is_privileged(user):
        logs = Oplog.objects.select_related("project").all()
    else:
        logs = (
            Oplog.objects.select_related("project")
            .filter(
                Q(project__projectinvite__user=user)
                | Q(project__client__clientinvite__user=user)
                | Q(project__projectassignment__operator=user)
            )
            .distinct()
        )
    return logs


def get_reports_list(user):
    """
    Retrieve a filtered list of :model:`reporting.Report` entries based on the user's role.

    Privileged users will receive all reports. Non-privileged users will receive only those reports to which they
    have access.

    **Parameters**

    ``user``
        The :model:`users.User` object
    """
    if verify_user_is_privileged(user):
        reports = Report.objects.select_related("created_by").all().order_by("complete", "title")
    else:
        reports = (
            Report.objects.select_related("created_by")
            .filter(
                Q(project__projectinvite__user=user)
                | Q(project__client__clientinvite__user=user)
                | Q(project__projectassignment__operator=user)
            )
            .order_by("complete", "title")
            .distinct()
        )
    return reports


def get_archives_list(user):
    """
    Retrieve a filtered list of :model:`reporting.Archive` entries based on the user's role.

    Privileged users will receive all archives. Non-privileged users will receive only those archives to which they
    have access.

    **Parameters**

    ``user``
        The :model:`users.User` object
    """
    if verify_user_is_privileged(user):
        archives = Archive.objects.select_related("project__client").all().order_by("project__client")
    else:
        archives = (
            Archive.objects.select_related("project__client")
            .filter(
                Q(project__projectinvite__user=user)
                | Q(project__client__clientinvite__user=user)
                | Q(project__projectassignment__operator=user)
            )
            .order_by("project__client")
            .distinct()
        )
    return archives


class ForbiddenJsonResponse(JsonResponse):
    """
    A  custom JSON response class with a static 403 status code and default error message.
    """

    status_code = 403

    def __init__(self, data=None, encoder=DjangoJSONEncoder, safe=True, json_dumps_params=None, **kwargs):
        if data is None:
            data = {"result": "error", "message": "Ah ah ah! You didn't say the magic word!"}
        super().__init__(data, encoder, safe, json_dumps_params, **kwargs)


class RoleBasedAccessControlMixin(AccessMixin):
    """
    Verify the current user is authenticated. If authenticated, deny a request with a permission error if the
    ``test_func()`` method returns ``False``.

    This class uses the same logic as Django's built-in ``LoginRequiredMixin`` and ``UserPassesTestMixin`` classes.
    """

    def dispatch(self, request, *args, **kwargs):
        # Test for authentication
        if not request.user.is_authenticated:
            return self.handle_not_authenticated()

        # Check the permissions
        user_test_result = self.get_test_func()()
        if not user_test_result:
            return self.handle_no_permission()

        # Continue with the request
        return super().dispatch(request, *args, **kwargs)

    def handle_not_authenticated(self):
        if self.raise_exception or self.request.user.is_authenticated:
            raise PermissionDenied(self.get_permission_denied_message())
        path = self.request.build_absolute_uri()
        resolved_login_url = resolve_url(self.get_login_url())
        # If the login url is the same scheme and net location then use the path as the "next" url
        login_scheme, login_netloc = urlparse(resolved_login_url)[:2]
        current_scheme, current_netloc = urlparse(path)[:2]
        if (
            (not login_scheme or login_scheme == current_scheme) and
            (not login_netloc or login_netloc == current_netloc)
        ):
            path = self.request.get_full_path()
        return redirect_to_login(
            path,
            resolved_login_url,
            self.get_redirect_field_name(),
        )

    def test_func(self):
        """Override this method to use a different ``test_func`` method."""
        return self.request.user.is_active

    def get_test_func(self):
        """Override this method to use a different ``test_func`` method."""
        return self.test_func

