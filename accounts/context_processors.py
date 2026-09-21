def navigation(request):
    """Expose role flags to every template so the nav can be built once."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    return {
        "nav_is_admin": user.is_admin_level,
        "nav_is_teacher": user.is_teacher,
        "nav_is_student": user.is_student,
        "nav_is_superadmin": user.is_superadmin,
    }
