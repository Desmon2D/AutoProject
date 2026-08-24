import json
import os
from pathlib import Path

from django.contrib.auth import get_user_model
from swirl.models import SearchProvider

base_url = os.environ["BOOKSTACK_BASE_URL"].strip().rstrip("/")
token_id = os.environ["BOOKSTACK_TOKEN_ID"].strip()
token_secret = os.environ["BOOKSTACK_TOKEN_SECRET"]
template_path = Path(
    os.environ.get(
        "BOOKSTACK_PROVIDER_TEMPLATE",
        "/opt/automation-swirl/searchproviders/bookstack.json",
    )
)
provider = json.loads(template_path.read_text(encoding="utf-8"))
provider["url"] = f"{base_url}/api/search"
provider["http_request_headers"]["Authorization"] = f"Token {token_id}:{token_secret}"
provider["page_fetch_config_json"]["headers"]["Authorization"] = (
    f"Token {token_id}:{token_secret}"
)

username = os.environ["SWIRL_USERNAME"]
owner = get_user_model().objects.get(username=username)
name = provider.pop("name")
SearchProvider.objects.update_or_create(
    name=name,
    defaults={"owner": owner, **provider},
)
print(f"BookStack SearchProvider configured: {name}")
