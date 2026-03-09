import os
from functools import wraps
from flask import Blueprint, redirect, render_template, session

def _staff_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("tb_user"):
            return redirect("/employee-portal/login?next=/training/")
        return view(*args, **kwargs)
    return wrapped

_template = os.path.join(os.path.dirname(__file__), "templates")
internal_bp = Blueprint("internal_training", __name__, url_prefix="/plugin/training_module", template_folder=_template)
public_bp = Blueprint("public_training", __name__, url_prefix="/training", template_folder=_template)


@internal_bp.get("/")
def admin_index():
    return redirect("/")


@public_bp.get("/")
@_staff_required
def public_index():
    return render_template("public/index.html", module_name="Training", module_description="View training and complete mandatory training.")


def get_blueprint():
    return internal_bp


def get_public_blueprint():
    return public_bp
