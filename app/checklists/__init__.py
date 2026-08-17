from flask import Blueprint


bp = Blueprint("checklists", __name__)

from . import routes  # noqa: E402, F401
from . import calendar_routes  # noqa: E402, F401
