import pytest

from app.extensions import db
from app.models import Application


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("company_name", "あ" * 101, "Field cannot be longer than 100 characters."),
        ("priority", "6", "志望度は1〜5で入力してください。"),
        ("application_url", "not-a-url", "正しいURLを入力してください。"),
    ],
)
def test_validation_errors(client, app, field, value, message):
    data = {
        "company_name": "テスト株式会社",
        "status": "応募予定",
        "priority": "3",
    }
    data[field] = value
    response = client.post("/applications/new", data=data)
    assert response.status_code == 200
    assert message in response.get_data(as_text=True)
    with app.app_context():
        assert Application.query.count() == 0
