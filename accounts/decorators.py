from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied


def _check(request, test):
    user = request.user
    if not user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    if not test(user):
        raise PermissionDenied("You do not have access to this screen.")
    return None


def admin_required(view):
    """Super admin or admin only."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        resp = _check(request, lambda u: u.is_admin_level)
        return resp or view(request, *args, **kwargs)
    return wrapper


def superadmin_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        resp = _check(request, lambda u: u.is_superadmin)
        return resp or view(request, *args, **kwargs)
    return wrapper


def staff_required(view):
    """Admin level or a teacher — teachers get filtered to their own sections."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        resp = _check(request, lambda u: u.is_admin_level or u.is_teacher)
        return resp or view(request, *args, **kwargs)
    return wrapper


def login_required_any(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        resp = _check(request, lambda u: True)
        return resp or view(request, *args, **kwargs)
    return wrapper
